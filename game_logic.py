# -*- coding: utf-8 -*-
"""
三人行 · 干瞪眼（3人版）核心规则引擎
======================================
纯逻辑模块，不依赖网络 / UI，可单独单元测试。

牌型、大小排序、压制规则、补牌机制、胜负判定、春天、记分 —— 全部按用户规则实现。
"""
import random
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量与基础数据
# ---------------------------------------------------------------------------

# 牌序：3＜4＜5＜6＜7＜8＜9＜10＜J＜Q＜K＜A＜2＜赖子＜小王＜大王
CARD_ORDER: List[str] = ["3", "4", "5", "6", "7", "8", "9", "10",
                         "J", "Q", "K", "A", "2", "赖子", "小王", "大王"]
RANK_VALUE: Dict[str, int] = {r: i for i, r in enumerate(CARD_ORDER)}

SUITS = ["S", "H", "C", "D"]          # 黑桃 / 红桃 / 梅花 / 方块
SUIT_CN = {"S": "♠", "H": "♥", "C": "♣", "D": "♦"}
JOKER_SUIT = "JK"

# 牌型
SINGLE        = "single"          # 单张
PAIR          = "pair"            # 对子
STRAIGHT      = "straight"        # 顺子（≥3张连续，不含2、王）
STRAIGHT_PAIR = "straight_pair"   # 连对（≥2组连续对子，不含2、王）
BOMB_3        = "bomb3"           # 普通炸弹（3张）倍率 ×2
BOMB_4        = "bomb4"           # 氢弹（4张）倍率 ×4
BOMB_JOKER    = "bomb_joker"      # 双王炸弹（大王+小王）倍率 ×4

NORMAL_TYPES = {SINGLE, PAIR, STRAIGHT, STRAIGHT_PAIR}
BOMB_TYPES   = {BOMB_3, BOMB_4, BOMB_JOKER}

# 炸弹优先级：普通炸弹 < 双王炸弹 < 氢弹（氢弹可压双王，双王可压普通炸弹）
BOMB_LEVEL = {BOMB_3: 0, BOMB_JOKER: 1, BOMB_4: 2}

COMBO_CN = {
    SINGLE: "单张", PAIR: "对子", STRAIGHT: "顺子",
    STRAIGHT_PAIR: "连对", BOMB_3: "炸弹×2", BOMB_4: "氢弹×4",
    BOMB_JOKER: "双王×4",
}

# ---------------------------------------------------------------------------
# 扑克牌基础
# ---------------------------------------------------------------------------

def build_deck() -> List[Dict[str, Any]]:
    """构建完整 55 张扑克牌（54 张普通牌 + 1 张赖子）。"""
    deck: List[Dict[str, Any]] = []
    for rank in CARD_ORDER[:13]:                # 3 ~ 2 各4张
        v = RANK_VALUE[rank]
        for s in SUITS:
            deck.append({"r": rank, "s": s, "v": v})
    deck.append({"r": "小王", "s": JOKER_SUIT, "v": RANK_VALUE["小王"]})
    deck.append({"r": "大王", "s": JOKER_SUIT, "v": RANK_VALUE["大王"]})
    deck.append({"r": "赖子", "s": "LZ", "v": RANK_VALUE["赖子"]})  # 1张赖子
    return deck


def sort_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按点数（同点按花色）排序。"""
    return sorted(cards, key=lambda c: (c["v"], c["s"]))


def roll_dice(seats=(0, 1, 2),
              rng: Optional[Callable[[], int]] = None) -> List[Dict[str, Any]]:
    """
    掷骰子决定座位顺序（开局彩蛋）。
    每轮给所有座位掷 1 个 6 面骰子；若存在点数相同，则该轮无效，全体重掷；
    直到一轮中三人点数各不相同，按点数从大到小排序（先手、第二、第三）。
    返回列表：[{"seat":.., "rolls":[点数序列], "final":最终点数}, ...] 按先手顺序排列。
    """
    rng = rng or (lambda: random.randint(1, 6))
    seats = list(seats)
    results: Dict[int, List[int]] = {s: [] for s in seats}

    while True:
        for s in seats:
            results[s].append(rng())
        last = {s: results[s][-1] for s in seats}
        if len(set(last.values())) == len(seats):
            break   # 点数各不相同，完成

    # 按最终点数降序
    ordered = sorted(seats, key=lambda s: last[s], reverse=True)
    return [{"seat": s, "rolls": results[s], "final": last[s]} for s in ordered]


# ---------------------------------------------------------------------------
# 牌型识别与压制规则
# ---------------------------------------------------------------------------

def _consecutive(vals: List[int]) -> bool:
    """是否严格连续递增（步长1）。"""
    return all(vals[i] + 1 == vals[i + 1] for i in range(len(vals) - 1))


def _consecutive_pairs(vals: List[int]) -> bool:
    """是否连续对子：每个点数出现恰好2次，且点数连续。"""
    if len(vals) % 2:
        return False
    if any(vals[i] != vals[i + 1] for i in range(0, len(vals), 2)):
        return False
    return _consecutive(vals[0::2])


def is_wild(c: Dict[str, Any]) -> bool:
    return c["r"] == "赖子"


def classify(cards):
    """
    赖子万能牌（用户确认最终版）：
    - 赖子不能单出
    - 赖子+1张非王 = 对子
    - 赖子+2张同rank非王 = 炸弹3
    - 赖子+3张同rank非王 = 氢弹4
    - 赖子可填顺子/连对的缝隙，也可扩展边界
    - 赖子不能与王组合
    """
    if not cards:
        return None
    cs = sort_cards(cards)
    n = len(cs)

    num_wild = sum(1 for c in cs if is_wild(c))
    real_cards = [c for c in cs if not is_wild(c)]
    rv_vals = sorted(set(c["v"] for c in real_cards))
    KING_VAL = RANK_VALUE["小王"]   # v >= KING_VAL is King

    rv_counter = {}
    for c in real_cards:
        rv_counter[c["v"]] = rv_counter.get(c["v"], 0) + 1

    def rv_top():
        if not rv_counter:
            return (0, 0)
        return max(rv_counter.items(), key=lambda x: x[1])

    def gaps_of(vals):
        g = 0
        for i in range(len(vals) - 1):
            g += vals[i+1] - vals[i] - 1
        return g

    def try_straight():
        """含赖子的顺子：枚举 lo，验证区间能否容纳实牌且缺=赖子"""
        if len(real_cards) < 2:
            return None
        g = gaps_of(rv_vals)
        if g > num_wild:
            return None
        # 枚举顺子起点 lo：lo_min 考虑赖子可向左扩展，lo_max 确保不超过 A
        lo_min = max(0, rv_vals[0] - num_wild)
        lo_max = RANK_VALUE["A"] - n + 1
        for lo in range(lo_min, lo_max + 1):
            hi = lo + n - 1
            if hi > RANK_VALUE["A"]:
                break
            expected = set(range(lo, hi + 1))
            real_set = set(rv_vals)
            if real_set <= expected and len(expected - real_set) == num_wild:
                return {"type": STRAIGHT, "value": lo,
                        "length": n, "cards": cs, "level": 0}
        return None

    def try_straight_pair():
        """含赖子的连对：枚举 lo，实牌须全成对，缺的对由赖子补"""
        if len(real_cards) < 2:
            return None
        if any(v >= KING_VAL for v in rv_vals):
            return None
        # rv_vals 须交替（相邻值不同），且每段 count=2
        if not all(rv_counter[v] == 2 for v in rv_vals):
            return None
        # pair_ranks = rv_vals[::2]
        pair_ranks = rv_vals[::2]
        g = gaps_of(pair_ranks)
        if g > num_wild:
            return None
        remaining = num_wild - g
        for left in range(remaining + 1):
            right = remaining - left
            lo = pair_ranks[0] - left
            hi = pair_ranks[-1] + right
            if lo < 1:
                continue
            expected_pairs = hi - lo + 1
            if expected_pairs * 2 != n:
                continue
            span = set(range(lo, hi + 1))
            if set(pair_ranks) <= span:
                return {"type": STRAIGHT_PAIR, "value": lo,
                        "length": expected_pairs, "cards": cs, "level": 0}
        return None

    # ==================== 无赖子 ====================
    if num_wild == 0:
        vals = [c["v"] for c in cs]
        # 王炸
        if n == 2 and vals[0] >= KING_VAL and vals[1] >= KING_VAL:
            return {"type": BOMB_JOKER, "value": vals[1],
                    "length": 1, "cards": cs, "level": BOMB_LEVEL[BOMB_JOKER]}
        # 单牌
        if n == 1:
            return {"type": SINGLE, "value": vals[0], "length": 1,
                    "cards": cs, "level": 0}
        # 对子
        if n == 2 and vals[0] == vals[1] and vals[0] < KING_VAL:
            return {"type": PAIR, "value": vals[0], "length": 1,
                    "cards": cs, "level": 0}
        # 三张
        if n == 3 and vals[0] == vals[2] and vals[0] < KING_VAL:
            return {"type": BOMB_3, "value": vals[0], "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_3]}
        # 三张
        if n == 3:
            if _consecutive(vals) and vals[-1] <= RANK_VALUE["A"]:
                return {"type": STRAIGHT, "value": vals[0], "length": 3,
                        "cards": cs, "level": 0}
            return None
        # 四张
        if n == 4:
            if vals[0] == vals[3] and vals[0] < KING_VAL:
                return {"type": BOMB_4, "value": vals[0], "length": 1,
                        "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
            if _consecutive_pairs(vals) and vals[-1] <= RANK_VALUE["A"]:
                return {"type": STRAIGHT_PAIR, "value": vals[0],
                        "length": 2, "cards": cs, "level": 0}
            if _consecutive(vals) and vals[-1] <= RANK_VALUE["A"]:
                return {"type": STRAIGHT, "value": vals[0], "length": 4,
                        "cards": cs, "level": 0}
            return None
        # 五张及以上：先检测顺子/连对，再检测四头炸弹
        if n >= 5:
            if _consecutive_pairs(vals) and vals[-1] <= RANK_VALUE["A"]:
                return {"type": STRAIGHT_PAIR, "value": vals[0],
                        "length": n // 2, "cards": cs, "level": 0}
            if _consecutive(vals) and vals[-1] <= RANK_VALUE["A"]:
                return {"type": STRAIGHT, "value": vals[0], "length": n,
                        "cards": cs, "level": 0}
            mc_v, mc_c = vals[0], 1
            for v in vals[1:]:
                if v == mc_v:
                    mc_c += 1
                elif mc_c == 1:
                    mc_v, mc_c = v, 1
            if mc_c == n and mc_v < KING_VAL:
                return {"type": BOMB_4, "value": mc_v, "length": 1,
                        "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
        return None

    # ==================== 含赖子 ====================
    # 赖子不能单出
    if n == 1:
        return None

    # 赖子不能与王组合
    if any(c["v"] >= KING_VAL for c in real_cards):
        return None

    # 炸弹（含赖子）：先检测（优先于顺子/对子）
    if len(rv_counter) == 1:
        top_v, top_c = rv_top()
        if top_v < KING_VAL:
            if top_c + num_wild == 3:
                return {"type": BOMB_3, "value": top_v, "length": 1,
                        "cards": cs, "level": BOMB_LEVEL[BOMB_3]}
            if top_c == 3 and num_wild == 1:
                return {"type": BOMB_4, "value": top_v, "length": 1,
                        "cards": cs, "level": BOMB_LEVEL[BOMB_4]}

    # 对子（2张含赖子）
    if n == 2 and num_wild == 1 and len(real_cards) == 1:
        return {"type": PAIR, "value": real_cards[0]["v"],
                "length": 1, "cards": cs, "level": 0}

    # 顺子（含赖子）
    s = try_straight()
    if s:
        return s

    # 连对（含赖子）
    sp = try_straight_pair()
    if sp:
        return sp

    return None



def can_beat(prev: Optional[Dict[str, Any]], new: Dict[str, Any]) -> bool:
    """
    判断 new 能否压过 prev。
    - prev 为 None：先手随便出合法牌型。
    - 普通牌型（单张/对子/顺子/连对）：
      必须同类型、同长度、恰好大 1 级（2 和王除外）。
      · 单张：2 可以管 3~A 任意单张（除王外），王可以管任意单张（小王管 2，大王管小王）
      · 对子：对2可以管任意对子（王不能组对子，所以对2最大）
      · 顺子/连对：不含 2 和王，仍然只接受恰好大 1 级
    - 炸弹：无视大一级规则，可压所有普通牌；
      优先级：普通炸弹 < 双王炸弹 < 氢弹（氢弹可压双王，双王可压普通炸弹）
    - 含赖子的牌型：赖子所代表的 value 已在 classify 中体现，直接按上述规则比较即可。
    """
    if prev is None:
        return True

    # 新出的牌是炸弹
    if new["type"] in BOMB_TYPES:
        if prev["type"] in NORMAL_TYPES:
            return True                          # 炸弹压普通牌
        if new["type"] == prev["type"]:
            return new["value"] > prev["value"]  # 同类炸弹：数值大者胜
        return BOMB_LEVEL[new["type"]] > BOMB_LEVEL[prev["type"]]

    # 新出的牌是普通牌型
    if prev["type"] in BOMB_TYPES:
        return False
    if new["type"] != prev["type"]:
        return False
    if new["length"] != prev["length"]:
        return False

    # 单张
    if new["type"] == SINGLE:
        new_v, prev_v = new["value"], prev["value"]
        if new_v >= RANK_VALUE["小王"]:
            if prev_v >= RANK_VALUE["小王"]:
                return new_v > prev_v
            return True
        if new_v == RANK_VALUE["2"]:
            return prev_v <= RANK_VALUE["A"]
        if prev_v >= RANK_VALUE["2"]:
            return False
        return new_v == prev_v + 1

    # 对子
    if new["type"] == PAIR:
        new_v, prev_v = new["value"], prev["value"]
        if new_v == RANK_VALUE["2"]:
            return prev_v <= RANK_VALUE["A"]
        if prev_v >= RANK_VALUE["2"]:
            return False
        return new_v == prev_v + 1

    # 顺子 / 连对
    return new["value"] == prev["value"] + 1


def combo_label(combo: Dict[str, Any]) -> str:
    """牌型的中文描述，用于消息提示。"""
    t = combo["type"]
    base = COMBO_CN.get(t, "")
    if t == BOMB_JOKER:
        return base
    if t in (SINGLE, PAIR, BOMB_3, BOMB_4):
        r = CARD_ORDER[combo["value"]]
        has_laizi = any(is_wild(c) for c in combo["cards"])
        suffix = "（含赖子）" if has_laizi else ""
        return f"{base} {r}{suffix}"
    low = CARD_ORDER[combo["value"]]
    high = CARD_ORDER[combo["value"] + combo["length"] - 1]
    if t == STRAIGHT:
        return f"{base} {low}~{high}"
    if t == STRAIGHT_PAIR:
        return f"{base} {low}{low}~{high}{high}"
    return base


# ---------------------------------------------------------------------------
# 单小轮对局
# ---------------------------------------------------------------------------

class Round:
    """一副牌（一个小轮）的完整对局状态与流程。"""

    def __init__(self, round_no: int, first_seat: int,
                 deck: Optional[List[Dict[str, Any]]] = None,
                 shuffle: Optional[Callable[[List], None]] = None):
        self.round_no = round_no
        self.first_seat = first_seat          # 本轮首个出牌玩家（用于春天判定）
        if deck is None:
            deck = build_deck()
            (shuffle or random.shuffle)(deck)
        self.deck = deck
        self.hands: Dict[int, List[Dict[str, Any]]] = {0: [], 1: [], 2: []}
        self.pile: List[Dict[str, Any]] = []
        self._deal()

        self.current_seat = first_seat
        self.current_play: Optional[Dict[str, Any]] = None   # 当前桌面牌型
        self.current_play_seat: Optional[int] = None
        self.pass_seats: List[int] = []       # 上次出牌后连续 pass 的玩家
        self.has_played = {0: False, 1: False, 2: False}
        self.round_multipliers: List[Dict[str, Any]] = []    # 本局打出的炸弹
        self.winner: Optional[int] = None

    # ---- 发牌 / 补牌 ----

    def _deal(self):
        """发牌：每小轮先手6张、其余两家5张，剩余全为底牌。"""
        for seat in range(3):
            # 先手6张，其余两家5张（每轮相同）
            base = 6 if seat == self.first_seat else 5
            for _ in range(base):
                if self.deck:
                    self.hands[seat].append(self.deck.pop())
        self.pile = self.deck
        for s in range(3):
            self.hands[s] = sort_cards(self.hands[s])

    def _draw_all(self):
        """全体补 1 张底牌（底牌耗尽则不再补）。
        已出完牌（0张）的玩家不再补牌，且应已判定为赢家。"""
        if not self.pile:
            return
        for seat in range(3):
            # 手牌已空（赢了）的玩家跳过补牌
            if self.hands[seat]:
                if self.pile:
                    self.hands[seat].append(self.pile.pop())
        for s in range(3):
            self.hands[s] = sort_cards(self.hands[s])

    # ---- 手牌操作 ----

    def _in_hand(self, seat: int, cards: List[Dict]) -> bool:
        hand = list(self.hands[seat])
        for c in cards:
            for i, hc in enumerate(hand):
                if hc["r"] == c["r"] and hc["s"] == c["s"]:
                    hand.pop(i)
                    break
            else:
                return False
        return True

    def _remove(self, seat: int, cards: List[Dict]):
        for c in cards:
            for i, hc in enumerate(self.hands[seat]):
                if hc["r"] == c["r"] and hc["s"] == c["s"]:
                    self.hands[seat].pop(i)
                    break

    # ---- 玩家操作 ----

    def play(self, seat: int, cards: List[Dict]) -> tuple:
        """出牌。返回 (是否成功, 错误信息或None)。"""
        if self.winner is not None:
            return False, "本轮已结束"
        if seat != self.current_seat:
            return False, "还没轮到你出牌"
        if not self._in_hand(seat, cards):
            return False, "你的手牌里没有这些牌"
        combo = classify(cards)
        if combo is None:
            return False, "这不是合法的牌型"
        if not can_beat(self.current_play, combo):
            return False, "必须恰好大一级，或打出炸弹"

        self._remove(seat, cards)
        self.current_play = combo
        self.current_play_seat = seat
        self.pass_seats = []
        self.has_played[seat] = True
        if combo["type"] in BOMB_TYPES:
            self.round_multipliers.append({"kind": combo["type"], "seat": seat})

        if not self.hands[seat]:
            self.winner = seat                 # 率先出完手牌 = 本轮赢家
            return True, None

        self.current_seat = (seat + 1) % 3
        return True, None

    def pass_(self, seat: int) -> tuple:
        """过牌。返回 (是否成功, 错误信息或None)。"""
        if self.winner is not None:
            return False, "本轮已结束"
        if seat != self.current_seat:
            return False, "还没轮到你出牌"
        if self.current_play is None:
            return False, "你是本轮先手，必须出牌"

        self.pass_seats.append(seat)
        if len(self.pass_seats) >= 2:          # 另外两人全部pass → 出牌者重新先手
            self._draw_all()                   # 三人各补1张
            self.current_seat = self.current_play_seat
            self.current_play = None
            self.current_play_seat = None
            self.pass_seats = []
            # 补牌后检测：任何玩家手牌为0且未设winner → 判定胜利
            self._auto_detect_winner()
        else:
            self.current_seat = (seat + 1) % 3
        return True, None

    def _auto_detect_winner(self):
        """兜底：任何玩家手牌为0且未设winner → 判定该玩家赢。"""
        if self.winner is not None:
            return
        for s in range(3):
            if not self.hands[s]:
                self.winner = s
                return

    # ---- 结算 ----

    def settle(self) -> Dict[str, Any]:
        """结算本轮，返回结果字典（含扣分明细与分数变动）。"""
        from scoring import settle_round
        return settle_round(self.winner, self.hands, self.round_multipliers,
                            self.has_played, round_no=self.round_no)


# ---------------------------------------------------------------------------
# 大局管理（10 小轮 = 1 大局）
# ---------------------------------------------------------------------------

class GameSession:
    """管理 10 小轮完整大局的进程、累计总分与全局结算。"""

    def __init__(self, total_rounds: int = 3,
                 shuffle: Optional[Callable[[List], None]] = None):
        self.total_rounds = total_rounds
        self.round_no = 0
        self.scores = {0: 0, 1: 0, 2: 0}
        self.shuffle = shuffle
        self.round: Optional[Round] = None
        self.history: List[Dict[str, Any]] = []
        self.final_ranking: Optional[List[Dict[str, Any]]] = None

    @property
    def is_over(self) -> bool:
        """大局是否结束：已打完 total_rounds 轮（当前轮已结算）。
        注意：round_no 在 start_round 时先 +1，所以第10局进行中 round_no==10，
        但此时当前轮还没结算，不算结束。只有第10局打完（round 已结算）才算。"""
        if self.round_no < self.total_rounds:
            return False
        # 已打完最后一轮且已结算（round 已结算后 history 含当前轮）
        return self.round is not None and len(self.history) >= self.round_no

    def start_round(self, first_seat: int) -> Round:
        """开始新一小轮。"""
        if self.round_no >= self.total_rounds:
            raise RuntimeError("大局已结束，请开启新一局")
        self.round_no += 1
        self.round = Round(self.round_no, first_seat, shuffle=self.shuffle)
        return self.round

    def finish_round(self) -> Dict[str, Any]:
        """结算当前小轮，累计进总分。"""
        result = self.round.settle()
        for s, d in result["deltas"].items():
            self.scores[s] += d
        self.history.append(result)
        return result

    def next_first_seat(self) -> int:
        """下一小轮先手 = 上一轮赢家。"""
        return self.history[-1]["winner_seat"]

    def compute_ranking(self) -> List[Dict[str, Any]]:
        """全局结算排名（按总分从高到低）。"""
        order = sorted(range(3), key=lambda s: self.scores[s], reverse=True)
        return [{"seat": s, "score": self.scores[s]} for s in order]

    def reset(self):
        """清空所有积分，开启全新 10 轮。"""
        self.round_no = 0
        self.scores = {0: 0, 1: 0, 2: 0}
        self.history = []
        self.round = None
        self.final_ranking = None
