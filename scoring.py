# -*- coding: utf-8 -*-
"""
三人行 · 记分结算模块
=====================
严格按用户规则：
- 基础底分：剩余手牌张数 × 2
- 倍率（全部乘法叠加，无上限）：
    普通炸弹 ×2 / 氢弹·双王炸弹 ×4 / 春天 ×2
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


def spring_multiplier(is_spring: bool) -> int:
    """春天 ×2。"""
    return 2 if is_spring else 1


def settle_round(winner: int,
                 hands: Dict[int, List[Any]],
                 bomb_records: List[Dict[str, Any]],
                 is_spring: bool,
                 round_no: int) -> Dict[str, Any]:
    """
    结算单小轮。

    返回：
    {
      "round_no": int,
      "winner_seat": int,
      "spring": bool,
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
    spring_mult = spring_multiplier(is_spring)

    losers = [s for s in range(3) if s != winner]
    total_gain = 0
    for s in losers:
        remaining = len(hands[s])
        base = base_score(remaining)
        loss = base * bomb_mult * spring_mult
        if loss > abs(MAX_LOSER_LOSS):
            capped[s] = True
            loss = abs(MAX_LOSER_LOSS)
        deltas[s] = -loss
        details[s] = (f"{remaining}张×2={base}"
                      f"{('×2' if bomb_mult > 1 else '')}"
                      f"{('×2春天' if is_spring else '')}"
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
        "spring": is_spring,
        "bombs": bomb_records,
        "remaining": {s: len(hands[s]) for s in range(3)},
        "deltas": deltas,
        "details": details,
        "capped": capped,
    }
