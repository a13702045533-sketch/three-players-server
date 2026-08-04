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
    """构建完整 54 张扑克牌。"""
    deck: List[Dict[str, Any]] = []
    for rank in CARD_ORDER[:13]:                # 3 ~ 2 各4张
        v = RANK_VALUE[rank]
        for s in SUITS:
            deck.append({"r": rank, "s": s, "v": v})
    deck.append({"r": "小王", "s": JOKER_SUIT, "v": RANK_VALUE["小王"]})
    deck.append({"r": "大王", "s": JOKER_SUIT, "v": RANK_VALUE["大王"]})
    deck.append({"r": "赖子", "s": "LZ", "v": RANK_VALUE["赖子"]})  # 1张赖子
    deck.append({"r": "赖子", "s": "LZ2", "v": RANK_VALUE["赖子"]})  # 第2张赖子
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


def classify(cards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    识别一组牌是什么牌型（含赖子万能牌）。
    赖子规则：
    - 必须与至少一张非赖子牌组合使用（赖子不能单出）
    - 赖子作为单张：可当任意非王单牌，value=所当牌的rank
    - 赖子+对子牌：视为该对（如赖子+3♠+3♥=对3）
    - 2张同rank非王+赖子：视为炸弹（如赖子+3♠+3♥=炸弹3）
    - 赖子不能组成顺子/连对（顺子不含王）
    - 赖子不能替代王，赖子+大王/小王≠王炸
    返回牌型字典或 None（非法牌型）。
    """
    if not cards:
        return None
    cs = sort_cards(cards)
    n = len(cs)

    # ---- 统计各成分数量 ----
    rank_vals = [c["v"] for c in cs]
    ranks_set = {c["r"] for c in cs}
    num_wild = sum(1 for c in cs if is_wild(c))
    real_cards = [c for c in cs if not is_wild(c)]

    # 纯赖子 = 非法（赖子不能单出）
    if num_wild == n:
        return None

    # ---- 赖子必须在 1~2 张以内（牌数限制） ----
    if num_wild > 2:
        return None

    def real_vals():
        """非赖子牌的 rank values（升序去重）。"""
        seen = []
        for v in sorted(c["v"] for c in real_cards):
            if not seen or v != seen[-1]:
                seen.append(v)
        return seen

    def is_king_val(v: int) -> bool:
        return v >= RANK_VALUE["小王"]

    def is_suit_ok(v: int) -> bool:
        """该 value 是否可用于组成不含王的牌型。"""
        return v < RANK_VALUE["小王"]   # ≤A(12) 才算普通值

    # ==================== 单牌 ====================
    if n == 1:
        # 赖子单出 = 合法万能单牌；普通牌正常处理
        return {"type": SINGLE, "value": cs[0]["v"], "length": 1,
                "cards": cs, "level": 0}


    # ==================== 两张 ====================
    if n == 2:
        if num_wild == 2:                # 赖子+赖子 = 非法
            return None
        # 赖子 + 一张普通牌 → 对子（普通牌 rank）
        if num_wild == 1:
            real = real_cards[0]
            if is_king_val(real["v"]):   # 赖子+王 = 非法
                return None
            return {"type": PAIR, "value": real["v"], "length": 1,
                    "cards": cs, "level": 0}
        # 双王炸弹
        if ranks_set == {"小王", "大王"}:
            return {"type": BOMB_JOKER, "value": 99, "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_JOKER]}
        # 赖子==0：普通对子
        if rank_vals[0] == rank_vals[1]:
            return {"type": PAIR, "value": rank_vals[0], "length": 1,
                    "cards": cs, "level": 0}
        return None

    # ==================== 三张 ====================
    if n == 3:
        # ---- 基于 real_cards 统计（非王 rank） ----
        from collections import Counter
        real_v_counts = Counter(c["v"] for c in real_cards if c["v"] < RANK_VALUE["小王"])

        if num_wild == 2:
            # 赖子+赖子+1张普通牌 → 对子（普通牌 rank）
            if not real_cards:
                return None
            real = real_cards[0]
            if is_king_val(real["v"]):
                return None
            return {"type": PAIR, "value": real["v"], "length": 1,
                    "cards": cs, "level": 0}
        if num_wild == 1:
            # 1赖子 + 2非王：看 real_v_counts
            if len(real_v_counts) == 1:
                # 2张同rank非王 + 赖子 → 炸弹3
                rv = list(real_v_counts.keys())[0]
                return {"type": BOMB_3, "value": rv, "length": 1,
                        "cards": cs, "level": BOMB_LEVEL[BOMB_3]}
            else:
                # 2张不同rank + 赖子 = 非法组合
                return None
        # 无赖子：普通炸弹
        if rank_vals[0] == rank_vals[2]:
            return {"type": BOMB_3, "value": rank_vals[0], "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_3]}
        # 无赖子：顺子（不含2、王）
        if rank_vals[-1] <= RANK_VALUE["A"] and _consecutive(rank_vals):
            return {"type": STRAIGHT, "value": rank_vals[0], "length": n,
                    "cards": cs, "level": 0}
        return None

    # ==================== 四张 ====================
    if n == 4:
        from collections import Counter
        real_v_counts = Counter(c["v"] for c in real_cards if c["v"] < RANK_VALUE["小王"])

        if num_wild >= 1:
            if not real_cards:
                return None
            if num_wild == 1:
                # 1赖子 + 3非王：必须恰好是3张同rank + 赖子 才算氢弹
                if len(real_v_counts) == 1:
                    # 3张同rank + 赖子 = 氢弹（4张炸弹）
                    rv = list(real_v_counts.keys())[0]
                    return {"type": BOMB_4, "value": rv, "length": 1,
                            "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
                elif len(real_v_counts) == 3:
                    # 1赖子+3单（3种不同rank）→ 顺子（如可填补）
                    sorted_vals = sorted(real_v_counts)
                    if _consecutive(sorted_vals):
                        return {"type": STRAIGHT, "value": sorted_vals[0], "length": n,
                                "cards": cs, "level": 0}
                return None
            elif num_wild == 2:
                # 2赖子 + 2非王
                if len(real_v_counts) == 1:
                    rv = list(real_v_counts.keys())[0]
                    return {"type": BOMB_4, "value": rv, "length": 1,
                            "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
                elif len(real_v_counts) == 2:
                    sorted_vals = sorted(real_v_counts)
                    if _consecutive(sorted_vals):
                        return {"type": STRAIGHT, "value": sorted_vals[0], "length": n,
                                "cards": cs, "level": 0}
                return None
        # 无赖子
        highest_ok = rank_vals[-1] <= RANK_VALUE["A"]   # 顺子/连对不含2、王
        if rank_vals[0] == rank_vals[3]:
            return {"type": BOMB_4, "value": rank_vals[0], "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
        if highest_ok and _consecutive(rank_vals):
            return {"type": STRAIGHT, "value": rank_vals[0], "length": n,
                    "cards": cs, "level": 0}
        if highest_ok and _consecutive_pairs(rank_vals):
            return {"type": STRAIGHT_PAIR, "value": rank_vals[0], "length": n // 2,
                    "cards": cs, "level": 0}
        return None

    # ==================== 五张及以上 ====================
    # 含赖子时：只处理顺子和连对（需要检查赖子能否填补空隙）
    if num_wild >= 1:
        rv = real_vals()
        if not rv:
            return None
        # 顺子：检查 rv 是否连续，且赖子能填满空隙
        if _consecutive(rv):
            # 顺子需要恰好 n 张，赖子只能填最多 gap=1 的小缝隙
            min_v, max_v = rv[0], rv[-1]
            expected_len = max_v - min_v + 1
            if len(rv) + num_wild < expected_len:
                pass  # 空隙太大，跳过
            else:
                # 刚好或超出，取 rv[0] 作为顺子起点（可能高估，但概率极低）
                if len(rv) + num_wild == expected_len:
                    # 完美匹配：rv 连续，赖子正好填满缝隙
                    if rv[0] > 0:
                        return {"type": STRAIGHT, "value": rv[0], "length": expected_len,
                                "cards": cs, "level": 0}
                elif len(rv) + num_wild == n and expected_len <= n:
                    # 可能的高牌型
                    if rv[0] > 0:
                        return {"type": STRAIGHT, "value": rv[0], "length": n,
                                "cards": cs, "level": 0}

        # 连对：3对及以上，检查
        if len(rv) >= 3 and len(rv) % 2 == 0:
            pairs_vals = rv[::2]
            if len(pairs_vals) >= 2 and _consecutive(pairs_vals):
                if num_wild >= 1:
                    return {"type": STRAIGHT_PAIR, "value": pairs_vals[0],
                            "length": len(pairs_vals),
                            "cards": cs, "level": 0}

        return None

    # 无赖子：标准判断
    has_joker = ("小王" in ranks_set or "大王" in ranks_set)
    highest_ok = rank_vals[-1] <= RANK_VALUE["A"]
    if _consecutive(rank_vals) and highest_ok:
        return {"type": STRAIGHT, "value": rank_vals[0], "length": n,
                "cards": cs, "level": 0}
    if _consecutive_pairs(rank_vals) and highest_ok:
        return {"type": STRAIGHT_PAIR, "value": rank_vals[0], "length": n // 2,
                "cards": cs, "level": 0}
    return None

    if n == 1:
        return {"type": SINGLE, "value": vals[0], "length": 1, "cards": cs, "level": 0}

    if n == 2:
        if ranks == {"小王", "大王"}:            # 双王炸弹
            return {"type": BOMB_JOKER, "value": 99, "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_JOKER]}
        if vals[0] == vals[1]:                    # 对子（王不能组对）
            return {"type": PAIR, "value": vals[0], "length": 1, "cards": cs, "level": 0}
        return None

    if n == 3:
        if vals[0] == vals[1] == vals[2]:         # 普通炸弹
            return {"type": BOMB_3, "value": vals[0], "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_3]}
        if not has_joker and _consecutive(vals) and highest_ok:
            return {"type": STRAIGHT, "value": vals[0], "length": n, "cards": cs, "level": 0}
        return None

    if n == 4:
        if vals[0] == vals[1] == vals[2] == vals[3]:   # 氢弹
            return {"type": BOMB_4, "value": vals[0], "length": 1,
                    "cards": cs, "level": BOMB_LEVEL[BOMB_4]}
        if not has_joker and _consecutive(vals) and highest_ok:
            return {"type": STRAIGHT, "value": vals[0], "length": n, "cards": cs, "level": 0}
        if not has_joker and _consecutive_pairs(vals) and highest_ok:
            return {"type": STRAIGHT_PAIR, "value": vals[0], "length": n // 2,
                    "cards": cs, "level": 0}
        return None

    # n >= 5
    if not has_joker and _consecutive(vals) and highest_ok:
        return {"type": STRAIGHT, "value": vals[0], "length": n, "cards": cs, "level": 0}
    if not has_joker and _consecutive_pairs(vals) and highest_ok:
        return {"type": STRAIGHT_PAIR, "value": vals[0], "length": n // 2,
                "cards": cs, "level": 0}
    return None


def can_beat(prev: Optional[Dict[str, Any]], new: Dict[str, Any]) -> bool:
    """
    判断 new 能否压过 prev。
    - prev 为 None：先手随便出合法牌型。
    - 普通牌型（单张/对子/顺子/连对）：
      必须同类型、同长度、恰好大 1 级（2 和王除外）。
      · 单张：2 可以管 3~A 任意单张（除王外），王可以管任意单张（小王管 2，大王管小王）
      · 赖子：单张可当任意非王单牌（含作为单张出）；组合中以所替代牌的价值计算
      · 对子：对2可以管任意对子（王不能组对子，所以对2最大）
      · 顺子/连对：不含 2 和王，仍然只接受恰好大 1 级
    - 炸弹：无视大一级规则，可压所有普通牌；
      优先级：普通炸弹 < 双王炸弹 < 氢弹（氢弹可压双王，双王可压普通炸弹）
    """
    if prev is None:
        return True

    # ---- 赖子特殊处理 ----
    new_has_laizi = any(is_wild(c) for c in new["cards"])
    prev_has_laizi = any(is_wild(c) for c in prev["cards"])
    if new_has_laizi and not prev_has_laizi:
        # 新牌含赖子，旧牌不含王：可以压（普通牌）
        if prev["type"] in BOMB_TYPES:
            return BOMB_LEVEL[new["type"]] > BOMB_LEVEL[prev["type"]] or \
                (new["type"] == prev["type"] and new["value"] > prev["value"])
        return True
    if not new_has_laizi and prev_has_laizi:
        # 旧牌含赖子，新牌不含赖子：必须是小王/大王才能管
        if new["type"] == SINGLE and new["value"] >= RANK_VALUE["小王"]:
            return new["value"] > prev["value"]
        return False
    if new_has_laizi and prev_has_laizi:
        # 两边都有赖子：比rank或类型
        if new["type"] == SINGLE and prev["type"] == SINGLE:
            return new["value"] > prev["value"]
        if new["type"] in BOMB_TYPES and prev["type"] in BOMB_TYPES:
            if new["type"] == prev["type"]:
                return new["value"] > prev["value"]
            return BOMB_LEVEL[new["type"]] > BOMB_LEVEL[prev["type"]]
        return True
    # 普通压制
        # 含赖子的组合（对子/炸弹）：只接受同类王炸或更大炸弹
        if prev["type"] in BOMB_TYPES:
            if prev["type"] == new["type"]:
                return new["value"] > prev["value"]
            return BOMB_LEVEL[new["type"]] > BOMB_LEVEL[prev["type"]]
        # 含赖子的非炸弹 vs 普通牌：赢
        if prev["type"] in NORMAL_TYPES:
            return True
        return False

    # ---- 双方无赖子：标准规则 ----

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

    def is_spring(self) -> bool:
        """春天：赢家是第一轮出牌者，且另外两人全程一张牌都没打过。"""
        if self.winner is None or self.winner != self.first_seat:
            return False
        return not any(self.has_played[s] for s in range(3) if s != self.winner)

    def settle(self) -> Dict[str, Any]:
        """结算本轮，返回结果字典（含扣分明细与分数变动）。"""
        from scoring import settle_round
        return settle_round(self.winner, self.hands, self.round_multipliers,
                            self.is_spring(), round_no=self.round_no)


# ---------------------------------------------------------------------------
# 大局管理（10 小轮 = 1 大局）
# ---------------------------------------------------------------------------

class GameSession:
    """管理 10 小轮完整大局的进程、累计总分与全局结算。"""

    def __init__(self, total_rounds: int = 10,
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
        return self.round_no >= self.total_rounds

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
