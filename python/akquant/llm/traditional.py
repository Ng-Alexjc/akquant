"""Leakage-aware traditional probability models for review-center signals."""

from __future__ import annotations

import math
import statistics
from typing import Any

MIN_BARS = 90
MAX_SAMPLES = 360
MIN_TRAIN_SAMPLES = 30
NEUTRAL_LOWER = 0.48
NEUTRAL_UPPER = 0.52
FEATURE_NAMES = [
    "return_1d",
    "return_5d",
    "return_20d",
    "ma5_over_ma20",
    "ma20_over_ma60",
    "volatility_10d",
    "rsi14_centered",
    "volume5_over_volume20",
]
SWING_SCORE_WEIGHTS = {
    "technical_score": 0.45,
    "next_trading_day_probability": 0.20,
    "next_5_trading_days_probability": 0.35,
}


def swing_composite_score(
    technical_score: float,
    next_day_probability: float | None,
    five_day_probability: float | None,
    next_day_validation: dict[str, Any] | None = None,
    five_day_validation: dict[str, Any] | None = None,
) -> float:
    """Blend technical quality with both probability horizons for swing trades.

    The five-day horizon receives the largest probability weight because the
    target style is a short trend swing rather than a one-day scalp. Missing
    horizons are excluded and the remaining weights are normalized, so an
    unavailable forecast does not silently become a bearish zero.
    """
    components: list[tuple[float, float]] = [
        (
            max(0.0, min(100.0, float(technical_score))),
            SWING_SCORE_WEIGHTS["technical_score"],
        )
    ]
    for probability, key, validation in (
        (
            next_day_probability,
            "next_trading_day_probability",
            next_day_validation,
        ),
        (
            five_day_probability,
            "next_5_trading_days_probability",
            five_day_validation,
        ),
    ):
        if probability is None:
            continue
        value = float(probability)
        if math.isfinite(value):
            components.append(
                (
                    max(0.0, min(1.0, value)) * 100.0,
                    SWING_SCORE_WEIGHTS[key]
                    * probability_reliability(validation),
                )
            )
    total_weight = sum(weight for _, weight in components)
    if total_weight <= 0:
        return round(max(0.0, min(100.0, float(technical_score))), 1)
    return round(
        sum(value * weight for value, weight in components) / total_weight,
        1,
    )


def probability_reliability(validation: dict[str, Any] | None) -> float:
    """Convert validation metrics into a conservative probability weight."""
    if not validation:
        return 0.5
    components: list[float] = []
    auc = validation.get("auc")
    if auc is not None:
        components.append(max(0.0, min(1.0, (float(auc) - 0.48) / 0.12)))
    brier = validation.get("brier_score")
    if brier is not None:
        components.append(max(0.0, min(1.0, (0.30 - float(brier)) / 0.08)))
    if not components:
        accuracy = validation.get("accuracy")
        if accuracy is not None:
            components.append(
                max(0.0, min(1.0, (float(accuracy) - 0.45) / 0.15))
            )
    return sum(components) / len(components) if components else 0.5


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _return(closes: list[float], end: int, lookback: int) -> float:
    start = end - lookback
    if start < 0 or closes[start] <= 0:
        return 0.0
    return closes[end] / closes[start] - 1.0


def _rsi(closes: list[float], end: int, period: int = 14) -> float:
    start = end - period
    if start < 0:
        return 50.0
    gains = 0.0
    losses = 0.0
    for index in range(start + 1, end + 1):
        change = closes[index] - closes[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses <= 1e-12:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100.0 - 100.0 / (1.0 + relative_strength)


def feature_row(
    closes: list[float], volumes: list[float], end: int
) -> list[float] | None:
    """Build the existing eight leakage-free price/volume features."""
    if end < 60 or closes[end] <= 0:
        return None
    daily_returns = [
        closes[index] / closes[index - 1] - 1.0
        for index in range(end - 9, end + 1)
        if closes[index - 1] > 0
    ]
    ma5 = _mean(closes[end - 4 : end + 1])
    ma20 = _mean(closes[end - 19 : end + 1])
    ma60 = _mean(closes[end - 59 : end + 1])
    volume5 = _mean(volumes[end - 4 : end + 1])
    volume20 = _mean(volumes[end - 19 : end + 1])
    return [
        _return(closes, end, 1),
        _return(closes, end, 5),
        _return(closes, end, 20),
        ma5 / ma20 - 1.0 if ma20 else 0.0,
        ma20 / ma60 - 1.0 if ma60 else 0.0,
        statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0.0,
        (_rsi(closes, end) - 50.0) / 50.0,
        volume5 / volume20 - 1.0 if volume20 else 0.0,
    ]


def _model() -> Any:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    class_weight="balanced", max_iter=500, random_state=0
                ),
            ),
        ]
    )


def _metric_or_none(function: Any, *args: Any, **kwargs: Any) -> float | None:
    try:
        value = float(function(*args, **kwargs))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _walk_forward_predictions(
    features: list[list[float]], labels: list[int]
) -> tuple[list[int], list[float]]:
    validation_size = min(60, max(20, len(features) // 5))
    start = max(MIN_TRAIN_SAMPLES, len(features) - validation_size)
    expected: list[int] = []
    predicted: list[float] = []
    for index in range(start, len(features)):
        train_labels = labels[:index]
        if len(set(train_labels)) < 2:
            continue
        model = _model()
        model.fit(features[:index], train_labels)
        probability = float(model.predict_proba([features[index]])[0][1])
        expected.append(labels[index])
        predicted.append(max(0.0, min(1.0, probability)))
    return expected, predicted


def _calibration_bins(
    labels: list[int], probabilities: list[float]
) -> list[dict[str, Any]]:
    if not labels:
        return []
    bins: list[dict[str, Any]] = []
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        upper = lower + 0.2
        indices = [
            index
            for index, probability in enumerate(probabilities)
            if lower <= probability < upper or (upper == 1.0 and probability == 1.0)
        ]
        if not indices:
            continue
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "count": len(indices),
                "mean_probability": _mean([probabilities[index] for index in indices]),
                "observed_rate": _mean([float(labels[index]) for index in indices]),
            }
        )
    return bins


def _fit_platt(
    labels: list[int], probabilities: list[float], final_probability: float
) -> tuple[float, dict[str, Any]]:
    if len(labels) < 40 or len(set(labels)) < 2:
        return final_probability, {"calibrated": False, "reason": "样本不足"}
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss

    split = max(20, int(len(labels) * 0.7))
    if split >= len(labels) or len(set(labels[:split])) < 2:
        return final_probability, {"calibrated": False, "reason": "校准标签不足"}
    train_x = np.asarray(probabilities[:split], dtype=float).reshape(-1, 1)
    train_y = np.asarray(labels[:split], dtype=int)
    evaluator = LogisticRegression(max_iter=300, random_state=0)
    evaluator.fit(train_x, train_y)
    eval_x = np.asarray(probabilities[split:], dtype=float).reshape(-1, 1)
    eval_y = np.asarray(labels[split:], dtype=int)
    calibrated_eval = evaluator.predict_proba(eval_x)[:, 1]
    calibrated_brier = _metric_or_none(brier_score_loss, eval_y, calibrated_eval)

    deploy = LogisticRegression(max_iter=300, random_state=0)
    deploy.fit(
        np.asarray(probabilities, dtype=float).reshape(-1, 1),
        np.asarray(labels, dtype=int),
    )
    calibrated_final = float(deploy.predict_proba([[final_probability]])[0][1])
    return max(0.0, min(1.0, calibrated_final)), {
        "calibrated": True,
        "method": "platt_on_walk_forward",
        "evaluation_sample_count": len(eval_y),
        "calibrated_brier_score": calibrated_brier,
    }


def predict_horizon(
    closes: list[float], volumes: list[float], *, horizon: int
) -> dict[str, Any]:
    """Predict one horizon and report walk-forward classification/calibration metrics."""
    horizon_name = "next_trading_day" if horizon == 1 else "next_5_trading_days"
    if len(closes) < MIN_BARS:
        return {
            "horizon": horizon_name,
            "value": None,
            "valid": False,
            "assessment_status": "insufficient_data",
            "unavailable_reason": f"至少需要 {MIN_BARS} 根日 K",
            "training_samples": 0,
            "validation": {},
        }
    features: list[list[float]] = []
    labels: list[int] = []
    for end in range(60, len(closes) - horizon):
        row = feature_row(closes, volumes, end)
        if row is None or not all(math.isfinite(value) for value in row):
            continue
        features.append(row)
        labels.append(1 if closes[end + horizon] > closes[end] else 0)
    if len(features) > MAX_SAMPLES:
        features = features[-MAX_SAMPLES:]
        labels = labels[-MAX_SAMPLES:]
    latest = feature_row(closes, volumes, len(closes) - 1)
    if latest is None or len(features) < MIN_TRAIN_SAMPLES:
        return {
            "horizon": horizon_name,
            "value": None,
            "valid": False,
            "assessment_status": "insufficient_data",
            "unavailable_reason": "有效训练样本不足",
            "training_samples": len(features),
            "validation": {},
        }
    if len(set(labels)) < 2:
        return {
            "horizon": horizon_name,
            "value": None,
            "valid": False,
            "assessment_status": "unavailable",
            "unavailable_reason": "训练标签只有一个类别",
            "training_samples": len(features),
            "validation": {},
        }
    try:
        from sklearn.metrics import (
            accuracy_score,
            brier_score_loss,
            precision_score,
            recall_score,
            roc_auc_score,
        )

        expected, predicted = _walk_forward_predictions(features, labels)
        final_model = _model()
        final_model.fit(features, labels)
        raw_probability = float(final_model.predict_proba([latest])[0][1])
        raw_probability = max(0.0, min(1.0, raw_probability))
        probability, calibration = _fit_platt(expected, predicted, raw_probability)
        predicted_labels = [1 if value >= 0.5 else 0 for value in predicted]
        validation = {
            "method": "expanding_walk_forward",
            "sample_count": len(expected),
            "positive_rate": _mean([float(value) for value in expected]),
            "accuracy": _metric_or_none(accuracy_score, expected, predicted_labels),
            "brier_score": _metric_or_none(brier_score_loss, expected, predicted),
            "auc": (
                _metric_or_none(roc_auc_score, expected, predicted)
                if len(set(expected)) >= 2
                else None
            ),
            "precision": _metric_or_none(
                precision_score, expected, predicted_labels, zero_division=0
            ),
            "recall": _metric_or_none(
                recall_score, expected, predicted_labels, zero_division=0
            ),
            "decision_threshold": 0.5,
            "calibration_bins": _calibration_bins(expected, predicted),
            **calibration,
        }
        status = (
            "valid_neutral"
            if NEUTRAL_LOWER <= probability <= NEUTRAL_UPPER
            else "valid_directional"
        )
        return {
            "horizon": horizon_name,
            "value": probability,
            "raw_value": raw_probability,
            "valid": True,
            "assessment_status": status,
            "unavailable_reason": None,
            "training_samples": len(features),
            "validation": validation,
        }
    except Exception as exc:  # noqa: BLE001 - explicit unavailable status is required
        return {
            "horizon": horizon_name,
            "value": None,
            "valid": False,
            "assessment_status": "unavailable",
            "unavailable_reason": f"传统模型训练失败: {exc}",
            "training_samples": len(features),
            "validation": {},
        }


def predict_all_horizons(closes: list[float], volumes: list[float]) -> dict[str, Any]:
    """Return independent next-day and five-day predictions."""
    next_day = predict_horizon(closes, volumes, horizon=1)
    next_five = predict_horizon(closes, volumes, horizon=5)
    statuses = {next_day["assessment_status"], next_five["assessment_status"]}
    if statuses == {"unavailable"}:
        overall = "unavailable"
    elif statuses <= {"insufficient_data", "unavailable"}:
        overall = "insufficient_data"
    elif any(not item["valid"] for item in (next_day, next_five)):
        overall = "degraded"
    elif statuses == {"valid_neutral"}:
        overall = "valid_neutral"
    else:
        overall = "valid_directional"
    return {
        "assessment_status": overall,
        "probabilities": {
            "next_trading_day": next_day,
            "next_5_trading_days": next_five,
        },
    }


def historical_probability_rows(
    closes: list[float],
    volumes: list[float],
    target_indices: list[int],
) -> dict[int, dict[str, Any]]:
    """Replay leakage-free point-in-time probabilities for historical closes.

    For a prediction made at bar ``t``, a training observation ending at bar
    ``e`` is eligible only when its forward label is already observable at
    ``t`` (``e + horizon <= t``).  This is intentionally lighter than the live
    evaluator: it fits one expanding model per requested date/horizon and lets
    the downstream candidate trainer perform the held-out evaluation.
    """
    if len(closes) != len(volumes):
        raise ValueError("closes 与 volumes 长度必须一致")
    valid_targets = sorted(
        {
            int(index)
            for index in target_indices
            if 60 <= int(index) < len(closes)
        }
    )
    if not valid_targets:
        return {}

    feature_cache = {
        index: feature_row(closes, volumes, index)
        for index in range(60, len(closes))
    }
    rows: dict[int, dict[str, Any]] = {index: {} for index in valid_targets}
    for horizon, horizon_name in (
        (1, "next_trading_day"),
        (5, "next_5_trading_days"),
    ):
        for target in valid_targets:
            latest = feature_cache.get(target)
            eligible_end = target - horizon
            pairs = [
                (feature_cache.get(end), 1 if closes[end + horizon] > closes[end] else 0)
                for end in range(60, eligible_end + 1)
                if feature_cache.get(end) is not None
            ]
            if len(pairs) > MAX_SAMPLES:
                pairs = pairs[-MAX_SAMPLES:]
            features = [list(item[0]) for item in pairs if item[0] is not None]
            labels = [int(item[1]) for item in pairs if item[0] is not None]
            if latest is None or len(features) < MIN_TRAIN_SAMPLES:
                rows[target][horizon_name] = {
                    "value": None,
                    "valid": False,
                    "assessment_status": "insufficient_data",
                    "training_samples": len(features),
                    "unavailable_reason": "历史时点有效训练样本不足",
                    "validation": {"method": "point_in_time_expanding_replay"},
                }
                continue
            if len(set(labels)) < 2:
                rows[target][horizon_name] = {
                    "value": None,
                    "valid": False,
                    "assessment_status": "unavailable",
                    "training_samples": len(features),
                    "unavailable_reason": "历史时点训练标签只有一个类别",
                    "validation": {"method": "point_in_time_expanding_replay"},
                }
                continue
            model = _model()
            model.fit(features, labels)
            probability = max(
                0.0,
                min(1.0, float(model.predict_proba([latest])[0][1])),
            )
            rows[target][horizon_name] = {
                "value": probability,
                "raw_value": probability,
                "valid": True,
                "assessment_status": (
                    "valid_neutral"
                    if NEUTRAL_LOWER <= probability <= NEUTRAL_UPPER
                    else "valid_directional"
                ),
                "training_samples": len(features),
                "unavailable_reason": None,
                "validation": {
                    "method": "point_in_time_expanding_replay",
                    "label_observation_rule": "feature_end + horizon <= prediction_bar",
                },
            }
    return rows
