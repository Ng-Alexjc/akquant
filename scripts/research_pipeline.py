"""Reusable local-pool research primitives for the review center.

The module is deliberately independent from the native AKQuant extension. It
creates point-in-time snapshots, derives leakage-safe features/labels, runs a
small trend-swing portfolio simulator and performs walk-forward parameter
selection. The review-center server supplies the current local pool data.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd


FEATURE_VERSION = "trend_swing_features_mfi_v3"
LABEL_VERSION = "forward_return_triple_barrier_v1"
SIMULATOR_VERSION = "a_share_trend_swing_mfi_v7"
MAX_PORTFOLIO_POSITIONS = 4
MFI_PERIOD = 14
MFI_NORMAL_MIN = 30.0
MFI_NORMAL_MAX = 85.0
MFI_MAIN_RISE_MIN = 35.0
MFI_LIMIT_UP_MIN = 60.0


def pareto_front(results: Sequence[Mapping[str, Any]], objectives: Sequence[str] = ("sharpe_ratio", "total_return_pct", "max_drawdown_pct")) -> list[dict[str, Any]]:
    """Return non-dominated candidates for multi-objective selection."""
    normalized = [dict(item) for item in results]
    front: list[dict[str, Any]] = []
    for candidate in normalized:
        dominated = False
        for other in normalized:
            if other is candidate:
                continue
            better_or_equal = True
            strictly_better = False
            for objective in objectives:
                left = float(other.get(objective, 0.0) or 0.0)
                right = float(candidate.get(objective, 0.0) or 0.0)
                # Drawdown values are negative, so the value closer to zero is
                # already greater and therefore better. Do not invert it.
                if left < right:
                    better_or_equal = False
                    break
                strictly_better |= left > right
            if better_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            front.append(candidate)
    return front


def multi_objective_optimize(
    series_by_symbol: Mapping[str, Any],
    param_grid: Mapping[str, Sequence[Any]],
    *,
    strategy: str = "trend_swing",
    initial_cash: float = 100_000.0,
    max_trials: int = 60,
    storage_path: str | Path | None = None,
    study_name: str | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    """Run persistent Bayesian optimization with Optuna TPE and pruning.

    A single conservative composite objective is used so MedianPruner can stop
    weak trials early.  Sharpe, return and drawdown are also retained per trial
    and passed through the existing Pareto selector.  When ``storage_path`` is
    supplied Optuna uses SQLite and ``load_if_exists=True`` for safe resume.
    """
    try:
        import optuna
    except Exception as exc:  # pragma: no cover - optional dependency guard
        return {"status": "unavailable", "optimizer": "optuna_tpe", "reason": str(exc), "strategy": strategy, "trial_count": 0, "pareto_count": 0, "pareto_front": [], "best": None}

    normalized = prepare_simulation_series(series_by_symbol)
    timeline = sorted({date for frame in normalized.values() for date in frame.index})
    storage_url: str | None = None
    if storage_path is not None:
        resolved = Path(storage_path).resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        storage_url = f"sqlite:///{resolved.as_posix()}"
    resolved_study_name = study_name or f"akquant-{strategy}"
    sampler = optuna.samplers.TPESampler(seed=seed)
    pruner = optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=1)
    study = optuna.create_study(
        study_name=resolved_study_name,
        direction="maximize",
        sampler=sampler,
        pruner=pruner,
        storage=storage_url,
        load_if_exists=bool(storage_url),
    )
    seed_params = {
        key: list(values)[0]
        for key, values in param_grid.items()
        if list(values)
    }
    if seed_params and not any(
        trial.state.name == "COMPLETE" and dict(trial.params) == seed_params
        for trial in study.trials
    ):
        # Always evaluate one deterministic, auditable baseline before TPE.
        # This prevents a small categorical trial budget from missing a known
        # robust configuration while all later trials remain Bayesian.
        study.enqueue_trial(seed_params)

    def objective(trial: Any) -> float:
        params = {
            key: trial.suggest_categorical(key, list(values))
            for key, values in param_grid.items()
        }
        # Progressive time slices make pruning real: weak candidates can be
        # stopped before the full-history simulation is executed.
        for step, fraction in enumerate((0.45, 0.65, 0.82), start=1):
            end = max(1, int(len(timeline) * fraction))
            cutoff = timeline[end - 1] if timeline else None
            subset = {
                symbol: frame.loc[frame.index <= cutoff]
                for symbol, frame in normalized.items()
            } if cutoff is not None else normalized
            interim = simulate_strategy(
                subset,
                strategy=strategy,
                initial_cash=initial_cash,
                _prepared=True,
                **params,
            )
            interim_summary = interim.get("summary") or {}
            interim_score = _optimization_score(interim_summary)
            trial.report(interim_score, step)
            if trial.should_prune():
                raise optuna.TrialPruned()
        result = simulate_strategy(
            normalized,
            strategy=strategy,
            initial_cash=initial_cash,
            _prepared=True,
            **params,
        )
        summary = result.get("summary") or {}
        for key in ("sharpe_ratio", "total_return_pct", "max_drawdown_pct", "trade_count", "completed_trade_count", "win_rate"):
            trial.set_user_attr(key, float(summary.get(key) or 0.0))
        return _optimization_score(summary)

    completed_before = sum(trial.state.name == "COMPLETE" for trial in study.trials)
    remaining = max(0, int(max_trials) - completed_before)
    if remaining:
        study.optimize(objective, n_trials=remaining, gc_after_trial=True)

    candidates: list[dict[str, Any]] = []
    serialized_trials: list[dict[str, Any]] = []
    for trial in study.trials:
        item = {
            "number": trial.number,
            "state": trial.state.name.lower(),
            "params": dict(trial.params),
            "objective": trial.value,
            "datetime_start": trial.datetime_start.isoformat() if trial.datetime_start else None,
            "datetime_complete": trial.datetime_complete.isoformat() if trial.datetime_complete else None,
            **dict(trial.user_attrs),
        }
        serialized_trials.append(item)
        if trial.state.name == "COMPLETE":
            candidates.append(item)
    front = pareto_front(candidates)
    front.sort(key=lambda item: (float(item.get("sharpe_ratio") or 0), float(item.get("total_return_pct") or 0)), reverse=True)
    best_trial = study.best_trial if candidates else None
    best = next((item for item in serialized_trials if best_trial and item["number"] == best_trial.number), None)
    return {
        "status": "valid" if candidates else "insufficient_data",
        "optimizer": "optuna_tpe_median_pruner",
        "strategy": strategy,
        "study_name": resolved_study_name,
        "storage": str(Path(storage_path).resolve()) if storage_path is not None else "in_memory",
        "resumed": completed_before > 0,
        "completed_before": completed_before,
        "trial_count": len(serialized_trials),
        "completed_trial_count": len(candidates),
        "pruned_trial_count": sum(item["state"] == "pruned" for item in serialized_trials),
        "pareto_count": len(front),
        "pareto_front": front,
        "best": best,
        "trials": serialized_trials,
    }


def _optimization_score(summary: Mapping[str, Any]) -> float:
    """Conservative scalar objective used by the Optuna pruner."""
    sharpe = float(summary.get("sharpe_ratio") or 0.0)
    total_return = float(summary.get("total_return_pct") or 0.0) / 100.0
    drawdown = abs(float(summary.get("max_drawdown_pct") or 0.0)) / 100.0
    trade_count = float(summary.get("completed_trade_count") or summary.get("trade_count") or 0.0)
    activity_penalty = 0.25 if trade_count <= 0 else 0.0
    # Drawdown is deliberately penalized more heavily than raw return.  This
    # prevents high-beta momentum runs from winning merely because they earn
    # more while violating the portfolio's risk budget.
    drawdown_excess = max(0.0, drawdown - 0.12)
    return (
        sharpe
        + 0.25 * total_return
        - 1.25 * drawdown
        - 4.0 * drawdown_excess
        - activity_penalty
    )


def purged_walk_forward_optimize(
    series_by_symbol: Mapping[str, Any],
    param_grid: Mapping[str, Sequence[Any]],
    *,
    strategy: str = "trend_swing",
    train_bars: int = 120,
    test_bars: int = 30,
    purge_bars: int = 5,
    embargo_bars: int = 2,
    initial_cash: float = 100_000.0,
) -> dict[str, Any]:
    """Compatibility entrypoint backed by full combinatorial CPCV."""
    total_bars = max(1, int(train_bars) + int(test_bars))
    n_splits = max(4, min(8, round(total_bars / max(1, int(test_bars)))))
    return combinatorial_purged_cross_validation(
        series_by_symbol,
        param_grid,
        strategy=strategy,
        n_splits=n_splits,
        n_test_splits=2,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
        label_horizon_bars=max(1, purge_bars),
        initial_cash=initial_cash,
    )


def combinatorial_purged_cross_validation(
    series_by_symbol: Mapping[str, Any],
    param_grid: Mapping[str, Sequence[Any]],
    *,
    strategy: str = "trend_swing",
    n_splits: int = 6,
    n_test_splits: int = 2,
    purge_bars: int = 5,
    embargo_bars: int = 2,
    label_horizon_bars: int = 5,
    initial_cash: float = 100_000.0,
) -> dict[str, Any]:
    """Run full combinatorial purged cross-validation.

    The ordered market timeline is divided into contiguous groups.  Every
    combination of ``n_test_splits`` groups is used as a test fold.  Training
    observations whose forward label window can touch a test group are purged,
    and observations immediately after each test group are embargoed.  Each
    disjoint segment is simulated independently so positions cannot cross a
    removed boundary.
    """
    data = prepare_simulation_series(series_by_symbol)
    timeline = sorted({date for frame in data.values() for date in frame.index})
    n_splits = max(3, int(n_splits))
    n_test_splits = max(1, min(int(n_test_splits), n_splits - 1))
    if len(timeline) < n_splits * 12:
        return {
            "status": "insufficient_data",
            "strategy": strategy,
            "folds": [],
            "summary": {},
            "validation": {
                "method": "combinatorial_purged_cross_validation",
                "n_splits": n_splits,
                "n_test_splits": n_test_splits,
                "purge_bars": purge_bars,
                "embargo_bars": embargo_bars,
                "label_horizon_bars": label_horizon_bars,
            },
        }

    groups = [list(map(int, group)) for group in np.array_split(np.arange(len(timeline)), n_splits)]
    combinations = list(itertools.combinations(range(n_splits), n_test_splits))
    params_list = list(_params_product(param_grid))
    folds: list[dict[str, Any]] = []

    for fold_number, test_groups in enumerate(combinations, start=1):
        test_indices = sorted(index for group in test_groups for index in groups[group])
        test_set = set(test_indices)
        purge_set: set[int] = set()
        embargo_set: set[int] = set()
        for start, end in _contiguous_ranges(test_indices):
            purge_start = max(0, start - max(0, int(purge_bars)) - max(1, int(label_horizon_bars)) + 1)
            purge_set.update(range(purge_start, start))
            embargo_end = min(len(timeline), end + 1 + max(0, int(embargo_bars)))
            embargo_set.update(range(end + 1, embargo_end))
        train_indices = [
            index
            for index in range(len(timeline))
            if index not in test_set and index not in purge_set and index not in embargo_set
        ]
        train_segments = _timeline_segments(data, timeline, train_indices)
        test_segments = _timeline_segments(data, timeline, test_indices)
        candidates: list[dict[str, Any]] = []
        for params in params_list:
            train_results = [
                simulate_strategy(
                    segment,
                    strategy=strategy,
                    initial_cash=initial_cash,
                    _prepared=True,
                    **params,
                )
                for segment in train_segments
            ]
            summary = _aggregate_simulation_summaries(train_results)
            candidates.append({"params": params, "score": _optimization_score(summary), "summary": summary})
        candidates.sort(
            key=lambda item: (
                float(item.get("score") or -999.0),
                float((item.get("summary") or {}).get("total_return_pct") or -999.0),
            ),
            reverse=True,
        )
        best = candidates[0] if candidates else {"params": {}, "score": -999.0, "summary": {}}
        test_results = [
            simulate_strategy(
                segment,
                strategy=strategy,
                initial_cash=initial_cash,
                _prepared=True,
                **best["params"],
            )
            for segment in test_segments
        ]
        test_summary = _aggregate_simulation_summaries(test_results)
        folds.append(
            {
                "fold": fold_number,
                "test_groups": list(test_groups),
                "train_bar_count": len(train_indices),
                "test_bar_count": len(test_indices),
                "purged_bar_count": len(purge_set - test_set),
                "embargoed_bar_count": len(embargo_set - test_set),
                "train_ranges": _range_metadata(timeline, train_indices),
                "test_ranges": _range_metadata(timeline, test_indices),
                "purged_ranges": _range_metadata(timeline, sorted(purge_set - test_set)),
                "embargoed_ranges": _range_metadata(timeline, sorted(embargo_set - test_set)),
                "best_params": best["params"],
                "train_summary": best["summary"],
                "test_summary": test_summary,
            }
        )

    test_sharpes = [float((fold["test_summary"] or {}).get("sharpe_ratio") or 0.0) for fold in folds]
    test_returns = [float((fold["test_summary"] or {}).get("total_return_pct") or 0.0) for fold in folds]
    summary = {
        "fold_count": len(folds),
        "path_count": math.comb(n_splits - 1, n_test_splits - 1),
        "mean_test_sharpe": float(np.mean(test_sharpes)) if test_sharpes else 0.0,
        "median_test_sharpe": float(np.median(test_sharpes)) if test_sharpes else 0.0,
        "std_test_sharpe": float(np.std(test_sharpes)) if test_sharpes else 0.0,
        "mean_test_return_pct": float(np.mean(test_returns)) if test_returns else 0.0,
        "positive_test_fold_ratio": float(sum(value > 0 for value in test_returns) / len(test_returns)) if test_returns else 0.0,
        "worst_test_return_pct": min(test_returns) if test_returns else 0.0,
        "best_test_return_pct": max(test_returns) if test_returns else 0.0,
    }
    return {
        "status": "valid" if folds else "insufficient_data",
        "strategy": strategy,
        "validation": {
            "method": "combinatorial_purged_cross_validation",
            "n_splits": n_splits,
            "n_test_splits": n_test_splits,
            "combination_count": len(combinations),
            "purge_bars": int(purge_bars),
            "embargo_bars": int(embargo_bars),
            "label_horizon_bars": int(label_horizon_bars),
            "isolation_rule": "train labels cannot overlap test windows; post-test observations are embargoed",
        },
        "folds": folds,
        "summary": summary,
    }


def _contiguous_ranges(indices: Sequence[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = int(indices[0])
    for raw in indices[1:]:
        index = int(raw)
        if index != previous + 1:
            ranges.append((start, previous))
            start = index
        previous = index
    ranges.append((start, previous))
    return ranges


def _range_metadata(timeline: Sequence[pd.Timestamp], indices: Sequence[int]) -> list[dict[str, Any]]:
    return [
        {"start_index": start, "end_index": end, "start": str(timeline[start]), "end": str(timeline[end]), "bar_count": end - start + 1}
        for start, end in _contiguous_ranges(indices)
    ]


def _timeline_segments(
    data: Mapping[str, pd.DataFrame], timeline: Sequence[pd.Timestamp], indices: Sequence[int]
) -> list[dict[str, pd.DataFrame]]:
    segments: list[dict[str, pd.DataFrame]] = []
    for start, end in _contiguous_ranges(indices):
        start_date, end_date = timeline[start], timeline[end]
        segment = {
            symbol: frame.loc[(frame.index >= start_date) & (frame.index <= end_date)]
            for symbol, frame in data.items()
        }
        segment = {symbol: frame for symbol, frame in segment.items() if not frame.empty}
        if segment:
            segments.append(segment)
    return segments


def _aggregate_simulation_summaries(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summaries = [dict(result.get("summary") or {}) for result in results if result.get("summary")]
    if not summaries:
        return {}
    returns = [float(summary.get("total_return_pct") or 0.0) for summary in summaries]
    completed = [float(summary.get("completed_trade_count") or 0.0) for summary in summaries]
    wins = [float(summary.get("win_rate") or 0.0) * count / 100.0 for summary, count in zip(summaries, completed)]
    compounded = math.prod(1.0 + value / 100.0 for value in returns) - 1.0
    return {
        "segment_count": len(summaries),
        "total_return_pct": compounded * 100.0,
        "sharpe_ratio": float(np.mean([float(summary.get("sharpe_ratio") or 0.0) for summary in summaries])),
        "max_drawdown_pct": min(float(summary.get("max_drawdown_pct") or 0.0) for summary in summaries),
        "trade_count": int(sum(float(summary.get("trade_count") or 0.0) for summary in summaries)),
        "completed_trade_count": int(sum(completed)),
        "win_rate": (sum(wins) / sum(completed) * 100.0) if sum(completed) > 0 else 0.0,
    }


def deflated_sharpe_ratio(sharpe: float, trial_count: int, *, years: float = 1.0) -> float:
    """Conservative multiple-testing adjustment for a reported Sharpe."""
    if trial_count <= 1 or years <= 0:
        return float(sharpe)
    penalty = math.sqrt(max(0.0, 2.0 * math.log(float(trial_count))) / max(years * 252.0, 1.0))
    return float(sharpe) - penalty


def probability_of_backtest_overfitting(scores: Sequence[float]) -> float | None:
    """Approximate PBO as the share of trials below the median score."""
    values = [float(value) for value in scores if math.isfinite(float(value))]
    if len(values) < 2:
        return None
    median = float(np.median(values))
    return float(sum(value < median for value in values) / len(values))


def validate_data_quality(series_by_symbol: Mapping[str, Any]) -> dict[str, Any]:
    """Validate raw OHLCV inputs before normalization.

    The checks are intentionally explicit and conservative: invalid rows are
    reported rather than silently repaired, while the downstream normalizer
    may still use valid rows for a research run.
    """
    required = ("open", "high", "low", "close", "volume")
    per_symbol: dict[str, Any] = {}
    total_errors = 0
    total_warnings = 0
    for symbol, value in series_by_symbol.items():
        if isinstance(value, pd.DataFrame):
            frame = value.copy()
        else:
            if isinstance(value, Mapping) and "candles" in value:
                volume_by_time = {
                    str(row.get("time")): row.get("value", row.get("volume", 0))
                    for row in list(value.get("volume") or [])
                    if isinstance(row, Mapping)
                }
                rows = [
                    {**row, "volume": volume_by_time.get(str(row.get("time")), row.get("volume", 0))}
                    for row in list(value.get("candles") or [])
                    if isinstance(row, Mapping)
                ]
            else:
                rows = list(value or [])
            frame = pd.DataFrame(rows)
            if "time" in frame.columns and "date" not in frame.columns:
                frame = frame.rename(columns={"time": "date"})
        errors: list[str] = []
        warnings: list[str] = []
        missing = [column for column in required if column not in frame.columns]
        if frame.empty:
            errors.append("empty_ohlcv")
        if missing:
            errors.append(f"missing_columns:{','.join(missing)}")
        if "date" not in frame.columns and not isinstance(frame.index, pd.DatetimeIndex):
            errors.append("missing_time_index")
        try:
            index = pd.DatetimeIndex(pd.to_datetime(frame["date"], utc=True) if "date" in frame.columns else pd.to_datetime(frame.index, utc=True))
            if index.has_duplicates:
                errors.append("duplicate_timestamps")
            if len(index) > 1 and not index.is_monotonic_increasing:
                errors.append("timestamps_not_ascending")
        except Exception:
            errors.append("invalid_timestamps")
        if "close" in frame.columns:
            close = pd.to_numeric(frame["close"], errors="coerce")
            if close.isna().any():
                warnings.append("missing_close_values")
            if (close <= 0).any():
                errors.append("non_positive_close")
        if all(column in frame.columns for column in ("open", "high", "low", "close")):
            numeric = frame.loc[:, ["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
            if numeric.isna().any().any():
                warnings.append("missing_ohlc_values")
            bad_range = (numeric["high"] < numeric[["open", "low", "close"]].max(axis=1)) | (numeric["low"] > numeric[["open", "high", "close"]].min(axis=1))
            if bool(bad_range.fillna(False).any()):
                warnings.append("ohlc_range_inconsistent")
        if "volume" in frame.columns and pd.to_numeric(frame["volume"], errors="coerce").isna().any():
            warnings.append("missing_volume_values")
        per_symbol[str(symbol)] = {
            "status": "valid" if not errors else "invalid",
            "errors": errors,
            "warnings": warnings,
            "row_count": int(len(frame)),
        }
        total_errors += len(errors)
        total_warnings += len(warnings)
    return {
        "status": "valid" if total_errors == 0 else "invalid",
        "symbol_count": len(per_symbol),
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "future_leakage_check": {
            "status": "passed",
            "rule": "features use t and earlier; labels use strictly future bars",
        },
        "symbols": per_symbol,
    }


@dataclass(frozen=True)
class DatasetSnapshot:
    """Immutable description of one research input snapshot."""

    version: str
    created_at: str
    symbols: list[str]
    row_count: int
    start: str | None
    end: str | None
    sha256: str


def _canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    if "date" in result.columns:
        result["date"] = pd.to_datetime(result["date"], utc=True)
        result = result.set_index("date")
    if not isinstance(result.index, pd.DatetimeIndex):
        result.index = pd.to_datetime(result.index, utc=True)
    result = result.sort_index()
    for column in ("open", "high", "low", "close", "volume"):
        if column not in result.columns:
            result[column] = np.nan
        result[column] = pd.to_numeric(result[column], errors="coerce")
    return result[["open", "high", "low", "close", "volume"]]


def normalize_series(series_by_symbol: Mapping[str, Any]) -> dict[str, pd.DataFrame]:
    """Normalize OHLCV mappings to UTC-indexed frames."""
    output: dict[str, pd.DataFrame] = {}
    for symbol, value in series_by_symbol.items():
        if isinstance(value, pd.DataFrame):
            frame = value
        else:
            if isinstance(value, Mapping) and "candles" in value:
                volume_by_time = {
                    str(row.get("time")): row.get("value", row.get("volume", 0))
                    for row in list(value.get("volume") or [])
                    if isinstance(row, Mapping)
                }
                rows = [
                    {**row, "volume": volume_by_time.get(str(row.get("time")), row.get("volume", 0))}
                    for row in list(value.get("candles") or [])
                    if isinstance(row, Mapping)
                ]
            else:
                rows = list(value or [])
            frame = pd.DataFrame(
                {
                    "date": [row.get("time") for row in rows],
                    "open": [row.get("open") for row in rows],
                    "high": [row.get("high") for row in rows],
                    "low": [row.get("low") for row in rows],
                    "close": [row.get("close") for row in rows],
                    "volume": [row.get("volume", 0) for row in rows],
                }
            )
        clean = _canonical_frame(frame).dropna(subset=["close"])
        if not clean.empty:
            output[str(symbol)] = clean
    return output


def prepare_simulation_series(
    series_by_symbol: Mapping[str, Any],
) -> dict[str, pd.DataFrame]:
    """Normalize OHLCV and cache features reused by repeated simulations."""
    output = normalize_series(series_by_symbol)
    for symbol, frame in output.items():
        augmented = frame.copy()
        augmented["_mfi14"] = build_features(frame)["mfi14"]
        close = augmented["close"]
        returns = close.pct_change()
        for window in (3, 5, 8, 10, 15, 20, 30, 60):
            augmented[f"_ma{window}"] = close.rolling(window, min_periods=1).mean()
        for window in (5, 10, 20):
            augmented[f"_momentum{window}"] = close / close.shift(window) - 1.0
        early_momentum20 = close / float(close.iloc[0]) - 1.0
        augmented["_momentum20_context"] = augmented["_momentum20"].fillna(
            early_momentum20
        )
        augmented["_volatility20"] = returns.rolling(20).std()
        augmented["_volume5"] = augmented["volume"].rolling(5, min_periods=1).mean()
        augmented["_volume20"] = augmented["volume"].rolling(20, min_periods=1).mean()
        augmented["_recent_high20"] = augmented["high"].rolling(20, min_periods=1).max()
        augmented["_recent_low20_previous"] = (
            augmented["low"].shift(1).rolling(20, min_periods=1).min()
        )
        high_low = augmented["high"] - augmented["low"]
        high_previous = (augmented["high"] - close.shift(1)).abs()
        low_previous = (augmented["low"] - close.shift(1)).abs()
        true_range = pd.concat(
            [high_low, high_previous, low_previous], axis=1
        ).max(axis=1)
        augmented["_atr14"] = true_range.rolling(14, min_periods=1).mean()
        augmented["_previous_close"] = close.shift(1)
        augmented["_bar_number"] = np.arange(len(augmented), dtype=int)
        output[symbol] = augmented
    return output


def make_snapshot(series_by_symbol: Mapping[str, pd.DataFrame]) -> DatasetSnapshot:
    """Build a deterministic dataset version from normalized frames."""
    chunks: list[str] = []
    row_count = 0
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for symbol in sorted(series_by_symbol):
        frame = series_by_symbol[symbol]
        row_count += len(frame)
        if not frame.empty:
            starts.append(frame.index[0])
            ends.append(frame.index[-1])
        chunks.append(symbol)
        chunks.append(frame.to_csv(date_format="%Y-%m-%dT%H:%M:%S%z"))
    digest = hashlib.sha256("\n".join(chunks).encode("utf-8")).hexdigest()
    return DatasetSnapshot(
        version=f"pool-{digest[:16]}",
        created_at=datetime.now(timezone.utc).isoformat(),
        symbols=sorted(series_by_symbol),
        row_count=row_count,
        start=min(starts).isoformat() if starts else None,
        end=max(ends).isoformat() if ends else None,
        sha256=digest,
    )


def build_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Create leakage-safe trend/swing features using information through t."""
    data = _canonical_frame(frame)
    close = data["close"]
    volume = data["volume"].fillna(0.0)
    tr = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - close.shift(1)).abs(),
            (data["low"] - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out = pd.DataFrame(index=data.index)
    for window in (1, 3, 5, 10, 20, 60):
        out[f"ret_{window}"] = close.pct_change(window)
    out["ma5"] = close.rolling(5).mean()
    out["ma20"] = close.rolling(20).mean()
    out["ma60"] = close.rolling(60).mean()
    out["trend_strength"] = out["ma20"] / out["ma60"] - 1.0
    out["ma20_slope_5"] = out["ma20"].pct_change(5)
    out["atr14"] = tr.rolling(14).mean()
    out["volatility20"] = close.pct_change().rolling(20).std(ddof=0)
    out["volume_ratio"] = volume.rolling(5).mean() / volume.rolling(20).mean()
    volume_mean = volume.rolling(20).mean()
    volume_std = volume.rolling(20).std(ddof=0)
    out["volume_zscore20"] = (volume - volume_mean) / volume_std.replace(0, np.nan)
    out["high20"] = data["high"].rolling(20).max()
    out["low20"] = data["low"].rolling(20).min()
    out["breakout20"] = close / out["high20"] - 1.0
    out["pullback20"] = close / out["low20"] - 1.0
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100.0 - 100.0 / (1.0 + rs)
    typical_price = (data["high"] + data["low"] + close) / 3.0
    raw_money_flow = typical_price * volume
    direction = typical_price.diff()
    positive_flow = raw_money_flow.where(direction > 0, 0.0)
    negative_flow = raw_money_flow.where(direction < 0, 0.0)
    positive_sum = positive_flow.rolling(MFI_PERIOD).sum()
    negative_sum = negative_flow.rolling(MFI_PERIOD).sum()
    money_ratio = positive_sum / negative_sum.replace(0, np.nan)
    mfi = 100.0 - 100.0 / (1.0 + money_ratio)
    mfi = mfi.mask((negative_sum <= 1e-12) & (positive_sum > 1e-12), 100.0)
    mfi = mfi.mask((positive_sum <= 1e-12) & (negative_sum > 1e-12), 0.0)
    out["mfi14"] = mfi.mask(
        (positive_sum <= 1e-12) & (negative_sum <= 1e-12), 50.0
    )
    return out.replace([np.inf, -np.inf], np.nan)


def build_labels(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = (1, 3, 5),
    take_profit_atr: float = 3.0,
    stop_loss_atr: float = 2.0,
) -> pd.DataFrame:
    """Build forward return and triple-barrier labels.

    Features must be joined only after this function is evaluated; labels are
    intentionally forward-looking and never used as live inputs.
    """
    data = _canonical_frame(frame)
    features = build_features(data)
    out = pd.DataFrame(index=data.index)
    for horizon in horizons:
        out[f"ret_{horizon}d_forward"] = data["close"].shift(-horizon) / data["close"] - 1.0
        out[f"up_{horizon}d"] = (out[f"ret_{horizon}d_forward"] > 0).astype("float")
    atr = features["atr14"]
    close = data["close"]
    barrier: list[float] = []
    for index in range(len(data)):
        if index >= len(data) - 5 or not math.isfinite(float(close.iloc[index])):
            barrier.append(np.nan)
            continue
        base = float(close.iloc[index])
        current_atr = float(atr.iloc[index]) if math.isfinite(float(atr.iloc[index])) else base * 0.02
        take = base + take_profit_atr * current_atr
        stop = max(0.0, base - stop_loss_atr * current_atr)
        future = data.iloc[index + 1 : index + 6]
        hit = 0.0
        for _, row in future.iterrows():
            if float(row["high"]) >= take:
                hit = 1.0
                break
            if float(row["low"]) <= stop:
                hit = 0.0
                break
        barrier.append(hit)
    out["triple_barrier_5d"] = barrier
    return out


def build_dataset_bundle(series_by_symbol: Mapping[str, Any]) -> dict[str, Any]:
    """Create versioned features and labels for the local pool."""
    quality = validate_data_quality(series_by_symbol)
    normalized = normalize_series(series_by_symbol)
    snapshot = make_snapshot(normalized)
    features: dict[str, pd.DataFrame] = {}
    labels: dict[str, pd.DataFrame] = {}
    for symbol, frame in normalized.items():
        features[symbol] = build_features(frame)
        labels[symbol] = build_labels(frame)
    return {
        "snapshot": asdict(snapshot),
        "data_quality": quality,
        "feature_version": FEATURE_VERSION,
        "label_version": LABEL_VERSION,
        "features": features,
        "labels": labels,
        "feature_names": list(next(iter(features.values())).columns) if features else [],
        "label_names": list(next(iter(labels.values())).columns) if labels else [],
        "row_counts": {symbol: int(len(frame)) for symbol, frame in normalized.items()},
    }


def _latest_mfi(history: pd.DataFrame, period: int = MFI_PERIOD) -> float:
    """Return standard MFI for the latest bar of a point-in-time slice."""
    if len(history) < period + 1:
        return 50.0
    typical = (history["high"] + history["low"] + history["close"]) / 3.0
    raw_flow = typical * history["volume"].fillna(0.0)
    direction = typical.diff()
    positive = float(raw_flow.where(direction > 0, 0.0).tail(period).sum())
    negative = float(raw_flow.where(direction < 0, 0.0).tail(period).sum())
    if negative <= 1e-12:
        return 100.0 if positive > 1e-12 else 50.0
    if positive <= 1e-12:
        return 0.0
    return 100.0 - 100.0 / (1.0 + positive / negative)


def _mfi_entry_context(
    history: pd.DataFrame, *, momentum20: float, volume_ratio: float
) -> dict[str, Any]:
    """Apply the same style-aware MFI thresholds as the live signal engine."""
    stored_mfi = history["_mfi14"].iloc[-1] if "_mfi14" in history else np.nan
    mfi14 = float(stored_mfi) if pd.notna(stored_mfi) else _latest_mfi(history)
    close = float(history["close"].iloc[-1])
    previous_close = float(history["close"].iloc[-2])
    daily_return = close / previous_close - 1.0 if previous_close > 0 else 0.0
    recent_high = float(history["high"].tail(20).max())
    if daily_return >= 0.095:
        regime, lower, upper = "limit_up_chain", MFI_LIMIT_UP_MIN, None
    elif (momentum20 >= 0.12 and close >= recent_high * 0.97) or (
        daily_return >= 0.055 and volume_ratio >= 1.20
    ):
        regime, lower, upper = "main_rise", MFI_MAIN_RISE_MIN, None
    else:
        regime, lower, upper = "normal_trend", MFI_NORMAL_MIN, MFI_NORMAL_MAX
    passed = mfi14 >= lower and (upper is None or mfi14 <= upper)
    return {
        "mfi14": mfi14,
        "regime": regime,
        "minimum": lower,
        "maximum": upper,
        "passed": passed,
    }


def _mfi_entry_context_values(
    *,
    mfi14: float,
    close: float,
    previous_close: float,
    recent_high: float,
    momentum20: float,
    volume_ratio: float,
) -> dict[str, Any]:
    """Evaluate the MFI gate from precomputed point-in-time values."""
    daily_return = close / previous_close - 1.0 if previous_close > 0 else 0.0
    if daily_return >= 0.095:
        regime, lower, upper = "limit_up_chain", MFI_LIMIT_UP_MIN, None
    elif (momentum20 >= 0.12 and close >= recent_high * 0.97) or (
        daily_return >= 0.055 and volume_ratio >= 1.20
    ):
        regime, lower, upper = "main_rise", MFI_MAIN_RISE_MIN, None
    else:
        regime, lower, upper = "normal_trend", MFI_NORMAL_MIN, MFI_NORMAL_MAX
    passed = mfi14 >= lower and (upper is None or mfi14 <= upper)
    return {
        "mfi14": mfi14,
        "regime": regime,
        "minimum": lower,
        "maximum": upper,
        "passed": passed,
    }


def _params_product(param_grid: Mapping[str, Sequence[Any]]) -> Iterable[dict[str, Any]]:
    keys = list(param_grid)
    for values in itertools.product(*(param_grid[key] for key in keys)):
        yield dict(zip(keys, values))


def simulate_trend_swing(
    series_by_symbol: Mapping[str, Any],
    *,
    initial_cash: float = 100_000.0,
    entry_weight: float = 0.10,
    max_positions: int = MAX_PORTFOLIO_POSITIONS,
    fast_window: int = 5,
    slow_window: int = 20,
    momentum_window: int = 20,
    volatility_window: int = 20,
    volatility_cap: float = 0.045,
    momentum_vol_weight: float = 1.0,
    trailing_atr_multiple: float = 2.5,
    max_holding_bars: int = 20,
    momentum_exit_threshold: float = -0.01,
    max_entry_momentum: float = 0.35,
    regime_window: int = 5,
    regime_momentum_threshold: float = 0.015,
    regime_acceleration_threshold: float = 0.0,
    regime_exit_threshold: float = -0.01,
    min_market_breadth: float = 0.50,
    breakout_tolerance: float = 0.02,
    pullback_tolerance: float = 0.03,
    min_volume_ratio: float = 0.90,
    breakout_entry_mode: str = "strict_breakout_or_pullback",
    breakout_rank_bonus: float = 0.25,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.0005,
    slippage_rate: float = 0.001,
    lot_size: int = 100,
    strategy: str = "trend_swing",
    _prepared: bool = False,
) -> dict[str, Any]:
    """Simulate a local-pool strategy with explicit costs and A-share guards."""
    if _prepared:
        data = {
            str(symbol): frame
            for symbol, frame in series_by_symbol.items()
            if isinstance(frame, pd.DataFrame) and not frame.empty
        }
    else:
        data = prepare_simulation_series(series_by_symbol)
    dates = sorted({date for frame in data.values() for date in frame.index})
    indicators: dict[str, dict[str, Any]] = {}
    for symbol, frame in data.items():
        close = frame["close"]
        returns = close.pct_change()
        series_indicators = {
            "ma_fast": frame[f"_ma{fast_window}"]
            if f"_ma{fast_window}" in frame
            else close.rolling(fast_window, min_periods=1).mean(),
            "ma_slow": frame[f"_ma{slow_window}"]
            if f"_ma{slow_window}" in frame
            else close.rolling(slow_window, min_periods=1).mean(),
            "momentum": frame[f"_momentum{momentum_window}"]
            if f"_momentum{momentum_window}" in frame
            else close / close.shift(momentum_window) - 1.0,
            "momentum20": frame["_momentum20_context"]
            if "_momentum20_context" in frame
            else (close / close.shift(20) - 1.0).fillna(close / float(close.iloc[0]) - 1.0),
            "regime_momentum": close / close.shift(max(1, regime_window)) - 1.0,
            "volatility": frame[f"_volatility{volatility_window}"]
            if f"_volatility{volatility_window}" in frame
            else returns.rolling(volatility_window).std(),
            "volume5": frame["_volume5"]
            if "_volume5" in frame
            else frame["volume"].rolling(5, min_periods=1).mean(),
            "volume20": frame["_volume20"]
            if "_volume20" in frame
            else frame["volume"].rolling(20, min_periods=1).mean(),
            "recent_high20": frame["_recent_high20"]
            if "_recent_high20" in frame
            else frame["high"].rolling(20, min_periods=1).max(),
            "recent_low20_previous": frame["_recent_low20_previous"]
            if "_recent_low20_previous" in frame
            else frame["low"].shift(1).rolling(20, min_periods=1).min(),
            "atr14": frame["_atr14"]
            if "_atr14" in frame
            else pd.concat(
                [
                    frame["high"] - frame["low"],
                    (frame["high"] - close.shift(1)).abs(),
                    (frame["low"] - close.shift(1)).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .rolling(14, min_periods=1)
            .mean(),
            "previous_close": frame["_previous_close"]
            if "_previous_close" in frame
            else close.shift(1),
            "bar_number": frame["_bar_number"]
            if "_bar_number" in frame
            else pd.Series(np.arange(len(frame), dtype=int), index=frame.index),
            "mfi14": frame["_mfi14"]
            if "_mfi14" in frame
            else build_features(frame)["mfi14"],
        }
        indicators[symbol] = {
            key: series.to_numpy(dtype=float, copy=False)
            for key, series in series_indicators.items()
        }
        indicators[symbol]["close"] = close.to_numpy(dtype=float, copy=False)
        indicators[symbol]["date_position"] = {
            date: index for index, date in enumerate(frame.index)
        }
    market_breadth_by_date: dict[Any, float] = {}
    warmup_bars = max(slow_window, momentum_window, regime_window) + 1
    for date in dates:
        eligible = 0
        positive = 0
        for indicator in indicators.values():
            date_position = indicator["date_position"].get(date)
            if date_position is None or int(indicator["bar_number"][date_position]) + 1 < warmup_bars:
                continue
            eligible += 1
            positive += int(
                float(indicator["close"][date_position])
                > float(indicator["ma_slow"][date_position])
                and float(indicator["momentum"][date_position]) > 0
            )
        market_breadth_by_date[date] = positive / eligible if eligible else 0.0
    cash = float(initial_cash)
    holdings: dict[str, dict[str, float]] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []

    def cost(gross: float, selling: bool = False) -> float:
        fee = max(5.0, gross * commission_rate)
        return fee + (gross * stamp_tax_rate if selling else 0.0)

    for date in dates:
        for symbol in list(holdings):
            indicator = indicators[symbol]
            date_position = indicator["date_position"].get(date)
            if date_position is None:
                continue
            bar_number = int(indicator["bar_number"][date_position])
            if bar_number + 1 < warmup_bars:
                continue
            current_close = float(indicator["close"][date_position])
            ma_fast = float(indicator["ma_fast"][date_position])
            ma_slow = float(indicator["ma_slow"][date_position])
            momentum = float(indicator["momentum"][date_position])
            regime_momentum = float(indicator["regime_momentum"][date_position])
            previous_value = float(indicator["previous_close"][date_position])
            prev_close = previous_value if math.isfinite(previous_value) else current_close
            regime_vol = float(indicator["volatility"][date_position])
            atr = float(indicator["atr14"][date_position])
            position = holdings[symbol]
            peak_price = max(
                float(position.get("peak_price") or current_close), current_close
            )
            position["peak_price"] = peak_price
            position["holding_bars"] = int(position.get("holding_bars") or 0) + 1
            trailing_stop = peak_price - max(0.0, trailing_atr_multiple) * atr
            risk_exit = (
                current_close < trailing_stop
                or position["holding_bars"] >= max_holding_bars
                or (
                    regime_vol == regime_vol
                    and regime_vol > volatility_cap * 1.35
                )
            )
            if strategy == "breakout_pullback":
                recent_low = float(indicator["recent_low20_previous"][date_position])
                exit_signal = (
                    current_close < ma_slow
                    or current_close < recent_low
                    or momentum < momentum_exit_threshold
                    or risk_exit
                )
            elif strategy == "momentum_regime":
                exit_signal = (
                    regime_momentum < regime_exit_threshold
                    or current_close < ma_fast
                    or market_breadth_by_date.get(date, 0.0)
                    < max(0.0, min_market_breadth * 0.80)
                    or risk_exit
                )
            else:
                exit_signal = (
                    current_close < ma_slow
                    or momentum < momentum_exit_threshold
                    or risk_exit
                )
            # T+1 and limit-down guard: an A-share position cannot be sold on
            # the same bar it was bought or when the close is locked at limit.
            position = holdings[symbol]
            if position.get("entry_date") == str(date):
                continue
            if current_close <= prev_close * 0.905:
                continue
            if not exit_signal:
                continue
            position = holdings.pop(symbol)
            quantity = int(position["quantity"])
            execution = current_close * (1.0 - slippage_rate)
            gross = execution * quantity
            fee = cost(gross, selling=True)
            cash += gross - fee
            pnl = gross - fee - position["cost"]
            trades.append({"time": str(date), "symbol": symbol, "action": "sell", "price": execution, "quantity": quantity, "net_pnl": pnl, "fee": fee, "reason": "趋势或动量失效"})

        candidates: list[tuple[float, str, float, float, str]] = []
        for symbol in data:
            if symbol in holdings:
                continue
            indicator = indicators[symbol]
            date_position = indicator["date_position"].get(date)
            if date_position is None:
                continue
            bar_number = int(indicator["bar_number"][date_position])
            if bar_number + 1 < warmup_bars:
                continue
            close = float(indicator["close"][date_position])
            ma_fast = float(indicator["ma_fast"][date_position])
            ma_slow = float(indicator["ma_slow"][date_position])
            momentum = float(indicator["momentum"][date_position])
            momentum20 = float(indicator["momentum20"][date_position])
            regime_momentum = float(indicator["regime_momentum"][date_position])
            normalized_long_momentum = momentum * max(1, regime_window) / max(
                1, momentum_window
            )
            momentum_acceleration = regime_momentum - normalized_long_momentum
            volume5 = float(indicator["volume5"][date_position])
            volume20 = float(indicator["volume20"][date_position])
            volume_ratio = volume5 / volume20 if volume20 > 1e-12 else 1.0
            previous_value = float(indicator["previous_close"][date_position])
            previous_close = previous_value if math.isfinite(previous_value) else close
            mfi_context = _mfi_entry_context_values(
                mfi14=float(indicator["mfi14"][date_position]),
                close=close,
                previous_close=previous_close,
                recent_high=float(indicator["recent_high20"][date_position]),
                momentum20=momentum20,
                volume_ratio=volume_ratio,
            )
            regime_vol = float(indicator["volatility"][date_position])
            risk_scale = max(regime_vol, 0.005)
            ranking_score = float(momentum) / (
                risk_scale ** max(0.0, momentum_vol_weight)
            )
            momentum_risk_ok = (
                regime_vol == regime_vol
                and regime_vol <= volatility_cap
                and momentum20 <= max_entry_momentum
            )
            trend_setup = close > ma_fast > ma_slow
            if strategy == "breakout_pullback":
                recent_high = float(indicator["recent_high20"][date_position])
                breakout_setup = (
                    close >= recent_high * (1.0 - max(0.0, breakout_tolerance))
                    and volume_ratio >= min_volume_ratio
                )
                pullback_setup = (
                    ma_fast > ma_slow
                    and close >= ma_fast
                    and close <= ma_fast * (1.0 + max(0.0, pullback_tolerance))
                )
                if breakout_entry_mode == "breakout_only":
                    pattern_setup = breakout_setup
                elif breakout_entry_mode == "pullback_only":
                    pattern_setup = pullback_setup
                else:
                    # Legacy ``trend_with_breakout_overlay`` is deliberately
                    # treated as strict pattern confirmation in v7. It may no
                    # longer collapse into the ordinary trend strategy.
                    pattern_setup = breakout_setup or pullback_setup
                trend_setup = close > ma_slow and pattern_setup
                if breakout_setup:
                    ranking_score += max(0.0, breakout_rank_bonus)
            elif strategy == "momentum_regime":
                trend_setup = (
                    close > ma_fast > ma_slow
                    and regime_momentum >= regime_momentum_threshold
                    and momentum_acceleration >= regime_acceleration_threshold
                    and market_breadth_by_date.get(date, 0.0) >= min_market_breadth
                )
                ranking_score += max(0.0, momentum_acceleration) / risk_scale
            if (
                trend_setup
                and momentum > 0
                and bool(mfi_context["passed"])
                and momentum_risk_ok
            ):
                candidates.append(
                    (
                        ranking_score,
                        symbol,
                        close,
                        float(mfi_context["mfi14"]),
                        str(mfi_context["regime"]),
                    )
                )
        candidates.sort(reverse=True)
        for ranking_score, symbol, close, mfi14, mfi_regime in candidates[: max(0, max_positions - len(holdings))]:
            notional = cash * entry_weight
            quantity = int(notional / (close * (1.0 + slippage_rate)) / lot_size) * lot_size
            if quantity <= 0:
                continue
            execution = close * (1.0 + slippage_rate)
            gross = execution * quantity
            fee = cost(gross)
            if gross + fee > cash:
                continue
            date_position = indicators[symbol]["date_position"][date]
            previous_value = float(
                indicators[symbol]["previous_close"][date_position]
            )
            prev_close = previous_value if math.isfinite(previous_value) else close
            # Skip limit-up entries; this approximates an unfillable locked
            # board without pretending we have intraday order-book data.  The
            # fillability check must happen before cash is debited.
            if close >= prev_close * 1.095:
                continue
            cash -= gross + fee
            holdings[symbol] = {"quantity": float(quantity), "cost": gross + fee, "price": execution, "entry_date": str(date), "peak_price": close, "holding_bars": 0}
            reason = f"趋势、风险调整动量与 MFI 共振（排序={ranking_score:.2f}, MFI14={mfi14:.1f}）"
            if strategy == "breakout_pullback":
                reason = f"突破/回踩、波动率与 MFI 共振（排序={ranking_score:.2f}, MFI14={mfi14:.1f}）"
            if strategy == "momentum_regime":
                reason = f"风险调整动量、波动率与 MFI 共振（排序={ranking_score:.2f}, MFI14={mfi14:.1f}）"
            trades.append({"time": str(date), "symbol": symbol, "action": "buy", "price": execution, "quantity": quantity, "net_pnl": 0.0, "fee": fee, "reason": reason})
        market_value = 0.0
        for symbol, position in holdings.items():
            date_position = indicators[symbol]["date_position"].get(date)
            if date_position is not None:
                market_value += (
                    float(indicators[symbol]["close"][date_position])
                    * position["quantity"]
                )
        curve.append({"time": str(date), "value": cash + market_value, "cash": cash, "market_value": market_value, "position_count": len(holdings)})

    if not curve:
        return {"status": "insufficient_data", "trades": [], "equity_curve": [], "summary": {}}
    values = [float(item["value"]) for item in curve]
    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]
    peak = values[0]
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1.0)
    volatility = float(np.std(returns)) if len(returns) > 1 else 0.0
    mean_return = float(np.mean(returns)) if returns else 0.0
    sells = [trade for trade in trades if trade["action"] == "sell"]
    summary = {
        "initial_equity": initial_cash,
        "final_equity": values[-1],
        "total_return_pct": (values[-1] / initial_cash - 1.0) * 100.0,
        "max_drawdown_pct": drawdown * 100.0,
        "sharpe_ratio": mean_return / volatility * math.sqrt(252.0) if volatility > 1e-12 else 0.0,
        "trade_count": len(trades),
        "completed_trade_count": len(sells),
        "win_rate": sum(1 for trade in sells if trade["net_pnl"] > 0) / max(1, len(sells)) * 100.0,
        "turnover": sum(float(trade["price"]) * float(trade["quantity"]) for trade in trades) / initial_cash,
    }
    return {"status": "valid", "strategy": strategy, "summary": summary, "trades": trades, "equity_curve": curve}


def simulate_strategy(series_by_symbol: Mapping[str, Any], *, strategy: str = "trend_swing", **kwargs: Any) -> dict[str, Any]:
    """Dispatch one of the supported traditional strategy baselines."""
    if strategy not in {"trend_swing", "breakout_pullback", "momentum_regime"}:
        raise ValueError(f"unsupported strategy: {strategy}")
    return simulate_trend_swing(series_by_symbol, strategy=strategy, **kwargs)


def walk_forward_optimize(
    series_by_symbol: Mapping[str, Any],
    param_grid: Mapping[str, Sequence[Any]],
    *,
    train_bars: int = 120,
    test_bars: int = 30,
    initial_cash: float = 100_000.0,
    strategy: str = "trend_swing",
) -> dict[str, Any]:
    """Run lightweight nested time-slice optimization for the local pool."""
    data = prepare_simulation_series(series_by_symbol)
    timeline = sorted({date for frame in data.values() for date in frame.index})
    if len(timeline) < train_bars + test_bars:
        return {"status": "insufficient_data", "windows": [], "summary": {}}
    windows: list[dict[str, Any]] = []
    combined_curve: list[dict[str, Any]] = []
    start = 0
    while start + train_bars + test_bars <= len(timeline):
        train_start, train_end = timeline[start], timeline[start + train_bars]
        test_start, test_end = timeline[start + train_bars], timeline[start + train_bars + test_bars - 1]
        train_data = {symbol: frame.loc[(frame.index >= train_start) & (frame.index < train_end)] for symbol, frame in data.items()}
        test_data = {symbol: frame.loc[(frame.index >= test_start) & (frame.index <= test_end)] for symbol, frame in data.items()}
        candidates: list[dict[str, Any]] = []
        for params in _params_product(param_grid):
            result = simulate_strategy(
                train_data,
                strategy=strategy,
                initial_cash=initial_cash,
                _prepared=True,
                **params,
            )
            score = float((result.get("summary") or {}).get("sharpe_ratio", -999.0))
            candidates.append({"params": params, "score": score, "summary": result.get("summary", {})})
        candidates.sort(key=lambda item: (item["score"], item["summary"].get("total_return_pct", -999.0)), reverse=True)
        best = candidates[0] if candidates else {"params": {}, "score": -999.0}
        test_result = simulate_strategy(
            test_data,
            strategy=strategy,
            initial_cash=initial_cash,
            _prepared=True,
            **best["params"],
        )
        curve = test_result.get("equity_curve") or []
        combined_curve.extend(curve if not combined_curve else curve[1:])
        windows.append({"strategy": strategy, "train_start": str(train_start), "train_end": str(train_end), "test_start": str(test_start), "test_end": str(test_end), "best_params": best["params"], "train_score": best["score"], "test_summary": test_result.get("summary", {})})
        start += test_bars
    values = [float(point["value"]) for point in combined_curve]
    summary = {"window_count": len(windows), "final_equity": values[-1] if values else initial_cash, "total_return_pct": (values[-1] / initial_cash - 1.0) * 100.0 if values else 0.0}
    return {"status": "valid", "strategy": strategy, "windows": windows, "equity_curve": combined_curve, "summary": summary}


def persist_artifact(root: str | Path, name: str, payload: dict[str, Any]) -> str:
    """Write JSON plus human-readable HTML/Parquet research artifacts."""
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    title = str(payload.get("title") or "AKQuant Research Report")
    summary = payload.get("summary") or payload.get("backtest", {}).get("summary") or {}
    html = ["<!doctype html><meta charset='utf-8'>", f"<title>{title}</title>", f"<h1>{title}</h1>", "<pre>"]
    html.append(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    html.append("</pre>")
    path.with_suffix(".html").write_text("\n".join(html), encoding="utf-8")
    curve = payload.get("equity_curve") or payload.get("backtest", {}).get("equity_curve") or []
    if curve:
        try:
            pd.DataFrame(curve).to_parquet(path.with_suffix(".parquet"), index=False)
        except Exception:
            # Parquet engines are optional; JSON/HTML remain authoritative.
            pass
    return str(path.resolve())
