from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import numpy as np
import pytest

from scripts.research_pipeline import (
    build_dataset_bundle,
    combinatorial_purged_cross_validation,
    deflated_sharpe_ratio,
    multi_objective_optimize,
    probability_of_backtest_overfitting,
    purged_walk_forward_optimize,
    simulate_trend_swing,
    simulate_strategy,
    validate_data_quality,
    walk_forward_optimize,
)


def _load_server():
    path = Path(__file__).resolve().parents[1] / "scripts" / "review_center_server.py"
    spec = importlib.util.spec_from_file_location("review_center_server_research_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = _load_server()


def test_analyze_series_includes_research_indicators() -> None:
    candles = []
    volume = []
    price = 20.0
    for index in range(120):
        price *= 1.0 + (0.001 if index % 5 else -0.0005)
        stamp = f"2026-01-{index + 1:02d}"
        candles.append(
            {
                "time": stamp,
                "open": price * 0.999,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
            }
        )
        volume.append({"time": stamp, "value": 1000.0 + index})
    result = SERVER._analyze_series({"candles": candles, "volume": volume})
    for key in (
        "trend_strength",
        "volatility20",
        "breakout20",
        "pullback20",
        "volume_zscore20",
    ):
        assert key in result
        assert isinstance(result[key], float)


def test_research_action_rejects_unknown_action() -> None:
    try:
        SERVER._run_research_action("unknown", {"positions": [], "watchlist": []})
    except ValueError as exc:
        assert "不支持的研究动作" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown research action should fail")


def test_local_execution_is_idempotent_for_empty_pool() -> None:
    result = SERVER._execute_local_signals(
        {"positions": [], "watchlist": [], "manual_trades": []},
        refresh=False,
    )
    assert result["status"] == "completed"
    assert result["applied"] == []


def test_local_cash_migrates_from_positions_and_realized_pnl() -> None:
    state = {
        "positions": [{"symbol": "000001", "quantity": 100, "entry_price": 20}],
        "manual_trades": [{"net_pnl": -100}],
    }
    assert SERVER._local_available_cash(state) == 97_900


def test_local_execution_reduces_buy_to_affordable_board_lots(monkeypatch) -> None:
    signal = {
        "symbol": "000001",
        "name": "测试股票",
        "selection_score": 90,
        "up_probability": 0.8,
        "execution_signal": {
            "signal_id": "test-buy-1",
            "symbol": "000001",
            "action": "buy",
            "price": 100,
            "quantity": 500,
        },
    }
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: [signal])
    state = {"positions": [], "watchlist": [], "manual_trades": [], "available_cash": 45_000}
    result = SERVER._execute_local_signals(state, refresh=False)
    assert result["applied"][0]["quantity"] == 400
    assert result["available_cash_after"] == 5_000


def test_local_execution_reports_insufficient_cash(monkeypatch) -> None:
    signal = {
        "symbol": "000001",
        "name": "测试股票",
        "selection_score": 90,
        "up_probability": 0.8,
        "execution_signal": {
            "signal_id": "test-buy-insufficient",
            "symbol": "000001",
            "action": "buy",
            "price": 100,
            "quantity": 100,
        },
    }
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: [signal])
    result = SERVER._execute_local_signals(
        {"positions": [], "watchlist": [], "manual_trades": [], "available_cash": 5_000},
        refresh=False,
    )
    assert result["applied"] == []
    assert result["skipped"][0]["reason_code"] == "insufficient_cash"
    assert result["skipped"][0]["required_cash"] > 10_000


def test_automated_execution_skips_conflict_and_human_review(monkeypatch) -> None:
    signals = []
    for symbol, fusion in (
        ("000001", {"conflict_level": "major", "human_review_required": False}),
        ("000002", {"conflict_level": "none", "human_review_required": True}),
    ):
        signals.append(
            {
                "symbol": symbol,
                "name": symbol,
                "fusion": fusion,
                "execution_signal": {
                    "signal_id": f"auto-gate-{symbol}",
                    "symbol": symbol,
                    "action": "buy",
                    "price": 10,
                    "quantity": 100,
                },
            }
        )
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: signals)
    state = {"positions": [], "watchlist": [], "manual_trades": [], "available_cash": 100_000}
    result = SERVER._execute_local_signals(state, refresh=False, automated=True)
    assert result["applied"] == []
    assert {item["reason_code"] for item in result["skipped"]} == {
        "signal_conflict",
        "human_review_required",
    }


def test_automated_execution_runs_clean_signal_without_confirmation(monkeypatch) -> None:
    signal = {
        "symbol": "000001",
        "name": "测试股票",
        "fusion": {"conflict_level": "none", "human_review_required": False},
        "execution_signal": {
            "signal_id": "auto-clean-signal",
            "symbol": "000001",
            "action": "buy",
            "price": 10,
            "quantity": 100,
        },
    }
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: [signal])
    state = {"positions": [], "watchlist": [], "manual_trades": [], "available_cash": 100_000}
    result = SERVER._execute_local_signals(state, refresh=False, automated=True)
    assert result["automated"] is True
    assert result["applied"][0]["source_signal_id"] == "auto-clean-signal"
    assert result["available_cash_after"] == 99_000


def _synthetic_frame(rows: int = 220) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="D")
    close = np.linspace(10.0, 20.0, rows) + np.sin(np.arange(rows))
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0},
        index=index,
    )


def test_dataset_bundle_versions_and_quality() -> None:
    bundle = build_dataset_bundle({"000001": _synthetic_frame()})
    assert bundle["snapshot"]["version"].startswith("pool-")
    assert bundle["data_quality"]["status"] == "valid"
    assert bundle["feature_version"]
    assert "triple_barrier_5d" in bundle["label_names"]


def test_quality_flags_descending_and_non_positive_close() -> None:
    frame = _synthetic_frame(4).iloc[::-1].copy()
    frame.iloc[0, frame.columns.get_loc("close")] = 0
    result = validate_data_quality({"x": frame})
    assert result["status"] == "invalid"
    assert "timestamps_not_ascending" in result["symbols"]["x"]["errors"]
    assert "non_positive_close" in result["symbols"]["x"]["errors"]


def test_strategy_dispatch_and_walk_forward() -> None:
    data = {"000001": _synthetic_frame()}
    assert simulate_strategy(data, strategy="momentum_regime")["status"] == "valid"
    result = walk_forward_optimize(
        data,
        {"fast_window": [3], "slow_window": [15], "momentum_window": [10]},
        train_bars=120,
        test_bars=30,
    )
    assert result["status"] == "valid"
    assert result["strategy"] == "trend_swing"


def test_p3_optimization_and_robustness_metrics(tmp_path: Path) -> None:
    data = {"000001": _synthetic_frame()}
    result = multi_objective_optimize(
        data,
        {"fast_window": [3, 5], "slow_window": [15], "momentum_window": [10]},
        max_trials=4,
        storage_path=tmp_path / "optuna.sqlite3",
        study_name="resume-test",
    )
    assert result["status"] == "valid"
    assert result["optimizer"] == "optuna_tpe_median_pruner"
    assert result["pareto_count"] >= 1
    resumed = multi_objective_optimize(
        data,
        {"fast_window": [3, 5], "slow_window": [15], "momentum_window": [10]},
        max_trials=4,
        storage_path=tmp_path / "optuna.sqlite3",
        study_name="resume-test",
    )
    assert resumed["resumed"] is True
    assert resumed["completed_before"] >= 4
    purged = purged_walk_forward_optimize(
        data,
        {"fast_window": [3], "slow_window": [15], "momentum_window": [10]},
        train_bars=120,
        test_bars=30,
    )
    assert purged["validation"]["method"] == "combinatorial_purged_cross_validation"
    assert purged["validation"]["combination_count"] == 10
    assert any(fold["purged_bar_count"] > 0 for fold in purged["folds"])
    assert any(fold["embargoed_bar_count"] > 0 for fold in purged["folds"])
    assert deflated_sharpe_ratio(1.0, 10) < 1.0
    assert probability_of_backtest_overfitting([1.0, 0.5, -0.2]) is not None


def test_cpcv_generates_every_test_group_combination() -> None:
    result = combinatorial_purged_cross_validation(
        {"000001": _synthetic_frame(240)},
        {"fast_window": [3], "slow_window": [15], "momentum_window": [10]},
        n_splits=6,
        n_test_splits=2,
        purge_bars=5,
        embargo_bars=2,
        label_horizon_bars=5,
    )
    assert result["status"] == "valid"
    assert len(result["folds"]) == 15
    assert result["summary"]["path_count"] == 5
    assert len({tuple(fold["test_groups"]) for fold in result["folds"]}) == 15


def test_limit_up_candidate_does_not_debit_cash_without_fill() -> None:
    dates = pd.date_range("2025-01-01", periods=45, freq="D", tz="UTC")
    close = np.array([10.0 * (1.10**index) for index in range(len(dates))])
    frame = pd.DataFrame(
        {
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(len(dates), 1_000_000.0),
        },
        index=dates,
    )
    result = simulate_trend_swing(
        {"000001": frame},
        fast_window=3,
        slow_window=10,
        momentum_window=5,
    )
    assert result["trades"] == []
    assert result["summary"]["final_equity"] == pytest.approx(100_000.0)
    assert result["summary"]["total_return_pct"] == pytest.approx(0.0)
