# -*- coding: utf-8 -*-
"""
三人行 · 服务器入口（状态机版）
================================
FastAPI + WebSocket。

严格遵循《AI 开发规范》状态机：
  WAITING → DICE → PLAYING → ROUND_END → SETTLEMENT → NEXT_ROUND → PLAYING
                                         ↓
                                     GAME_END（10轮打完）

ROUND_END 之后禁止一切游戏逻辑，直到 NEXT_ROUND。

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

app = FastAPI(title="三人行·干瞪眼服务器", version="2.0.0")

# ---------------------------------------------------------------------------
# 状态常量（规范唯一合法状态）
# ---------------------------------------------------------------------------
STATE_WAITING    = "WAITING"
STATE_DICE      = "DICE"
STATE_PLAYING   = "PLAYING"
STATE_ROUND_END = "ROUND_END"
STATE_SETTLEMENT= "SETTLEMENT"
STATE_NEXT_ROUND= "NEXT_ROUND"
STATE_GAME_END  = "GAME_END"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
AUTO_START_DELAY   = 5      # 人齐后自动开局倒计时（秒）
ROUND_END_PAUSE    = 5      # 桌面停留：ROUND_END 后展示5秒
SETTLEMENT_DELAY   = 3      # 结算小提示展示：SETTLEMENT 默认3秒后自动下一轮
TURN_TIMEOUT_S     = 60     # 出牌超时：60秒不出自动操作
RECONNECT_GRACE    = 60     # 断线重连宽限期（秒）

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class Player:
    def __init__(self, seat: int, name: str, ws: WebSocket, is_bot: bool = False,
                 avatar: str = ""):
        self.seat = seat
        self.name = name
        self.avatar = avatar or "🐱"   # 默认头像
        self.player_id: str = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        self.ws = ws
        self.connected = True
        self.is_bot = is_bot


class Room:
    def __init__(self, code: str, host_name: str, host_ws: WebSocket, host_avatar: str = ""):
        self.code = code
        self.players: Dict[int, Player] = {}
        self.order: List[int] = []
        self.game: gl.GameSession = gl.GameSession(total_rounds=1)  # 升级版：一轮即一局
        self.started = False

        # ---- 状态机核心 ----
        # 唯一合法状态：WAITING / DICE / PLAYING / ROUND_END / SETTLEMENT / NEXT_ROUND / GAME_END
        self.state: str = STATE_WAITING

        self.first_round_first_seat: Optional[int] = None
        self.dice_order: Optional[List[Dict]] = None   # 掷骰结果
        self.practice_mode = False

        # 暂停
        self.paused = False
        self.paused_by: Optional[str] = None

        # 异步任务
        self.lock = asyncio.Lock()
        self._next_seat = 0
        self.auto_start_task: Optional[asyncio.Task] = None
        self.turn_timer_task: Optional[asyncio.Task] = None
        # ROUND_END → SETTLEMENT（5秒桌面停留）
        self.round_end_task: Optional[asyncio.Task] = None
        # SETTLEMENT → NEXT_ROUND（30秒黑板）
        self.settlement_task: Optional[asyncio.Task] = None
        # 提前关闭黑板（confirm_next）
        self.settlement_cancelled = False

        self.add_player(host_name, host_ws, host_avatar)

    # ---- 座位管理 ----

    def add_player(self, name: str, ws: WebSocket, avatar: str = "") -> Optional[int]:
        if len(self.players) >= 3:
            return None
        seat = self._next_seat
        self._next_seat += 1
        p = Player(seat, name, ws, avatar=avatar)
        self.players[seat] = p
        self.order.append(seat)
        return seat

    def add_bot(self, name: str) -> Optional[int]:
        if len(self.players) >= 3:
            return None
        seat = self._next_seat
        self._next_seat += 1
        p = Player(seat, name, None, is_bot=True)
        self.players[seat] = p
        self.order.append(seat)
        return seat

    def reconnect_player(self, player_id: str, ws: WebSocket) -> Optional[Player]:
        for p in self.players.values():
            if p.player_id == player_id:
                p.ws = ws
                p.connected = True
                return p
        return None

    def remove_player(self, seat: int):
        """玩家掉线：标记 disconnected，保留座位以便重连找回。
        不删除 players 记录（reconnect_player 依赖它按 player_id 找回）。"""
        if seat in self.players:
            self.players[seat].connected = False
            self.players[seat].ws = None
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

    # ---- 状态机断言（调试用）----

    def _assert_state(self, *states):
        """断言当前状态为其中之一，非则抛异常。"""
        if self.state not in states:
            raise RuntimeError(
                f"[状态错误] 当前状态为 {self.state}，操作仅在 {states} 下允许"
            )

    # ---- 游戏流程 ----

    def start_game(self) -> bool:
        """开局：WAITING → DICE（掷骰） → 自动进入 PLAYING"""
        if self.started or not self.is_full:
            return False
        self.started = True
        self.state = STATE_DICE

        # 掷骰定先手
        self.dice_order = gl.roll_dice()
        self.first_round_first_seat = self.dice_order[0]["seat"]

        # 广播 DICE 状态（客户端显示骰子动画）
        return True

    def proceed_from_dice(self):
        """骰子展示完毕后：切换到 PLAYING 并发牌"""
        self.state = STATE_PLAYING
        self.start_round(self.first_round_first_seat)

    def start_round(self, first_seat: int) -> gl.Round:
        return self.game.start_round(first_seat)

    def restart_game(self) -> bool:
        """大局结束重开：GAME_END → WAITING → DICE"""
        if not self.game.is_over:
            return False
        self.game.reset()
        self.started = True
        self.state = STATE_DICE
        # 随机先手（或重新掷骰）
        self.dice_order = gl.roll_dice()
        self.first_round_first_seat = self.dice_order[0]["seat"]
        return True

    # ---- 回合定时器 ----

    def cancel_turn_timer(self):
        if self.turn_timer_task and not self.turn_timer_task.done():
            self.turn_timer_task.cancel()
        self.turn_timer_task = None

    def schedule_turn_timer(self):
        """启动当前轮玩家超时定时器"""
        self.cancel_turn_timer()
        self.turn_timer_task = asyncio.ensure_future(self._turn_timer())

    # ---- 自动开局 ----

    def schedule_auto_start(self):
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
                    if self.start_game():
                        await broadcast(self, {"type": "DICE", "data": self.dice_order})
                        await send_view(self)
                        # 短暂延迟后进入 PLAYING（客户端动画播放）
                        await asyncio.sleep(2)
                        self.proceed_from_dice()
                        await broadcast(self, {"type": "notice", "message": "对局开始！"})
                        await send_view(self)
                        self.schedule_turn_timer()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    # ---- 回合超时 / Bot 调度（仅在 PLAYING 状态） ----

    async def _turn_timer(self):
        """仅在 PLAYING 状态下运行。ROUND_END 进入后此定时器必须停止。"""
        if not self.started or not self.game.round:
            return
        current_p = self.players.get(self.game.round.current_seat)
        is_bot = current_p is not None and current_p.is_bot
        delay = 2 if is_bot else TURN_TIMEOUT_S

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        # 双重检查：防止 CancelledError 后仍然执行
        if self.state != STATE_PLAYING:
            return
        if not self.started or not self.game.round:
            return

        async with self.lock:
            # 再次确认状态（加锁后）
            if self.state != STATE_PLAYING:
                return
            if self.turn_timer_task is None or self.turn_timer_task.done():
                return

            r = self.game.round
            if r.winner is not None:
                return  # 已有人赢，不再操作

            seat = r.current_seat
            try:
                import sys
                print(f"[T] seat={seat} is_bot={is_bot} "
                      f"hand={len(r.hands[seat]) if seat in r.hands else '?'} "
                      f"state={self.state}", file=sys.stderr, flush=True)

                if is_bot:
                    ok = await self._bot_act(seat, r)
                    if not ok:
                        ok, _ = r.pass_(seat)
                else:
                    # 真人超时
                    await broadcast(self, {"type": "notice",
                                         "message": f"座位{seat}超时{TURN_TIMEOUT_S}秒，自动操作"})
                    if r.current_play is not None:
                        ok, _ = r.pass_(seat)
                    else:
                        hand = r.hands[seat]
                        ok, _ = r.play(seat, [hand[0]]) if hand else r.pass_(seat)

                if ok:
                    await send_view(self)
                    # 有人手牌为0 → 进入 ROUND_END
                    if r.winner is not None:
                        await self._enter_round_end(r)
                    elif self.state == STATE_PLAYING:
                        self.schedule_turn_timer()
            except Exception:
                traceback.print_exc()

    async def _bot_act(self, seat: int, r: gl.Round) -> bool:
        """Bot 决策（仅 PLAYING 状态可调用）"""
        import bot_ai
        hand = r.hands[seat]
        if not hand:
            if r.winner is None:
                r.winner = seat
            return True
        current_play = r.current_play
        opp_hands = [len(r.hands[s]) for s in range(3) if s != seat]
        move = bot_ai.decide_move(hand, current_play, opp_hands, seat)
        if move:
            ok, _ = r.play(seat, move)
            return ok
        ok, _ = r.pass_(seat)
        return ok

    # -------------------------------------------------------------------------
    # 状态机核心：ROUND_END 严格流程
    # -------------------------------------------------------------------------
    # 进入条件：PLAYING 期间有人手牌=0
    # 执行步骤：
    #   1. 立即取消所有定时器（禁止继续操作）
    #   2. 计算分数
    #   3. 广播 ROUND_END
    #   4. 5秒桌面停留
    #   5. 进入 SETTLEMENT，广播 SHOW_SETTLEMENT
    #   6. 30秒黑板（cancel_settlement 可提前关闭）
    #   7. 进入 NEXT_ROUND，重新洗牌发牌
    #   8. 进入 PLAYING
    # -------------------------------------------------------------------------

    async def _enter_round_end(self, r: gl.Round):
        """PLAYING → ROUND_END（严格禁止任何游戏逻辑继续执行）"""
        import sys

        # 【步骤1】立即停止一切游戏逻辑
        self.cancel_turn_timer()           # 停止出牌定时器
        self.state = STATE_ROUND_END       # 状态锁定，禁止 play/pass/draw/ai

        winner_seat = r.winner
        winner_name = self.players.get(winner_seat, Player(0, f"座位{winner_seat}", None)).name
        print(f"[ROUND_END] winner=seat{winner_seat}({winner_name}) "
              f"hands={[len(r.hands[s]) for s in range(3)]}", file=sys.stderr, flush=True)

        # 【步骤2】计算分数（不允许修改任何游戏状态）
        result = self.game.finish_round()

        # 【步骤3】广播 ROUND_END（桌面停留5秒）
        await broadcast(self, {
            "type": "ROUND_END",
            "winner": winner_name,
            "winner_seat": winner_seat,
            "data": result,
        })
        await send_view(self)

        # 【步骤4】5秒桌面停留
        self.round_end_task = asyncio.ensure_future(self._round_end_pause())

    async def _round_end_pause(self):
        """ROUND_END → SETTLEMENT：桌面停留5秒"""
        try:
            await asyncio.sleep(ROUND_END_PAUSE)
        except asyncio.CancelledError:
            return

        if self.state != STATE_ROUND_END:
            return

        # 【步骤5】进入 SETTLEMENT，广播 SHOW_SETTLEMENT
        self.state = STATE_SETTLEMENT
        await broadcast(self, {"type": "SHOW_SETTLEMENT"})
        await send_view(self)

        # 【步骤6】30秒黑板（可提前被 confirm_next 取消）
        self.settlement_cancelled = False
        self.settlement_task = asyncio.ensure_future(self._settlement_wait())

    async def _settlement_wait(self):
        """SETTLEMENT：小提示展示几秒后自动进下一轮（或提前被 confirm_next 触发）。
        若已是最后一局（game.is_over），直接进入 GAME_END。"""
        elapsed = 0
        try:
            if self.game.is_over:
                # 最后一局打完：直接进 GAME_END
                self.settlement_cancelled = True
            else:
                while elapsed < SETTLEMENT_DELAY and not self.settlement_cancelled:
                    await asyncio.sleep(1)
                    elapsed += 1
        except asyncio.CancelledError:
            return

        if self.state != STATE_SETTLEMENT:
            return

        # 避免自我取消：清掉 settlement_task 引用，防止 _proceed_next_round 里
        # settlement_task.cancel() 取消当前正在执行的 task 自己（导致 GAME_OVER 广播被中断）
        self.settlement_task = None

        # 小提示展示结束 或 提前确认 或 最后一局结束，进入 NEXT_ROUND（或 GAME_END）
        await self._proceed_next_round()

    async def _proceed_next_round(self):
        """NEXT_ROUND → PLAYING"""
        if self.state not in (STATE_SETTLEMENT, STATE_NEXT_ROUND):
            return

        self.state = STATE_NEXT_ROUND
        self.settlement_cancelled = True

        # 取消黑板定时器
        if self.settlement_task and not self.settlement_task.done():
            self.settlement_task.cancel()

        # 检查是否大局结束
        if self.game.is_over:
            self.state = STATE_GAME_END
            ranking = self.game.compute_ranking()
            # 本轮结算结果（一局即一轮，history[-1] 就是本轮）
            last_result = self.game.history[-1] if self.game.history else {}
            await broadcast(self, {"type": "GAME_OVER", "data": {
                "ranking": ranking,
                "scores": self.game.scores,
                "round_result": last_result,
            }})
            await send_view(self)
            return

        # 重新洗牌发牌，先手为上轮赢家
        first = self.game.next_first_seat()
        self.game.start_round(first)
        self.state = STATE_PLAYING

        await broadcast(self, {"type": "NEXT_ROUND", "round": self.game.round_no})
        await broadcast(self, {"type": "notice", "message": f"第 {self.game.round_no} 小轮开始"})
        await send_view(self)
        self.schedule_turn_timer()

    def cancel_settlement(self):
        """confirm_next 提前触发：跳过黑板等待，立即进入 NEXT_ROUND"""
        self.settlement_cancelled = True


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
        others[s] = {"name": p.name, "connected": p.connected, "is_bot": p.is_bot,
                     "avatar": getattr(p, "avatar", "")}
    base = {
        "code": room.code,
        "started": room.started,
        "state": room.state,              # 规范：客户端必须知道当前状态
        "players": others,
        "my_seat": seat,
        "is_host": seat == room.host_seat,
        "round_no": room.game.round_no,
        "total_rounds": room.game.total_rounds,
        "scores": room.game.scores,
        "finished": room.game.is_over,
        "auto_starting": bool(room.auto_start_task and not room.auto_start_task.done()),
        "dice_order": room.dice_order,
        "practice": room.practice_mode,
        "paused": room.paused,
        "paused_by": room.paused_by,
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
            continue
        try:
            await p.ws.send_json({"type": "state", "data": player_view(room, s)})
        except Exception:
            pass


async def broadcast(room: Room, message: Dict[str, Any], except_seat: Optional[int] = None):
    for seat, p in list(room.players.items()):
        if seat == except_seat:
            continue
        if p.ws is None or p.is_bot:
            continue
        try:
            await p.ws.send_json(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 消息处理（严格按状态机）
# ---------------------------------------------------------------------------

async def handle_message(room: Room, seat: int, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mtype = msg.get("type")

    # ---- 全局消息（任何状态均可处理）----

    if mtype == "ping":
        return {"type": "pong"}

    # ---- WAITING 状态 ----

    if room.state == STATE_WAITING:
        if mtype == "start_game":
            if seat != room.host_seat:
                return {"type": "error", "message": "只有房主才能开始游戏"}
            if not room.is_full:
                return {"type": "error", "message": "还差人，等3人到齐"}
            if room.started:
                return {"type": "error", "message": "游戏已经开始"}
            async with room.lock:
                if room.start_game():
                    await broadcast(room, {"type": "DICE", "data": room.dice_order})
                    await send_view(room)
                    # 客户端动画播放约2秒后由服务端推进到 PLAYING
                    await asyncio.sleep(2)
                    room.proceed_from_dice()
                    await broadcast(room, {"type": "notice", "message": "对局开始！"})
                    await send_view(room)
                    room.schedule_turn_timer()
            return None

        if mtype == "restart_game":
            return {"type": "error", "message": "游戏还没开始"}

        # WAITING 下其他操作全部忽略
        return {"type": "error", "message": f"当前状态为 {room.state}，请等待开始"}

    # ---- GAME_END 状态 ----

    if room.state == STATE_GAME_END:
        if mtype == "restart_game":
            if seat != room.host_seat:
                return {"type": "error", "message": "只有房主才能开新一局"}
            async with room.lock:
                if room.restart_game():
                    await broadcast(room, {"type": "DICE", "data": room.dice_order})
                    await send_view(room)
                    await asyncio.sleep(2)
                    room.proceed_from_dice()
                    await broadcast(room, {"type": "notice", "message": "新一局开始！积分已清零"})
                    await send_view(room)
                    room.schedule_turn_timer()
            return None
        return {"type": "error", "message": f"游戏已结束，请等待房主开新一局"}

    # ---- ROUND_END / SETTLEMENT / NEXT_ROUND 状态 ----
    # 这些状态下禁止任何游戏操作

    if room.state in (STATE_ROUND_END, STATE_SETTLEMENT, STATE_NEXT_ROUND):
        if mtype == "confirm_next":
            # 提前结束结算等待，进入 NEXT_ROUND
            room.cancel_settlement()
            async with room.lock:
                await room._proceed_next_round()
            return None

        if mtype == "pause":
            return {"type": "error", "message": f"当前状态 {room.state}，暂不支持暂停"}

        if mtype == "resume":
            return {"type": "error", "message": f"当前状态 {room.state}，暂不支持恢复"}

        # 禁止：play / pass / start_game 等所有游戏操作
        return {"type": "error", "message": f"当前状态为 {room.state}，请等待结算完成"}

    # ---- DICE 状态（掷骰子）----
    # 禁止一切出牌/补牌操作

    if room.state == STATE_DICE:
        if mtype == "play" or mtype == "pass":
            return {"type": "error", "message": "请等待骰子结果"}
        if mtype == "confirm_next":
            return {"type": "error", "message": "游戏还未开始"}
        return {"type": "error", "message": f"当前状态为 {room.state}"}

    # ---- PLAYING 状态（唯一允许游戏逻辑的状态）----

    if room.state != STATE_PLAYING:
        # 兜底：任何未处理状态
        return {"type": "error", "message": f"未知状态 {room.state}"}

    # ---- 以下仅 PLAYING 状态可执行 ----

    if not room.started or not room.game.round:
        return {"type": "error", "message": "游戏还没开始"}

    if mtype == "pause":
        if room.paused:
            return {"type": "error", "message": "游戏已暂停"}
        async with room.lock:
            room.paused = True
            room.cancel_turn_timer()
            pname = room.players.get(seat).name if seat in room.players else f"座位{seat}"
            room.paused_by = pname
            await broadcast(room, {"type": "paused", "data": {"by": pname}})
            await send_view(room)
        return None

    if mtype == "resume":
        if not room.paused:
            return {"type": "error", "message": "游戏未暂停"}
        async with room.lock:
            room.paused = False
            room.paused_by = None
            await broadcast(room, {"type": "resumed"})
            await send_view(room)
            if room.game.round:
                room.schedule_turn_timer()
        return None

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
            if room.state != STATE_PLAYING:
                return {"type": "error", "message": "游戏状态已变化"}
            ok, err = r.play(seat, card_objs)
        if not ok:
            return {"type": "error", "message": err}
        await send_view(room)
        if r.winner is not None:
            await room._enter_round_end(r)
        elif room.state == STATE_PLAYING:
            room.schedule_turn_timer()
        return None

    if mtype == "pass":
        async with room.lock:
            if room.state != STATE_PLAYING:
                return {"type": "error", "message": "游戏状态已变化"}
            ok, err = r.pass_(seat)
        if not ok:
            return {"type": "error", "message": err}
        await send_view(room)
        if r.winner is not None:
            await room._enter_round_end(r)
        elif room.state == STATE_PLAYING:
            room.schedule_turn_timer()
        return None

    return {"type": "error", "message": f"未知操作：{mtype}"}


# ---------------------------------------------------------------------------
# WebSocket 端点
# ---------------------------------------------------------------------------

async def ws_endpoint(ws: WebSocket, code: str, name: str, player_id: str = "",
                      avatar: str = ""):
    await ws.accept()
    room = ROOMS.get(code)
    if room is None:
        await ws.send_json({"type": "error", "message": "房间不存在"})
        await ws.close()
        return

    # 断线重连
    if player_id:
        p = room.reconnect_player(player_id, ws)
        if p:
            seat = p.seat
            if avatar:
                p.avatar = avatar
            await broadcast(room, {"type": "notice",
                                   "message": f"{p.name} 重新连接"})
            await send_view(room)
            return await _player_loop(room, seat, name, ws)

    if room.started:
        await ws.send_json({"type": "error", "message": "游戏已开始，无法加入"})
        await ws.close()
        return
    if room.is_full:
        await ws.send_json({"type": "error", "message": "房间已满"})
        await ws.close()
        return

    seat = room.add_player(name, ws, avatar=avatar)
    if seat is None:
        await ws.send_json({"type": "error", "message": "房间已满"})
        await ws.close()
        return

    await broadcast(room, {"type": "notice", "message": f"{name} 加入了房间"})
    await send_view(room)

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
                await ws.send_json({"type": "error", "message": "消息格式错误"})
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
        traceback.print_exc()
    finally:
        # 仅当该座位当前连接仍为本人时才移除（防止重连后误删）
        if (room.players.get(seat) is not None
                and room.players[seat].ws is ws):
            if room.num_players == 1:
                # 最后一人离开：销毁房间
                if room.auto_start_task:
                    room.auto_start_task.cancel()
                room.cancel_turn_timer()
                if room.round_end_task:
                    room.round_end_task.cancel()
                if room.settlement_task:
                    room.settlement_task.cancel()
                ROOMS.pop(room.code, None)
            else:
                pname = room.players[seat].name
                room.remove_player(seat)
                room.cancel_turn_timer()
                await broadcast(room, {"type": "notice",
                                       "message": f"{pname} 离开了房间"})
                await send_view(room)


@app.websocket("/ws/room")
async def ws_room(ws: WebSocket, code: str, name: str, pid: str = "", avatar: str = ""):
    await ws_endpoint(ws, code, name, pid, avatar)


@app.websocket("/ws/create")
async def ws_create(ws: WebSocket, name: str, avatar: str = ""):
    await ws.accept()
    code = gen_room_code()
    room = Room(code, name, ws, avatar)
    ROOMS[code] = room
    await ws.send_json({"type": "room_created", "data": {
        "code": code,
        "player_id": room.players[0].player_id,
    }})
    await send_view(room)
    await _player_loop(room, room.host_seat, name, ws)


@app.websocket("/ws/practice")
async def ws_practice(ws: WebSocket, name: str, avatar: str = ""):
    await ws.accept()
    code = gen_room_code()
    room = Room(code, name, ws, avatar)
    room.add_bot("电脑1")
    room.add_bot("电脑2")
    room.practice_mode = True
    ROOMS[code] = room
    await ws.send_json({"type": "room_created", "data": {
        "code": code,
        "player_id": room.players[0].player_id,
        "practice": True,
    }})
    await send_view(room)
    # 练习模式自动开局
    if room.start_game():
        await broadcast(room, {"type": "DICE", "data": room.dice_order})
        await send_view(room)
        await asyncio.sleep(2)
        room.proceed_from_dice()
        await broadcast(room, {"type": "notice",
                               "message": "练习模式开始！对战两个电脑"})
        await send_view(room)
        room.schedule_turn_timer()
    await _player_loop(room, room.host_seat, name, ws)


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return {"ok": True, "game": "三人行·干瞪眼", "rooms": len(ROOMS)}
