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
    pareto_front,
    prepare_simulation_series,
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
        "mfi14",
    ):
        assert key in result
        assert isinstance(result[key], float)


def test_local_sector_fallback_keeps_pool_grouped_when_provider_omits_industry() -> None:
    assert SERVER._local_sector_name("603629", "利通电子") == "消费电子"
    assert SERVER._local_sector_name("600547", "山东黄金") == "贵金属"


def test_research_action_rejects_unknown_action() -> None:
    try:
        SERVER._run_research_action("unknown", {"positions": [], "watchlist": []})
    except ValueError as exc:
        assert "不支持的研究动作" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown research action should fail")


def test_pareto_front_prefers_drawdown_closer_to_zero() -> None:
    front = pareto_front(
        [
            {
                "name": "lower_drawdown",
                "sharpe_ratio": 2.0,
                "total_return_pct": 20.0,
                "max_drawdown_pct": -5.0,
            },
            {
                "name": "higher_drawdown",
                "sharpe_ratio": 2.0,
                "total_return_pct": 20.0,
                "max_drawdown_pct": -10.0,
            },
        ]
    )

    assert [item["name"] for item in front] == ["lower_drawdown"]


def test_strategy_distinctness_rejects_identical_optimized_trade_paths() -> None:
    identical = {
        "trades": [
            {"time": "2026-01-01", "symbol": "000001", "action": "buy"},
            {"time": "2026-01-05", "symbol": "000001", "action": "sell"},
        ]
    }
    distinct = {
        "trades": [
            {"time": "2026-02-01", "symbol": "000002", "action": "buy"},
            {"time": "2026-02-08", "symbol": "000002", "action": "sell"},
        ]
    }

    audit = SERVER._strategy_distinctness_audit(
        {
            "trend_swing": identical,
            "breakout_pullback": identical,
            "momentum_regime": distinct,
        }
    )

    assert audit["trend_swing"]["passed"] is False
    assert audit["breakout_pullback"]["max_peer_trade_overlap"] == 1.0
    assert audit["momentum_regime"]["passed"] is True


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


def test_local_execution_limits_portfolio_to_four_and_buys_by_priority(monkeypatch) -> None:
    signals = []
    for symbol, score, probability, sector_strength in (
        ("000001", 70, 0.60, "弱"),
        ("000002", 95, 0.75, "强"),
        ("000003", 80, 0.70, "中"),
    ):
        signals.append(
            {
                "symbol": symbol,
                "name": symbol,
                "selection_score": score,
                "up_probability": probability,
                "sector_strength": sector_strength,
                "execution_signal": {
                    "signal_id": f"limit-{symbol}",
                    "symbol": symbol,
                    "action": "buy",
                    "price": 10,
                    "quantity": 100,
                },
            }
        )
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: signals)
    state = {
        "positions": [
            {"symbol": "600001", "quantity": 100, "entry_price": 10},
            {"symbol": "600002", "quantity": 100, "entry_price": 10},
        ],
        "watchlist": [],
        "manual_trades": [],
        "available_cash": 100_000,
    }
    result = SERVER._execute_local_signals(state, refresh=False)
    assert [item["symbol"] for item in result["applied"]] == ["000002", "000003"]
    assert len(state["positions"]) == 4
    assert result["position_count"] == 4
    assert result["max_positions"] == 4
    assert any(
        item["symbol"] == "000001" and item["reason_code"] == "position_limit"
        for item in result["skipped"]
    )


def test_local_execution_sells_weakest_first_then_buys_strongest(monkeypatch) -> None:
    signals = [
        {
            "symbol": "000002",
            "selection_score": 80,
            "up_probability": 0.55,
            "sector_strength": "中",
            "execution_signal": {"signal_id": "sell-stronger", "symbol": "000002", "action": "sell", "price": 10, "quantity": 100},
        },
        {
            "symbol": "000001",
            "selection_score": 40,
            "up_probability": 0.30,
            "sector_strength": "弱",
            "execution_signal": {"signal_id": "sell-weaker", "symbol": "000001", "action": "sell", "price": 10, "quantity": 100},
        },
        {
            "symbol": "000003",
            "selection_score": 90,
            "up_probability": 0.80,
            "sector_strength": "强",
            "execution_signal": {"signal_id": "buy-strong", "symbol": "000003", "action": "buy", "price": 10, "quantity": 100},
        },
    ]
    monkeypatch.setattr(SERVER, "_research_pool_items", lambda state, refresh=True: [])
    monkeypatch.setattr(SERVER, "_pool_signals", lambda state, rendered: signals)
    state = {
        "positions": [
            {"symbol": "000001", "quantity": 100, "entry_price": 10},
            {"symbol": "000002", "quantity": 100, "entry_price": 10},
        ],
        "watchlist": [],
        "manual_trades": [],
        "available_cash": 100_000,
    }
    result = SERVER._execute_local_signals(state, refresh=False)
    assert [(item["action"], item["symbol"]) for item in result["applied"]] == [
        ("sell", "000001"),
        ("sell", "000002"),
        ("buy", "000003"),
    ]


def test_published_model_gates_rule_buy_signal() -> None:
    candles = [
        {
            "time": f"2026-07-{index + 1:02d}",
            "open": 10.0 + index * 0.1,
            "high": 10.2 + index * 0.1,
            "low": 9.8 + index * 0.1,
            "close": 10.0 + index * 0.1,
        }
        for index in range(40)
    ]
    signal = {
        "symbol": "000001",
        "action": "买入",
        "updated_at": "2026-08-31",
        "mfi_filter": {"passed": True},
        "execution_signal": {
            "signal_id": "rule-buy",
            "symbol": "000001",
            "action": "buy",
            "price": 13.9,
            "quantity": 100,
        },
    }
    model = {
        "model_id": "champion-v4",
        "model_name": "momentum_regime",
        "version": "v4",
        "params": {
            "fast_window": 3,
            "slow_window": 30,
            "momentum_window": 20,
            "volatility_cap": 0.10,
            "max_entry_momentum": 0.01,
        },
    }

    result = SERVER._active_model_signals(
        [signal],
        [{"symbol": "000001", "_ai_series": {"candles": candles}}],
        {"positions": []},
        model,
    )

    assert result[0]["execution_signal"] is None
    assert result[0]["action"] == "等待买入"
    assert result[0]["active_model"]["entry_allowed"] is False


def test_published_model_can_create_exit_signal() -> None:
    closes = [20.0 + index * 0.05 for index in range(25)] + [
        21.0 - index * 0.35 for index in range(15)
    ]
    candles = [
        {
            "time": f"2026-07-{index + 1:02d}",
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]
    signal = {
        "symbol": "000001",
        "action": "持有",
        "updated_at": "2026-08-31",
        "atr14": 0.5,
        "mfi_filter": {"passed": True},
        "execution_signal": None,
    }
    state = {
        "positions": [{"symbol": "000001", "quantity": 300, "entry_price": 20}]
    }
    model = {
        "model_id": "champion-v4",
        "model_name": "momentum_regime",
        "version": "v4",
        "params": {
            "fast_window": 3,
            "slow_window": 30,
            "momentum_window": 20,
            "volatility_cap": 0.10,
            "momentum_exit_threshold": -0.01,
        },
    }

    result = SERVER._active_model_signals(
        [signal],
        [{"symbol": "000001", "_ai_series": {"candles": candles}}],
        state,
        model,
    )

    assert result[0]["execution_signal"]["action"] == "sell"
    assert result[0]["execution_signal"]["quantity"] == 300
    assert result[0]["active_model"]["exit_required"] is True


def test_daily_trade_action_never_runs_research_or_agent(monkeypatch) -> None:
    today = SERVER.datetime.now().date().isoformat()
    events = []
    refreshed_items = [{"symbol": "000001", "analysis": {"as_of": today}}]
    model = {
        "model_id": "champion-v4",
        "model_name": "momentum_regime",
        "version": "v4",
        "params": {},
    }
    def active_model():
        events.append("model")
        return model

    monkeypatch.setattr(SERVER, "_active_champion_runtime_policy", active_model)
    monkeypatch.setattr(
        SERVER,
        "_research_pool_items",
        lambda state, refresh=True: (
            events.append(f"refresh:{refresh}") or refreshed_items
        ),
    )
    monkeypatch.setattr(
        SERVER,
        "_run_research_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("daily trade must not run research")
        ),
    )
    def execute_signals(*args, **kwargs):
        events.append("execute")
        assert kwargs["refresh"] is False
        assert kwargs["rendered"] is refreshed_items
        return {
            "status": "completed",
            "applied": [],
            "skipped": [],
            "research_performed": False,
            "model_changed": False,
            "llm_agent_called": False,
        }

    monkeypatch.setattr(SERVER, "_execute_local_signals", execute_signals)

    result = SERVER._run_daily_trade_action(
        {
            "positions": [],
            "watchlist": [{"symbol": "000001"}],
            "manual_trades": [],
        },
        refresh=False,
    )

    assert result["status"] == "completed"
    assert events == ["refresh:True", "model", "execute"]
    assert result["pool_refresh_performed"] is True
    assert result["pool_refresh_forced"] is True
    assert result["pool_refresh_requested"] is False
    assert result["pool_refresh_status"] == "completed"
    assert result["pool_refresh_symbol_count"] == 1
    assert result["pool_refresh_success_count"] == 1
    assert result["pool_refresh_errors"] == {}
    assert result["pool_refresh_dates"] == {"000001": today}
    assert result["mode"] == "local_paper_trade_only"
    assert result["research_performed"] is False
    assert result["model_changed"] is False
    assert result["llm_agent_called"] is False


def test_daily_trade_action_aborts_before_model_when_pool_refresh_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        SERVER,
        "_research_pool_items",
        lambda state, refresh=True: [
            {"symbol": "000001", "quote_error": "market source unavailable"}
        ],
    )
    monkeypatch.setattr(
        SERVER,
        "_active_champion_runtime_policy",
        lambda: (_ for _ in ()).throw(
            AssertionError("model must not be read before a successful pool refresh")
        ),
    )
    monkeypatch.setattr(
        SERVER,
        "_execute_local_signals",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed pool refresh must not execute signals")
        ),
    )

    result = SERVER._run_daily_trade_action(
        {
            "positions": [{"symbol": "000001", "quantity": 100}],
            "watchlist": [],
            "manual_trades": [],
        }
    )

    assert result["status"] == "failed"
    assert result["pool_refresh_status"] == "failed"
    assert result["pool_refresh_symbol_count"] == 1
    assert result["pool_refresh_success_count"] == 0
    assert result["pool_refresh_errors"] == {
        "000001": "market source unavailable"
    }
    assert result["applied"] == []


def test_server_native_scheduler_records_once_without_agent(tmp_path, monkeypatch) -> None:
    class FakeStorage:
        def __init__(self):
            self.items = []

        def active_champion(self):
            return {
                "model_id": "champion-v4",
                "model_name": "momentum_regime",
                "role": "champion",
                "status": "active_paper",
                "version": "v4",
                "published_at": "2026-08-31T12:00:00+00:00",
                "metrics": {"best": {"params": {}}},
            }

        def list_automation_runs(self, limit=20):
            return list(self.items)

        def create_automation_run(self, action, params):
            self.items.insert(
                0,
                {
                    "run_id": "automation-1",
                    "action": action,
                    "status": "running",
                    "params": params,
                },
            )
            return "automation-1"

        def finish_automation_run(self, run_id, *, status, result=None, error=None):
            self.items[0].update(status=status, result=result, error=error)

    class FakeService:
        def __init__(self, storage):
            self.storage = storage

    storage = FakeStorage()
    trade_refresh_values = []
    monkeypatch.setattr(SERVER, "_get_ai_service", lambda: FakeService(storage))

    def run_daily_trade(state, refresh=True):
        trade_refresh_values.append(refresh)
        return {
            "status": "completed",
            "applied": [],
            "research_performed": False,
            "model_changed": False,
            "llm_agent_called": False,
        }

    monkeypatch.setattr(
        SERVER,
        "_run_daily_trade_action",
        run_daily_trade,
    )
    (tmp_path / SERVER.STATE_FILE).write_text(
        '{"positions": [], "watchlist": [], "manual_trades": []}',
        encoding="utf-8",
    )

    first = SERVER._execute_scheduled_daily_trade(tmp_path, "2026-08-31")
    second = SERVER._execute_scheduled_daily_trade(tmp_path, "2026-08-31")

    assert first["status"] == "completed"
    assert second["status"] == "deduplicated"
    assert len(storage.items) == 1
    assert storage.items[0]["params"]["trigger"] == (
        "review_center_server_native_scheduler"
    )
    assert storage.items[0]["params"]["llm_agent_called"] is False
    assert storage.items[0]["params"]["refresh_forced"] is True
    assert trade_refresh_values == [True]


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
    assert "mfi14" in bundle["feature_names"]
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


def test_momentum_regime_v4_accepts_risk_controls_and_caps_positions() -> None:
    data = {
        f"00000{index}": _synthetic_frame()
        for index in range(1, 7)
    }
    result = simulate_strategy(
        data,
        strategy="momentum_regime",
        max_positions=4,
        volatility_cap=0.08,
        momentum_vol_weight=1.0,
        trailing_atr_multiple=2.0,
        max_holding_bars=15,
        momentum_exit_threshold=-0.01,
        max_entry_momentum=0.50,
    )
    assert result["status"] == "valid"
    assert max(item["position_count"] for item in result["equity_curve"]) <= 4


def test_momentum_regime_has_distinct_entry_path_from_trend_swing() -> None:
    data = {"000001": _synthetic_frame(180), "000002": _synthetic_frame(180)}
    common = {
        "fast_window": 3,
        "slow_window": 15,
        "momentum_window": 10,
        "volatility_cap": 0.20,
        "max_entry_momentum": 1.0,
    }
    trend = simulate_strategy(data, strategy="trend_swing", **common)
    momentum = simulate_strategy(
        data,
        strategy="momentum_regime",
        regime_window=5,
        regime_momentum_threshold=0.08,
        regime_acceleration_threshold=0.05,
        min_market_breadth=0.80,
        **common,
    )
    trend_events = {
        (item["time"], item["symbol"], item["action"])
        for item in trend["trades"]
    }
    momentum_events = {
        (item["time"], item["symbol"], item["action"])
        for item in momentum["trades"]
    }

    assert trend_events
    assert trend_events != momentum_events


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


def test_prepared_simulation_series_matches_normal_execution() -> None:
    data = {"000001": _synthetic_frame(180), "000002": _synthetic_frame(180)}
    params = {
        "strategy": "momentum_regime",
        "fast_window": 3,
        "slow_window": 15,
        "momentum_window": 10,
    }
    expected = simulate_strategy(data, **params)
    prepared = prepare_simulation_series(data)
    actual = simulate_strategy(prepared, _prepared=True, **params)

    assert actual["summary"] == expected["summary"]
    assert actual["trades"] == expected["trades"]
    assert actual["equity_curve"] == expected["equity_curve"]


def test_review_baseline_never_exceeds_local_position_limit(monkeypatch) -> None:
    dates = pd.date_range("2026-01-01", periods=80, freq="D", tz="UTC")

    def fake_kline(symbol: str):
        offset = int(symbol[-1]) * 0.001
        candles = []
        price = 10.0
        for index, date in enumerate(dates):
            price *= 1.002 + offset
            candles.append(
                {
                    "time": date.date().isoformat(),
                    "open": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "close": price,
                }
            )
        return {"series": {"candles": candles}}

    monkeypatch.setattr(SERVER, "stock_kline", fake_kline)
    state = {
        "positions": [],
        "watchlist": [
            {"symbol": f"00000{index}", "name": f"stock-{index}"}
            for index in range(1, 7)
        ],
    }

    result = SERVER._review_baseline_backtest(state, calendar_days=60)

    assert result["assumptions"]["max_positions"] == 4
    assert max(point["position_count"] for point in result["equity_curve"]) <= 4


def test_backtest_dashboard_compacts_research_models_and_selection() -> None:
    class FakeStorage:
        def list_research_runs(self, limit: int = 20):
            return [
                {
                    "run_id": "research-test",
                    "action": "full",
                    "status": "completed",
                    "started_at": "2026-08-30T10:00:00+08:00",
                    "finished_at": "2026-08-30T10:05:00+08:00",
                    "result": {
                        "dataset": {
                            "snapshot": {"version": "pool-test", "row_count": 120, "start": "2026-01-01", "end": "2026-08-29"},
                            "data_quality": {"status": "valid", "symbol_count": 1, "total_errors": 0, "total_warnings": 0},
                            "feature_names": ["ret_5", "ma20"],
                            "label_names": ["up_5d"],
                        },
                        "backtest": {
                            "status": "valid",
                            "summary": {"total_return_pct": -1.0, "sharpe_ratio": -0.2},
                            "equity_curve": [{"time": "2026-08-29", "value": 99_000, "drawdown_pct": -1.0}],
                            "trades": [
                                {"time": "2026-08-20", "symbol": "000001", "action": "sell", "price": 11, "quantity": 100, "net_pnl": 100}
                            ],
                        },
                        "strategies": {"trend_swing": {"summary": {"sharpe_ratio": 1.2, "total_return_pct": 20.0}}},
                        "walk_forward": {"trend_swing": {"summary": {"total_return_pct": 2.0}}},
                    },
                }
            ]

        def list_models(self):
            return [
                {
                    "model_id": "challenger-test",
                    "model_name": "trend_swing",
                    "role": "challenger",
                    "status": "evaluated",
                    "version": "pool-test",
                    "metrics": {
                        "best": {"params": {"fast_window": 5}, "sharpe_ratio": 1.8, "total_return_pct": 30.0, "trade_count": 12},
                        "completed_trial_count": 12,
                        "pruned_trial_count": 3,
                        "cpcv": {"summary": {"mean_test_sharpe": 1.1, "positive_test_fold_ratio": 0.8}},
                        "pbo": 0.2,
                        "simulator_version": "v2",
                    },
                },
                {
                    "model_id": "challenger-test-old",
                    "model_name": "trend_swing",
                    "role": "challenger",
                    "status": "evaluated",
                    "version": "pool-old",
                    "metrics": {
                        "best": {
                            "params": {"fast_window": 20},
                            "sharpe_ratio": 0.2,
                            "total_return_pct": 1.0,
                        }
                    },
                },
            ]

        def active_champion(self):
            return {"model_id": "champion-test", "model_name": "trend_swing", "metrics": {"simulator_version": "v2"}}

        def evaluate_release_gate(self, model_id: str):
            return {"passed": model_id == "challenger-test", "checks": []}

        def performance_report(self):
            return {"next_trading_day": {"status": "valid", "sample_count": 20}}

        def training_dataset_summary(self):
            return {"status": "insufficient_data", "samples": {"next_trading_day": 20}}

    payload = SERVER._backtest_dashboard_payload(
        {"positions": [], "watchlist": [], "manual_trades": []},
        {
            "available_cash": 100_000,
            "positions": [],
            "watchlist": [{"symbol": "000001", "name": "测试股", "sector_name": "测试板块"}],
            "signals": [
                {
                    "symbol": "000001",
                    "name": "测试股",
                    "pool": "观察",
                    "action": "买入",
                    "selection_rank": 1,
                    "selection_score": 88,
                    "up_probability": 0.62,
                    "validation_accuracy": 0.58,
                    "current_price": 10,
                    "stop_price": 9,
                    "take_profit_price": 12,
                    "execution_signal": {"action": "buy"},
                }
            ],
        },
        storage=FakeStorage(),
        ai_status={"enabled": True, "ready": False, "provider": "test"},
    )
    assert payload["status"] == "ready"
    assert payload["verdict"]["best_robust_strategy"] == "trend_swing"
    assert payload["strategies"][0]["model"]["release_gate_passed"] is True
    assert payload["strategies"][0]["model"]["model_id"] == "challenger-test"
    assert payload["strategies"][0]["model"]["version"] == "pool-test"
    assert payload["selection"]["signals"][0]["risk_reward_ratio"] == pytest.approx(2.0)
    assert payload["selection"]["sector_strength"][0]["sector"] == "测试板块"
    assert payload["backtest"]["trade_metrics"]["expectancy"] == pytest.approx(100.0)
    assert payload["backtest"]["trades"][0]["name"] == "测试股"
    assert any(item["title"] == "LLM 尚未就绪" for item in payload["verdict"]["alerts"])


def test_historical_probability_replay_uses_only_observable_labels() -> None:
    traditional = SERVER._llm_submodule("traditional")
    closes = [100.0]
    for index in range(1, 150):
        closes.append(closes[-1] * (1.012 if index % 7 in {0, 1, 2} else 0.994))
    volumes = [1_000_000.0 + (index % 11) * 25_000 for index in range(150)]

    original = traditional.historical_probability_rows(
        closes,
        volumes,
        [120],
    )[120]
    changed_future = list(closes)
    changed_future[121:] = [value * 3.0 for value in changed_future[121:]]
    replayed = traditional.historical_probability_rows(
        changed_future,
        volumes,
        [120],
    )[120]

    assert original["next_trading_day"]["value"] == pytest.approx(
        replayed["next_trading_day"]["value"]
    )
    assert original["next_5_trading_days"]["value"] == pytest.approx(
        replayed["next_5_trading_days"]["value"]
    )
    assert (
        original["next_5_trading_days"]["validation"]["label_observation_rule"]
        == "feature_end + horizon <= prediction_bar"
    )
