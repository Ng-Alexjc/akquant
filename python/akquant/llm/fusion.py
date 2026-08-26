"""Deterministic constrained fusion and short-swing trading plans."""

from __future__ import annotations

import math
from typing import Any

from .config import FusionConfig
from .schemas import LLMTradeAnalysis

ACTION_ORDER = [
    "观察",
    "等待买入",
    "买入",
    "加仓",
    "持有",
    "减仓",
    "卖出",
    "止损",
    "清仓",
]


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _round_lot(account_equity: float, weight: float, price: float) -> int:
    if price <= 0 or account_equity <= 0 or weight <= 0:
        return 0
    return max(0, math.floor(account_equity * weight / price / 100) * 100)


def price_plan(technical: dict[str, Any], current_price: float) -> dict[str, Any]:
    atr = float(technical.get("atr14") or 0)
    ma5 = float(technical.get("ma5") or 0)
    ma20 = float(technical.get("ma20") or 0)
    support = float(technical.get("support_price") or 0)
    resistance = float(technical.get("resistance_price") or 0)
    anchors = [
        value
        for value in (ma5, ma20, support)
        if value > 0 and value <= current_price * 1.03
    ]
    anchor = (
        min(anchors, key=lambda value: abs(value - current_price)) if anchors else 0.0
    )
    if atr <= 0 or anchor <= 0 or resistance <= 0:
        return {"status": "insufficient_data", "version": "short_swing_price_v1"}
    lower = anchor - min(0.35 * atr, anchor * 0.012)
    upper = anchor + min(0.25 * atr, anchor * 0.008)
    breakout_confirm = resistance + max(0.10 * atr, 0.01)
    hard_stop = current_price * 0.92
    structural_stop = support - max(0.25 * atr, support * 0.005)
    stop = (
        max(hard_stop, structural_stop)
        if structural_stop < current_price
        else hard_stop
    )
    return {
        "status": "valid",
        "version": "short_swing_price_v1",
        "buy": {
            "mode": "pullback_support",
            "anchor_price": round(anchor, 3),
            "lower": round(max(0.01, lower), 3),
            "upper": round(upper, 3),
            "allowed_deviation": round(upper - anchor, 3),
            "max_chase_price": round(
                breakout_confirm + min(0.50 * atr, breakout_confirm * 0.015), 3
            ),
        },
        "sell": {
            "anchor_price": round(resistance, 3),
            "lower": round(resistance - min(0.30 * atr, resistance * 0.01), 3),
            "upper": round(resistance + min(0.30 * atr, resistance * 0.01), 3),
        },
        "stop_loss": {"trigger_price": round(max(0.01, stop), 3), "exit_ratio": 1.0},
    }


def _traditional_action(traditional: dict[str, Any], holding: bool) -> str | None:
    if not traditional.get("available", True):
        return None
    score = float(traditional.get("selection_score") or 0)
    probability = (
        (traditional.get("probabilities") or {}).get("next_trading_day") or {}
    ).get("value")
    if probability is None:
        return None
    price = float(traditional.get("close") or 0)
    ma20 = float(traditional.get("ma20") or 0)
    ma60 = float(traditional.get("ma60") or 0)
    momentum = float(traditional.get("momentum20") or 0)
    if holding:
        if price < ma60 and momentum < 0:
            return "清仓"
        if probability <= 0.40 and price < ma20:
            return "减仓"
        if score >= 75 and probability >= 0.60 and price > ma20 > ma60:
            return "加仓"
        return "持有"
    if score >= 72 and probability >= 0.58 and price > ma20 > ma60:
        return "买入"
    if score >= 65 and probability >= 0.54 and price > ma20:
        return "买入"
    return "等待买入" if score >= 60 and probability >= 0.50 else "观察"


def fuse(
    traditional: dict[str, Any],
    llm: LLMTradeAnalysis,
    config: FusionConfig,
    *,
    position: dict[str, Any] | None = None,
    account_equity: float = 100000.0,
    data_quality: float = 1.0,
) -> dict[str, Any]:
    holding = bool(position and float(position.get("quantity") or 0) > 0)
    traditional_action = _traditional_action(traditional, holding)
    current_price = float(traditional.get("close") or 0)
    entry_price = float((position or {}).get("entry_price") or 0)
    hard_stop_triggered = bool(
        holding
        and current_price > 0
        and entry_price > 0
        and current_price / entry_price - 1 <= -0.08
    )
    if hard_stop_triggered:
        traditional_action = "止损"
    valid_llm = llm.assessment_status in {
        "valid_directional",
        "valid_neutral",
        "degraded",
    }
    llm_weight = (
        min(
            config.initial_llm_max_weight,
            config.initial_llm_max_weight * llm.confidence * _clip(data_quality, 0, 1),
        )
        if valid_llm
        else 0.0
    )
    traditional_weight = 1.0 - llm_weight
    traditional_score = float(traditional.get("selection_score") or 0)
    if valid_llm and llm.score is not None:
        score_delta = _clip(
            (llm.score - traditional_score) * llm_weight,
            -config.llm_score_max_delta,
            config.llm_score_max_delta,
        )
    else:
        score_delta = 0.0
    final_score = _clip(traditional_score + score_delta, 0, 100)
    final_probabilities: dict[str, dict[str, Any]] = {}
    probability_deltas: dict[str, float | None] = {}
    for horizon in ("next_trading_day", "next_5_trading_days"):
        baseline = ((traditional.get("probabilities") or {}).get(horizon) or {}).get(
            "value"
        )
        subjective = (
            getattr(llm.subjective_up_probabilities, horizon) if valid_llm else None
        )
        if baseline is None:
            final_probabilities[horizon] = {
                "value": None,
                "source": "unavailable",
                "calibrated": False,
            }
            probability_deltas[horizon] = None
            continue
        delta = _clip(
            ((subjective - baseline) * llm_weight) if subjective is not None else 0.0,
            -config.probability_max_delta,
            config.probability_max_delta,
        )
        probability_deltas[horizon] = delta
        final_probabilities[horizon] = {
            "value": round(_clip(float(baseline) + delta, 0, 1), 6),
            "source": "rule_adjusted" if delta else "traditional_calibrated",
            "calibrated": False,
        }
    final_action = traditional_action
    action_step_delta = 0
    if (
        not hard_stop_triggered
        and valid_llm
        and traditional_action
        and llm.suggested_action in ACTION_ORDER
    ):
        base_index = ACTION_ORDER.index(traditional_action)
        llm_index = ACTION_ORDER.index(llm.suggested_action)
        action_step_delta = max(
            -config.max_action_step_change,
            min(config.max_action_step_change, llm_index - base_index),
        )
        final_action = ACTION_ORDER[
            max(0, min(len(ACTION_ORDER) - 1, base_index + action_step_delta))
        ]
    plan = price_plan(traditional, current_price)
    target_weight = (
        0.10
        if final_action in {"买入", "等待买入"}
        else (0.05 if final_action == "加仓" else 0.0)
    )
    quantity = _round_lot(account_equity, target_weight, current_price)
    held_quantity = int(float((position or {}).get("quantity") or 0))
    position_return = (
        current_price / float((position or {}).get("entry_price") or current_price or 1)
        - 1
        if holding
        else 0.0
    )
    sell_ratio = (
        0.25 if position_return > 0.08 else (0.50 if position_return >= 0 else 0.75)
    )
    if final_action in {"止损", "清仓"}:
        sell_ratio = 1.0
    plans = {
        "buy": {
            "enabled": final_action == "买入",
            "price_zone": plan.get("buy"),
            "position": {"target_weight": target_weight, "quantity": quantity},
        },
        "add": {
            "enabled": final_action == "加仓",
            "price_zone": plan.get("buy"),
            "position": {"target_weight": target_weight, "quantity": quantity},
        },
        "hold": {"enabled": final_action == "持有"},
        "reduce": {
            "enabled": final_action == "减仓",
            "position": {
                "target_exit_ratio": sell_ratio,
                "quantity": math.floor(held_quantity * sell_ratio / 100) * 100,
            },
        },
        "sell": {
            "enabled": final_action == "卖出",
            "price_zone": plan.get("sell"),
            "position": {
                "target_exit_ratio": sell_ratio,
                "quantity": math.floor(held_quantity * sell_ratio / 100) * 100,
            },
        },
        "stop_loss": {
            "enabled": holding,
            "price_zone": plan.get("stop_loss"),
            "position": {"target_exit_ratio": 1.0, "quantity": held_quantity},
        },
        "clear": {
            "enabled": holding,
            "trigger_conditions": ["持仓逻辑完全失效或重大风险触发"],
            "position": {"target_exit_ratio": 1.0, "quantity": held_quantity},
        },
    }
    conflict = "none"
    if valid_llm and traditional_action and llm.suggested_action:
        distance = abs(
            ACTION_ORDER.index(traditional_action)
            - ACTION_ORDER.index(llm.suggested_action)
        )
        conflict = "major" if distance >= 3 else ("minor" if distance else "none")
    # Keep risk as a first-class object instead of scattering it across plans.
    llm_risk = getattr(llm, "risk", None)
    risk_factors = list(getattr(llm_risk, "risk_factors", []) or [])
    invalidations = list(
        getattr(llm_risk, "invalidation_conditions", []) or llm.invalidation_conditions or []
    )
    if hard_stop_triggered:
        risk_status, risk_level = "valid", "critical"
        risk_factors = ["持仓相对成本跌幅达到 8% 硬止损"] + risk_factors
    elif not valid_llm and llm.assessment_status == "unavailable":
        risk_status, risk_level = "unavailable", "unknown"
    else:
        risk_status = "valid"
        risk_level = "high" if conflict in {"major", "critical"} else (
            getattr(llm_risk, "risk_level", "unknown") if llm_risk else "unknown"
        )
    risk = {
        "risk_status": risk_status,
        "risk_level": risk_level,
        "risk_factors": risk_factors,
        "invalidation_conditions": invalidations,
        "stop_loss_enabled": bool(holding),
        "stop_loss_price": (plan.get("stop_loss") or {}).get("trigger_price"),
        "stop_loss_exit_ratio": 1.0,
        "clear_enabled": bool(holding),
        "clear_exit_ratio": 1.0,
        "human_review_required": bool(llm.requires_human_review or conflict in {"major", "critical"}),
    }
    return {
        "mode": "rule_adjustment",
        "assessment_status": traditional.get("assessment_status", "unavailable"),
        "final_score": round(final_score, 1),
        "final_up_probabilities": final_probabilities,
        "weights": {
            "traditional": round(traditional_weight, 4),
            "llm": round(llm_weight, 4),
        },
        "adjustments": {
            "score_delta": round(score_delta, 3),
            "probability_deltas": probability_deltas,
            "action_step_delta": action_step_delta,
        },
        "final_confidence": round(
            (0.6 * traditional_weight) + (llm.confidence * llm_weight), 4
        ),
        "final_trend_direction": llm.trend_direction
        if valid_llm
        else traditional.get("trend_direction", "未知"),
        "final_action": final_action,
        "conflict_level": conflict,
        "human_review_required": bool(llm.requires_human_review or conflict == "major"),
        "vetoes": ["8% 硬止损触发，禁止 LLM 下调退出动作"]
        if hard_stop_triggered
        else [],
        "summary": llm.operation_advice
        if valid_llm
        else "LLM 不可用，采用传统基线与本地硬风控。",
        "price_plan": plan,
        "plans": plans,
        "risk": risk,
    }
