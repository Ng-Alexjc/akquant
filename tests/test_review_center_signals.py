"""Review-center selection, prediction and trigger integration tests."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from types import ModuleType


def _load_server() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "review_center_server.py"
    spec = importlib.util.spec_from_file_location("review_center_server_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = _load_server()


def _series(direction: float, count: int = 240) -> dict:
    candles = []
    volume = []
    price = 30.0
    for index in range(count):
        # Alternating noise keeps both target classes present while direction
        # determines the medium-term trend and selection score.
        noise = 0.004 if index % 3 else -0.006
        price *= 1.0 + direction * 0.0015 + noise
        timestamp = f"2025-{(index // 28) % 12 + 1:02d}-{index % 28 + 1:02d}"
        candles.append(
            {
                "time": timestamp,
                "open": price * 0.998,
                "high": price * 1.012,
                "low": price * 0.988,
                "close": price,
            }
        )
        volume.append(
            {
                "time": timestamp,
                "value": 100_000.0 + index * 100.0,
                "up": price >= candles[-1]["open"],
            }
        )
    return {"symbol": "TEST", "candles": candles, "volume": volume, "markers": []}


def test_analyze_series_ranks_uptrend_above_downtrend() -> None:
    uptrend = SERVER._analyze_series(_series(1.0))
    downtrend = SERVER._analyze_series(_series(-1.0))

    assert uptrend["available"] is True
    assert downtrend["available"] is True
    assert uptrend["selection_score"] > downtrend["selection_score"]
    assert 0.0 <= uptrend["up_probability"] <= 1.0
    assert math.isfinite(uptrend["up_probability"])
    assert uptrend["training_samples"] >= 100
    assert uptrend["resistance_price"] > uptrend["close"]
    assert uptrend["support_price"] < uptrend["close"]
    assert 0.0 <= uptrend["mfi14"] <= 100.0
    assert uptrend["mfi_filter"]["regime"] in {
        "normal_trend",
        "main_rise",
        "limit_up_chain",
    }


def test_swing_score_prioritizes_five_day_probability_over_raw_technical_score() -> None:
    traditional = SERVER._llm_submodule("traditional")
    high_technical_weak_probabilities = traditional.swing_composite_score(
        72.0, 0.45, 0.45
    )
    earlier_swing_setup = traditional.swing_composite_score(62.0, 0.58, 0.68)
    five_day_strength = traditional.swing_composite_score(60.0, 0.50, 0.70)
    one_day_spike = traditional.swing_composite_score(60.0, 0.70, 0.50)

    assert earlier_swing_setup > high_technical_weak_probabilities
    assert five_day_strength > one_day_spike


def test_mfi_thresholds_preserve_main_rise_and_limit_up_strength() -> None:
    assert SERVER._mfi_filter(29.9, "normal_trend")["passed"] is False
    assert SERVER._mfi_filter(50.0, "normal_trend")["passed"] is True
    assert SERVER._mfi_filter(85.1, "normal_trend")["passed"] is False
    assert SERVER._mfi_filter(90.0, "main_rise")["passed"] is True
    assert SERVER._mfi_filter(90.0, "limit_up_chain")["passed"] is True
    assert SERVER._mfi_filter(59.9, "limit_up_chain")["passed"] is False


def test_mfi_failure_downgrades_new_entry_to_waiting() -> None:
    state = {
        "watchlist": [{"symbol": "HOT"}],
        "positions": [],
        "manual_trades": [],
    }
    quote = {
        "symbol": "HOT",
        "name": "普通趋势过热票",
        "current_price": 120.0,
        "analysis": {
            "available": True,
            "as_of": "2026-08-31",
            "close": 120.0,
            "ma5": 118.0,
            "ma20": 110.0,
            "ma60": 100.0,
            "momentum20": 0.10,
            "atr14": 2.0,
            "selection_score": 77.0,
            "up_probability": 0.66,
            "probabilities": {
                "next_trading_day": {"value": 0.66},
                "next_5_trading_days": {"value": 0.68},
            },
            "trend": "多头排列",
            "trend_direction": "上升",
            "mfi14": 90.0,
            "mfi_filter": SERVER._mfi_filter(90.0, "normal_trend"),
        },
    }
    signal = SERVER._pool_signals(state, [quote])[0]
    assert signal["action"] == "等待买入"
    assert signal["execution_signal"] is None
    assert "MFI" in signal["trigger"]


def test_pool_signals_rank_candidates_and_trigger_buy_sell() -> None:
    state = {
        "watchlist": [
            {"symbol": "UP"},
            {"symbol": "CANDIDATE"},
            {"symbol": "IDLE"},
        ],
        "positions": [{"symbol": "DOWN", "quantity": 100.0, "entry_price": 100.0}],
        "manual_trades": [],
    }
    quotes = [
        {
            "symbol": "UP",
            "name": "上涨候选",
            "current_price": 120.0,
            "analysis": {
                "available": True,
                "as_of": "2026-08-24",
                "close": 120.0,
                "ma5": 115.0,
                "ma20": 110.0,
                "ma60": 100.0,
                "momentum20": 0.12,
                "atr14": 2.0,
                "selection_score": 82.0,
                "up_probability": 0.66,
                "probabilities": {
                    "next_trading_day": {"value": 0.66},
                    "next_5_trading_days": {"value": 0.70},
                },
                "validation_accuracy": 0.58,
                "trend": "多头排列",
                "trend_direction": "上升",
            },
        },
        {
            "symbol": "DOWN",
            "name": "下跌持仓",
            "current_price": 90.0,
            "analysis": {
                "available": True,
                "as_of": "2026-08-24",
                "close": 90.0,
                "ma5": 92.0,
                "ma20": 95.0,
                "ma60": 100.0,
                "momentum20": -0.10,
                "atr14": 2.0,
                "selection_score": 18.0,
                "up_probability": 0.30,
                "probabilities": {
                    "next_trading_day": {"value": 0.30},
                    "next_5_trading_days": {"value": 0.28},
                },
                "validation_accuracy": 0.55,
                "trend": "空头排列",
                "trend_direction": "下降",
            },
        },
        {
            "symbol": "OUTSIDE",
            "name": "非票池标的",
            "current_price": 50.0,
            "analysis": {
                "available": True,
                "close": 50.0,
                "ma5": 48.0,
                "ma20": 45.0,
                "ma60": 40.0,
                "momentum20": 0.2,
                "atr14": 1.0,
                "selection_score": 99.0,
                "up_probability": 0.9,
                "probabilities": {
                    "next_trading_day": {"value": 0.90},
                    "next_5_trading_days": {"value": 0.90},
                },
                "trend": "多头排列",
                "trend_direction": "上升",
            },
        },
        {
            "symbol": "CANDIDATE",
            "name": "候选关注股",
            "current_price": 50.0,
            "analysis": {
                "available": True,
                "close": 50.0,
                "ma5": 49.0,
                "ma20": 48.0,
                "ma60": 52.0,
                "momentum20": 0.03,
                "atr14": 1.0,
                "selection_score": 62.0,
                "up_probability": 0.52,
                "probabilities": {
                    "next_trading_day": {"value": 0.52},
                    "next_5_trading_days": {"value": 0.57},
                },
                "trend": "趋势混合",
                "trend_direction": "偏强",
            },
        },
        {
            "symbol": "IDLE",
            "name": "观望观察股",
            "current_price": 50.0,
            "analysis": {
                "available": True,
                "close": 50.0,
                "ma5": 49.0,
                "ma20": 48.0,
                "ma60": 52.0,
                "momentum20": 0.01,
                "atr14": 1.0,
                "selection_score": 55.0,
                "up_probability": 0.51,
                "probabilities": {
                    "next_trading_day": {"value": 0.51},
                    "next_5_trading_days": {"value": 0.50},
                },
                "trend": "趋势混合",
                "trend_direction": "震荡",
            },
        },
    ]

    signals = SERVER._pool_signals(state, quotes)
    by_symbol = {signal["symbol"]: signal for signal in signals}

    assert by_symbol["UP"]["selection_rank"] == 1
    assert by_symbol["UP"]["action"] == "买入"
    assert by_symbol["DOWN"]["action"] == "止损"
    assert by_symbol["CANDIDATE"]["action"] == "等待买入"
    assert by_symbol["CANDIDATE"]["execution_signal"] is None
    assert by_symbol["UP"]["execution_signal"]["action"] == "buy"
    assert by_symbol["DOWN"]["execution_signal"]["quantity"] == 100.0
    assert "OUTSIDE" not in by_symbol
    assert by_symbol["IDLE"]["action"] == "观察"
    assert by_symbol["UP"]["trend_direction"] == "上升"
    assert by_symbol["UP"]["suggested_price"] == 115.0
    assert by_symbol["UP"]["execution_signal"]["price"] == 115.0
    assert "建议买入价 115.00" in by_symbol["UP"]["evaluation"]
    assert "建议卖出价 90.00" in by_symbol["DOWN"]["evaluation"]
    assert by_symbol["DOWN"]["stop_price"] < by_symbol["DOWN"]["suggested_price"]
    assert by_symbol["UP"]["take_profit_price"] > by_symbol["UP"]["suggested_price"]
    assert signals[0]["action"] == "止损"
