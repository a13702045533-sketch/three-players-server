# -*- coding: utf-8 -*-
"""
三人行 · 记分结算模块
=====================
严格按用户规则：
- 基础底分：剩余手牌张数 × 2
- 倍率（全部乘法叠加，无上限）：
    普通炸弹 ×2 / 氢弹·双王炸弹 ×4 / 春天 ×2
- 春天：单个玩家全局一张牌都没打出 = 该玩家被春天，其本轮输分 ×2（额外部分加给赢家）
- 单轮封顶：赢家最高 +1600，单个输家最高 -800
- 赢家得分 = 两名输家扣除分数总和
"""
from typing import Any, Dict, List

MAX_WINNER_GAIN = 1600
MAX_LOSER_LOSS = -800


def base_score(remaining_cards: int) -> int:
    """基础底分 = 剩余手牌张数 × 2。"""
    return remaining_cards * 2


def round_multiplier(bomb_kinds: List[str]) -> int:
    """
    由本局打出的炸弹计算倍率（乘法叠加）。
    BOMB_3 = ×2；BOMB_4 / BOMB_JOKER = ×4。
    """
    m = 1
    for kind in bomb_kinds:
        if kind == "bomb3":
            m *= 2
        elif kind in ("bomb4", "bomb_joker"):
            m *= 4
    return m


def settle_round(winner: int,
                 hands: Dict[int, List[Any]],
                 bomb_records: List[Dict[str, Any]],
                 has_played: Dict[int, bool],
                 round_no: int) -> Dict[str, Any]:
    """
    结算单小轮。

    - 春天：某输家全程没出过牌（has_played[s] == False）= 该玩家被春天，
      其基础输分先 ×2（连同炸弹倍率），多出的部分计入赢家得分。

    返回：
    {
      "round_no": int,
      "winner_seat": int,
      "spring": bool,               # 是否存在春天
      "spring_seats": [被春天的座位...],
      "bombs": [{"kind","seat"}...],
      "remaining": {seat: 张数},
      "deltas": {seat: 分数变动(相对总分)},   # 赢家为正，输家为负
      "details": {seat: 扣分计算明细},
      "capped": {seat: 是否触发封顶}
    }
    """
    deltas = {0: 0, 1: 0, 2: 0}
    details: Dict[int, str] = {}
    capped = {0: False, 1: False, 2: False}

    bomb_kinds = [b["kind"] for b in bomb_records]
    bomb_mult = round_multiplier(bomb_kinds)

    # 春天：输家全程没出牌
    spring_seats = [s for s in range(3) if s != winner and not has_played.get(s, True)]

    losers = [s for s in range(3) if s != winner]
    total_gain = 0
    for s in losers:
        remaining = len(hands[s])
        base = base_score(remaining)
        loss = base * bomb_mult
        is_spring = s in spring_seats
        if is_spring:
            loss *= 2   # 被春天：输分 ×2
        if loss > abs(MAX_LOSER_LOSS):
            capped[s] = True
            loss = abs(MAX_LOSER_LOSS)
        deltas[s] = -loss
        spring_tag = "×2春天" if is_spring else ""
        details[s] = (f"{remaining}张×2={base}"
                      f"{('×2' if bomb_mult > 1 else '')}"
                      f"{spring_tag}"
                      f"→{loss}")
        total_gain += loss

    if total_gain > MAX_WINNER_GAIN:
        capped[winner] = True
        total_gain = MAX_WINNER_GAIN
    deltas[winner] = total_gain
    details[winner] = f"赢家：两名输家扣分总和{total_gain}"

    return {
        "round_no": round_no,
        "winner_seat": winner,
        "spring": bool(spring_seats),
        "spring_seats": spring_seats,
        "bombs": bomb_records,
        "remaining": {s: len(hands[s]) for s in range(3)},
        "deltas": deltas,
        "details": details,
        "capped": capped,
    }
