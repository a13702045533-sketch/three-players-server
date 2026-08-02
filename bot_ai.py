# -*- coding: utf-8 -*-
"""
三人行 · 高手 AI（Bot）决策引擎
================================
输入：手牌、桌面牌型 → 输出：要出的牌（列表）或 None（过牌）。

策略（高手 AI）：
1. 能压就压：优先找能压住桌面的"最小可压牌型"（省牌、跟牌快）
2. 先手：优先出顺子/连对（大牌型清牌快），其次对子，最后最小单张
3. 炸弹：平时保留，关键时刻（对方快出完 或 自己牌很少）才用
4. 手牌 ≤2 时果断压（尽快出完）
"""
import itertools
from typing import Any, Dict, List, Optional

import game_logic as gl


def _hand_to_gl(hand: List[Dict]) -> List[Dict]:
    """把客户端格式手牌转成 game_logic 可用的牌（带 v 值）。"""
    out = []
    for c in hand:
        if c["s"] == gl.JOKER_SUIT:
            out.append({"r": c["r"], "s": c["s"], "v": gl.RANK_VALUE[c["r"]]})
        else:
            out.append({"r": c["r"], "s": c["s"], "v": gl.RANK_VALUE[c["r"]]})
    return out


def _to_prev(play: Optional[Dict]) -> Optional[Dict]:
    """把桌面 current_play 转成 can_beat 需要的 prev。"""
    if play is None:
        return None
    return {
        "type": play["type"],
        "value": play["value"],
        "length": play["length"],
        "level": 0,
        "cards": [],
    }


def _enumerate_beats(hand: List[Dict], prev: Optional[Dict],
                     max_size: int = 6):
    """
    枚举所有能压过 prev 的出牌组合。
    返回 [(combo_cards, combo), ...]，按"牌张数少、点数小"粗略排序。
    """
    n = len(hand)
    results = []
    for size in range(1, min(n, max_size) + 1):
        for idxs in itertools.combinations(range(n), size):
            combo_cards = [hand[i] for i in idxs]
            combo = gl.classify(combo_cards)
            if combo and gl.can_beat(prev, combo):
                results.append((combo_cards, combo))
    return results


def _score_combo(combo: Dict, hand_size: int, opp_min_hand: int) -> int:
    """
    给一个可出的牌型打分（越低越优先）。
    - 炸弹类（要省）默认高分（不优先）
    - 单张/对子 低分（容易出）
    - 顺子/连对 中低分（清牌多）
    - 手牌越少，越倾向出能压的（压低分）
    """
    t = combo["type"]
    score = 100
    if t in (gl.BOMB_3, gl.BOMB_4, gl.BOMB_JOKER):
        # 炸弹：默认保留，除非关键时刻
        if opp_min_hand <= 2 or hand_size <= 2:
            score = 30
        else:
            score = 500
    elif t == gl.SINGLE:
        score = 10 + combo["value"]
    elif t == gl.PAIR:
        score = 20 + combo["value"]
    elif t == gl.STRAIGHT:
        score = 25 + combo["length"]
    elif t == gl.STRAIGHT_PAIR:
        score = 30 + combo["length"]
    return score


def decide_move(hand: List[Dict], current_play: Optional[Dict],
                opp_hands: Optional[List[int]] = None,
                my_seat: int = 0) -> Optional[List[Dict]]:
    """
    高手 AI 决策。
    返回：要出的牌列表（game_logic 格式），或 None（过牌）。

    hand: 自己的手牌（客户端格式，含 r/s/v）
    current_play: 桌面牌型（None 表示先手）
    opp_hands: 对手手牌张数列表（用于判断是否用炸弹）
    """
    gl_hand = _hand_to_gl(hand)
    if not gl_hand:
        return None

    prev = _to_prev(current_play)
    opp_min = min(opp_hands) if opp_hands else 99

    # ---- 先手：无桌面牌，自由出牌 ----
    if prev is None:
        return _decide_first(gl_hand)

    # ---- 有桌面牌：找能压的 ----
    candidates = _enumerate_beats(gl_hand, prev)
    if not candidates:
        return None   # 压不了，过牌

    # 打分选最优
    best_cards, best_combo, best_score = None, None, float("inf")
    for combo_cards, combo in candidates:
        s = _score_combo(combo, len(gl_hand), opp_min)
        if s < best_score:
            best_score = s
            best_cards = combo_cards
            best_combo = combo

    # 炸弹策略：非关键时刻即使是最优也权衡——如果唯一可压是炸弹且不该用，过牌
    if best_combo and best_combo["type"] in (gl.BOMB_3, gl.BOMB_4, gl.BOMB_JOKER):
        if opp_min > 2 and len(gl_hand) > 3:
            # 保留炸弹：除非手牌很少快出完
            return None

    return best_cards


def _decide_first(hand: List[Dict]) -> Optional[List[Dict]]:
    """先手策略：优先大牌型，其次对子，最后最小单张。"""
    # 找最长的顺子（优先出）
    # 简单实现：从所有顺子组合里找最长的
    best = None
    best_size = 0
    n = len(hand)
    for size in range(min(n, 6), 2, -1):   # 从长到短
        for idxs in itertools.combinations(range(n), size):
            combo_cards = [hand[i] for i in idxs]
            combo = gl.classify(combo_cards)
            if combo and combo["type"] == gl.STRAIGHT:
                best = combo_cards
                best_size = size
                break
        if best:
            return best

    # 找连对
    for size in range(min(n // 2, 4), 1, -1):
        # 枚举 size 对
        for idxs in itertools.combinations(range(n), size * 2):
            combo_cards = [hand[i] for i in idxs]
            combo = gl.classify(combo_cards)
            if combo and combo["type"] == gl.STRAIGHT_PAIR:
                return combo_cards

    # 找最小对子
    hand_sorted = sorted(hand, key=lambda c: (c["v"], c["s"]))
    for i in range(len(hand_sorted) - 1):
        if hand_sorted[i]["v"] == hand_sorted[i + 1]["v"]:
            return [hand_sorted[i], hand_sorted[i + 1]]

    # 最小单张（或炸弹兜底）
    return [hand_sorted[0]]
