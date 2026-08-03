# -*- coding: utf-8 -*-
"""
三人行 · 服务器入口
===================
FastAPI + WebSocket。
职责：房间管理、玩家加入、开局（自动/手动）、实时对局状态同步。

优化：
- 自动开局：3 人齐后倒计时自动开始，房主也可手动开始
- 断线重连：玩家掉线后凭 player_id 重连回原座位
- 心跳保活：ping/pong，防止空闲休眠（Render 免费版）
- 重开局：10 轮打完，房主点按钮开新大局（分数清零）
- 服务端加锁：房间操作串行化，防并发冲突

部署：uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import json
import random
import string
import traceback
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

import game_logic as gl

app = FastAPI(title="三人行·干瞪眼服务器", version="1.0.1")

AUTO_START_DELAY = 5          # 人齐后 5 秒自动开局
ROUND_RESULT_PAUSE = 5        # 单轮结算后展示结果 5 秒
RECONNECT_GRACE = 60          # 断线后 60 秒内可重连
TURN_TIMEOUT_S = 60           # 出牌超时：60 秒不出自动操作


# ---------------------------------------------------------------------------
# 房间数据结构
# ---------------------------------------------------------------------------

class Player:
    def __init__(self, seat: int, name: str, ws: WebSocket, is_bot: bool = False):
        self.seat = seat
        self.name = name
        self.player_id: str = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        self.ws = ws
        self.connected = True
        self.is_bot = is_bot


class Room:
    """一个 3 人房间。"""

    def __init__(self, code: str, host_name: str, host_ws: WebSocket):
        self.code = code
        self.players: Dict[int, Player] = {}          # seat -> player
        self.order: List[int] = []                     # 加入顺序
        self.game: gl.GameSession = gl.GameSession(total_rounds=10)
        self.started = False
        self.first_round_first_seat: Optional[int] = None
        self.lock = asyncio.Lock()
        self._next_seat = 0
        self.auto_start_task: Optional[asyncio.Task] = None
        self.turn_timer_task: Optional[asyncio.Task] = None   # 出牌超时定时器
        self.turn_timeout_s = TURN_TIMEOUT_S                  # 60秒不出视为过牌
        self.dice_order: Optional[list] = None                # 掷骰子结果（先手顺序）
        self.practice_mode = False                            # 练习模式（含 Bot）
        self.paused = False                                   # 暂停状态
        self.paused_by: Optional[str] = None                  # 谁暂停的
        self.waiting_confirm = False                          # 等待三家确认开始下一轮
        self.confirm_seats: set = set()                       # 已确认的座位
        self.next_round_task: Optional[asyncio.Task] = None   # 自动开下一轮的任务
        self.add_player(host_name, host_ws)

    def add_player(self, name: str, ws: WebSocket) -> Optional[int]:
        """新玩家加入。返回座位号，满员返回 None。"""
        if len(self.players) >= 3:
            return None
        seat = self._next_seat
        self._next_seat += 1
        p = Player(seat, name, ws)
        self.players[seat] = p
        self.order.append(seat)
        return seat

    def add_bot(self, name: str) -> Optional[int]:
        """添加 Bot 玩家（无 WebSocket 连接）。"""
        if len(self.players) >= 3:
            return None
        seat = self._next_seat
        self._next_seat += 1
        p = Player(seat, name, None, is_bot=True)
        self.players[seat] = p
        self.order.append(seat)
        return seat

    def reconnect_player(self, player_id: str, ws: WebSocket) -> Optional[Player]:
        """断线重连：凭 player_id 找回座位并替换 WebSocket。"""
        for p in self.players.values():
            if p.player_id == player_id:
                p.ws = ws
                p.connected = True
                return p
        return None

    def remove_player(self, seat: int):
        """移除玩家。游戏进行中不重排座位，保持对局状态稳定。"""
        if seat in self.players:
            self.players[seat].connected = False
            del self.players[seat]
            if seat in self.order:
                self.order.remove(seat)

    @property
    def host_seat(self) -> int:
        return self.order[0] if self.order else 0

    @property
    def is_full(self) -> bool:
        return len(self.players) == 3

    @property
    def num_players(self) -> int:
        return len(self.players)

    # ---- 开局 ----

    def start_game(self) -> bool:
        """开局（仅当 3 人齐）。返回是否成功开始。"""
        if self.started or not self.is_full:
            return False
        self.started = True
        # 掷骰子决定首轮先手与座位顺序
        roll_result = gl.roll_dice()
        # roll_result 按先手顺序排列：第一个是首轮先手
        self.first_round_first_seat = roll_result[0]["seat"]
        self.dice_order = roll_result          # 记录骰子结果（供客户端展示）
        self.start_round(self.first_round_first_seat)
        return True

    def start_round(self, first_seat: int) -> gl.Round:
        return self.game.start_round(first_seat)

    def restart_game(self) -> bool:
        """大局结束后开新一局：清分，重新开局。"""
        if not self.game.is_over:
            return False
        self.game.reset()
        self.started = True
        self.first_round_first_seat = random.randint(0, 2)
        self.start_round(self.first_round_first_seat)
        return True

    def schedule_auto_start(self):
        """人齐后启动自动开局倒计时（房主可先手动开始）。"""
        if self.auto_start_task and not self.auto_start_task.done():
            return
        self.auto_start_task = asyncio.ensure_future(self._auto_start())

    async def _auto_start(self):
        try:
            await asyncio.sleep(AUTO_START_DELAY)
            if self.started or not self.is_full:
                return
            async with self.lock:
                if not self.started and self.is_full:
                    self.start_game()
                    await broadcast(self, {"type": "notice", "message": "人齐，对局自动开始！"})
                    await send_view(self)
                    self.schedule_turn_timer()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    # ---- 出牌超时 / Bot 调度 ----

    def cancel_turn_timer(self):
        """取消当前出牌超时定时器。"""
        if self.turn_timer_task and not self.turn_timer_task.done():
            self.turn_timer_task.cancel()
        self.turn_timer_task = None

    def schedule_turn_timer(self):
        """为当前轮到的玩家启动定时器。
        - 真人：超时自动操作（60秒）
        - Bot：短延迟后走 AI 决策（2秒，模拟思考）
        """
        self.cancel_turn_timer()
        self.turn_timer_task = asyncio.ensure_future(self._turn_timer())

    async def _turn_timer(self):
        """定时器触发：Bot 走 AI 决策；真人超时自动操作。"""
        # 判断当前玩家是不是 Bot，决定等待时长
        if not self.started or not self.game.round:
            return
        current_player = self.players.get(self.game.round.current_seat)
        is_bot = current_player is not None and current_player.is_bot
        delay = 2 if is_bot else self.turn_timeout_s
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self.started or not self.game.round:
            return
        async with self.lock:
            # 确认定时器还对应当前状态
            if self.turn_timer_task is None or self.turn_timer_task.done():
                return
            r = self.game.round
            if r.winner is not None:
                return
            seat = r.current_seat
            try:
                import sys
                print(f"[T] seat={seat} is_bot={is_bot} hand={len(r.hands[seat]) if seat in r.hands else '?'} winner={r.winner}", file=sys.stderr, flush=True)
                if is_bot:
                    # Bot：调用 AI 决策出牌
                    ok = await self._bot_act(seat, r)
                    if not ok:
                        # AI 决策失败则过牌
                        ok, _ = r.pass_(seat)
                else:
                    # 真人：超时自动操作
                    await broadcast(self, {"type": "notice",
                                           "message": f"座位{seat}超过{self.turn_timeout_s}秒未出牌，自动操作"})
                    if r.current_play is not None:
                        ok, _ = r.pass_(seat)
                    else:
                        hand = r.hands[seat]
                        if hand:
                            ok, _ = r.play(seat, [hand[0]])
                        else:
                            ok, _ = r.pass_(seat)
                if ok:
                    await send_view(self)
                    # 兜底检查：任何玩家手牌为0且winner未设 → 强制结算
                    if r.winner is None:
                        for s in range(3):
                            if not r.hands[s]:
                                r.winner = s
                                print(f"[SAFETY] 座位{s}手牌空，强制设为winner", file=sys.stderr, flush=True)
                                break
                    # 若有人赢了一轮 → 结算
                    if r.winner is not None:
                        self.cancel_turn_timer()
                        await finish_round(self, r)
                    else:
                        self.schedule_turn_timer()
            except Exception:
                traceback.print_exc()

    async def _bot_act(self, seat: int, r) -> bool:
        """Bot 决策出牌。返回是否成功执行了操作。"""
        import bot_ai
        hand = r.hands[seat]
        if not hand:
            # Bot 手牌已空：如果是它出完的，应该已经设置 winner
            # 若 winner 未设（异常情况），视为出完赢
            if r.winner is None:
                r.winner = seat
            return True
        current_play = r.current_play
        opp_hands = [len(r.hands[s]) for s in range(3) if s != seat]
        move = bot_ai.decide_move(hand, current_play, opp_hands, seat)
        if move:
            ok, _ = r.play(seat, move)
            return ok
        # 过牌
        ok, _ = r.pass_(seat)
        return ok


# ---------------------------------------------------------------------------
# 全局房间表
# ---------------------------------------------------------------------------

ROOMS: Dict[str, Room] = {}

def gen_room_code() -> str:
    while True:
        code = "".join(random.choices(string.digits, k=6))
        if code not in ROOMS:
            return code


# ---------------------------------------------------------------------------
# 视图（玩家视角）
# ---------------------------------------------------------------------------

def card_list_for_send(cards: List[Dict]) -> List[Dict]:
    return [{"r": c["r"], "s": c["s"], "v": c["v"]} for c in cards]


def player_view(room: Room, seat: int) -> Dict[str, Any]:
    others = {}
    for s, p in room.players.items():
        others[s] = {"name": p.name, "connected": p.connected, "is_bot": p.is_bot}
    base = {
        "code": room.code,
        "started": room.started,
        "players": others,
        "my_seat": seat,
        "is_host": seat == room.host_seat,
        "round_no": room.game.round_no,
        "total_rounds": room.game.total_rounds,
        "scores": room.game.scores,
        "finished": room.game.is_over,
        "auto_starting": bool(room.auto_start_task and not room.auto_start_task.done()),
        "dice_order": room.dice_order,      # 掷骰子结果（先手顺序）
        "practice": room.practice_mode,     # 练习模式标记
        "paused": room.paused,              # 暂停状态
        "paused_by": room.paused_by,
        "waiting_confirm": room.waiting_confirm,   # 等待三家确认
        "confirm_seats": list(room.confirm_seats), # 已确认座位
    }
    if room.started and room.game.round:
        r: gl.Round = room.game.round
        base.update({
            "current_seat": r.current_seat,
            "current_play": r.current_play,
            "current_play_seat": r.current_play_seat,
            "hand": card_list_for_send(r.hands[seat]),
            "hand_count": {s: len(r.hands[s]) for s in range(3)},
            "pile_count": len(r.pile),
            "can_pass": r.current_play is not None and seat == r.current_seat,
            "can_play": seat == r.current_seat and r.winner is None,
            "winner_seat": r.winner,
            "has_played": r.has_played,
            "round_multipliers": [b["kind"] for b in r.round_multipliers],
        })
    return base


async def send_view(room: Room):
    for s, p in room.players.items():
        if p.ws is None or p.is_bot:
            continue   # 跳过 Bot（无连接）
        try:
            await p.ws.send_json({"type": "state", "data": player_view(room, s)})
        except Exception:
            pass


async def broadcast(room: Room, message: Dict[str, Any], except_seat: Optional[int] = None):
    for seat, p in list(room.players.items()):
        if seat == except_seat:
            continue
        if p.ws is None or p.is_bot:
            continue   # 跳过 Bot（无连接）
        try:
            await p.ws.send_json(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------

async def handle_message(room: Room, seat: int, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mtype = msg.get("type")

    if mtype == "ping":
        return {"type": "pong"}

    if mtype == "start_game":
        if seat != room.host_seat:
            return {"type": "error", "message": "只有房主才能开始游戏"}
        if not room.is_full:
            return {"type": "error", "message": "还差人，等 3 人到齐"}
        if room.started:
            return {"type": "error", "message": "游戏已经开始"}
        async with room.lock:
            if room.start_game():
                await broadcast(room, {"type": "notice", "message": "对局开始！"})
                await send_view(room)
                room.schedule_turn_timer()
        return None

    if mtype == "restart_game":
        if seat != room.host_seat:
            return {"type": "error", "message": "只有房主才能开启新一局"}
        if not room.game.is_over:
            return {"type": "error", "message": "本大局还没结束"}
        async with room.lock:
            if room.restart_game():
                await broadcast(room, {"type": "notice", "message": "新一局开始！积分已清零"})
                await send_view(room)
                room.schedule_turn_timer()
        return None

    if mtype == "confirm_next":
        if room.paused:
            return {"type": "error", "message": "游戏已暂停，无法确认"}
        async with room.lock:
            # 取消 30 秒自动开下一轮任务，立即开下一轮
            if room.next_round_task and not room.next_round_task.done():
                room.next_round_task.cancel()
            if room.game.round and room.game.round.winner is not None:
                await start_next_round(room)
        return None

    if mtype == "pause":
        if room.paused:
            return {"type": "error", "message": "游戏已处于暂停状态"}
        async with room.lock:
            room.paused = True
            room.cancel_turn_timer()   # 暂停所有定时器
            pname = room.players.get(seat).name if seat in room.players else f"座位{seat}"
            room.paused_by = pname
            await broadcast(room, {"type": "paused", "data": {"by": pname}})
            await send_view(room)
        return None

    if mtype == "resume":
        if not room.paused:
            return {"type": "error", "message": "游戏未处于暂停状态"}
        async with room.lock:
            room.paused = False
            room.paused_by = None
            await broadcast(room, {"type": "resumed"})
            await send_view(room)
            if room.game.round:
                room.schedule_turn_timer()   # 恢复定时器
        return None

    if not room.started or not room.game.round:
        return {"type": "error", "message": "游戏还没开始"}

    r: gl.Round = room.game.round
    if mtype == "play":
        cards = msg.get("cards", [])
        if not isinstance(cards, list) or not cards:
            return {"type": "error", "message": "出牌不能为空"}
        card_objs = []
        for c in cards:
            rank = c.get("r")
            suit = c.get("s", "")
            if rank in ("小王", "大王"):
                card_objs.append({"r": rank, "s": gl.JOKER_SUIT,
                                  "v": gl.RANK_VALUE[rank]})
            else:
                card_objs.append({"r": rank, "s": suit,
                                  "v": gl.RANK_VALUE.get(rank, -1)})
        async with room.lock:
            ok, err = r.play(seat, card_objs)
        if not ok:
            return {"type": "error", "message": err}
        await send_view(room)
        if r.winner is not None:
            room.cancel_turn_timer()
            await finish_round(room, r)
        else:
            room.schedule_turn_timer()
        return None

    if mtype == "pass":
        async with room.lock:
            ok, err = r.pass_(seat)
        if not ok:
            return {"type": "error", "message": err}
        await send_view(room)
        # 补牌后若检测到 winner → 立即结算
        if r.winner is not None:
            room.cancel_turn_timer()
            await finish_round(room, r)
        else:
            room.schedule_turn_timer()
        return None

    return {"type": "error", "message": f"未知操作：{mtype}"}


async def _auto_next_round(room: Room):
    """结算展示 35 秒后自动开下一轮（服务端兜底，比客户端30秒长5秒）；confirm_next 可提前取消。"""
    try:
        await asyncio.sleep(35)
        if room.game.is_over or room.paused:
            return
        await start_next_round(room)
    except asyncio.CancelledError:
        pass


async def finish_round(room: Room, r: gl.Round):
    import sys
    print(f"[FINISH] 结算触发! winner={r.winner} 手牌={[len(r.hands[s]) for s in range(3)]}", file=sys.stderr, flush=True)
    result = room.game.finish_round()
    # 桌面停留 5 秒（让玩家看到手牌为0的瞬间）
    await asyncio.sleep(5)

    # 最后一大局：直接全局结算，不需要确认
    if room.game.is_over:
        ranking = room.game.compute_ranking()
        await broadcast(room, {
            "type": "game_over",
            "data": {"ranking": ranking, "scores": room.game.scores},
        })
        return

    # 先调度兜底自动开下一轮：即便下方广播异常，也保证一定能进下一轮，绝不会卡死
    room.next_round_task = asyncio.ensure_future(_auto_next_round(room))
    # 广播结算结果 → 客户端显示黑板
    try:
        await broadcast(room, {"type": "round_result", "data": result})
    except Exception:
        import traceback as _tb
        _tb.print_exc()


async def _broadcast_confirm(room: Room):
    """广播三家确认状态。"""
    await broadcast(room, {
        "type": "round_confirm",
        "data": {"waiting": room.waiting_confirm, "confirmed": list(room.confirm_seats)},
    })
    await send_view(room)


async def start_next_round(room: Room):
    """三家确认后开下一轮。"""
    room.waiting_confirm = False
    room.confirm_seats = set()
    first = room.game.next_first_seat()
    room.game.start_round(first)
    await broadcast(room, {"type": "notice", "message": f"第 {room.game.round_no} 小轮开始"})
    await send_view(room)
    room.schedule_turn_timer()


# ---------------------------------------------------------------------------
# WebSocket 端点
# ---------------------------------------------------------------------------

async def ws_endpoint(ws: WebSocket, code: str, name: str, player_id: str = ""):
    await ws.accept()
    room = ROOMS.get(code)
    if room is None:
        await ws.send_json({"type": "error", "message": "房间不存在，请检查房间号"})
        await ws.close()
        return

    # 断线重连：凭 player_id 找回原座位
    if player_id:
        p = room.reconnect_player(player_id, ws)
        if p:
            seat = p.seat
            await broadcast(room, {"type": "notice", "message": f"{p.name} 重新连接"})
            await send_view(room)
            return await _player_loop(room, seat, name)

    if room.started:
        await ws.send_json({"type": "error", "message": "对局已开始，无法加入"})
        await ws.close()
        return
    if room.is_full:
        await ws.send_json({"type": "error", "message": "房间已满（3人）"})
        await ws.close()
        return

    seat = room.add_player(name, ws)
    if seat is None:
        await ws.send_json({"type": "error", "message": "房间已满"})
        await ws.close()
        return

    await broadcast(room, {"type": "notice", "message": f"{name} 加入了房间"})
    await send_view(room)

    # 人齐自动开局
    if room.is_full:
        room.schedule_auto_start()

    await _player_loop(room, seat, name, ws)


async def _player_loop(room: Room, seat: int, name: str, ws: WebSocket):
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                try:
                    await ws.send_json({"type": "error", "message": "消息格式错误"})
                except Exception:
                    break
                continue
            resp = await handle_message(room, seat, msg)
            if resp is not None:
                try:
                    await ws.send_json(resp)
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        # 连接异常：打印堆栈便于排查（之前静默吞掉会导致“赢了不结算”类卡死无日志）
        import traceback as _tb
        _tb.print_exc()
    finally:
        # 仅当该 seat 还对应本连接时才移除（防止重连后误删新连接）
        if room.players.get(seat) is not None and room.players[seat].ws is ws:
            if room.num_players == 1:
                if room.auto_start_task:
                    room.auto_start_task.cancel()
                room.cancel_turn_timer()
                ROOMS.pop(room.code, None)
            else:
                pname = room.players[seat].name
                room.remove_player(seat)
                room.cancel_turn_timer()   # 有人离开，暂停超时定时
                await broadcast(room, {"type": "notice", "message": f"{pname} 离开了房间"})
                await send_view(room)


@app.websocket("/ws/room")
async def ws_room(ws: WebSocket, code: str, name: str, pid: str = ""):
    """加入房间 / 断线重连。"""
    await ws_endpoint(ws, code, name, pid)


@app.websocket("/ws/create")
async def ws_create(ws: WebSocket, name: str):
    """创建房间。"""
    await ws.accept()
    code = gen_room_code()
    room = Room(code, name, ws)
    ROOMS[code] = room
    await ws.send_json({"type": "room_created",
                        "data": {"code": code, "player_id": room.players[0].player_id}})
    await send_view(room)
    await _player_loop(room, room.host_seat, name, ws)


@app.websocket("/ws/practice")
async def ws_practice(ws: WebSocket, name: str):
    """创建练习房间：1 真人 + 2 个电脑 Bot。"""
    await ws.accept()
    code = gen_room_code()
    room = Room(code, name, ws)
    # 补 2 个 Bot
    room.add_bot("电脑1")
    room.add_bot("电脑2")
    room.practice_mode = True
    ROOMS[code] = room
    await ws.send_json({"type": "room_created",
                        "data": {"code": code, "player_id": room.players[0].player_id,
                                 "practice": True}})
    await send_view(room)
    # 练习房间自动开局（无需等3人齐）
    if room.is_full:
        room.start_game()
        await broadcast(room, {"type": "notice", "message": "练习模式开始！对战两个电脑"})
        await send_view(room)
        room.schedule_turn_timer()
    await _player_loop(room, room.host_seat, name, ws)


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return {"ok": True, "game": "三人行·干瞪眼", "rooms": len(ROOMS)}
