"""本地复盘中心 HTTP 服务.

提供静态复盘页面之外的最小本地 API:

* ``/api/stocks/search``: 查询 A 股代码/名称
* ``/api/stocks/kline``: 获取日线 K 线
* ``/api/pools``: 读取票池并刷新行情
* ``/api/watchlist`` / ``/api/positions``: 票池增删改

服务默认只监听 127.0.0.1,状态落在当前工作目录的 JSON 文件中。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from research_pipeline import (  # noqa: E402
    SIMULATOR_VERSION,
    build_dataset_bundle,
    deflated_sharpe_ratio,
    multi_objective_optimize,
    persist_artifact,
    probability_of_backtest_overfitting,
    purged_walk_forward_optimize,
    simulate_strategy,
    walk_forward_optimize,
)

EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
EASTMONEY_TRENDS = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
STATE_FILE = ".review_center_state.json"
POOL_CACHE_FILE = ".review_center_pool_cache.json"
BACKTEST_CACHE_FILE = ".review_center_backtest_cache.json"
POOL_CACHE_VERSION = 4
MODEL_ENSEMBLE_VERSION = "short_swing_ensemble_v1"
MODEL_ENSEMBLE_WEIGHTS = {
    # Trend is the non-negotiable style gate; the other two models confirm
    # timing and market state.  This keeps the ensemble short/ultra-short
    # trend-following instead of drifting into medium/long-term selection.
    "trend_swing": 0.40,
    "breakout_pullback": 0.30,
    "momentum_regime": 0.30,
}
MODEL_ENSEMBLE_ENTRY_THRESHOLD = 0.60
REVIEW_INITIAL_EQUITY = 100000.0
KLINE_HISTORY_DAYS = 360
QUOTE_CACHE_TTL_SECONDS = 180.0
SIGNAL_MODEL_MIN_BARS = 90
SIGNAL_MODEL_MAX_SAMPLES = 360
WATCH_SCORE_THRESHOLD = 60.0
WATCH_PROBABILITY_THRESHOLD = 0.50
BUY_SCORE_THRESHOLD = 65.0
BUY_PROBABILITY_THRESHOLD = 0.54
STRONG_BUY_SCORE_THRESHOLD = 72.0
STRONG_BUY_PROBABILITY_THRESHOLD = 0.58
ADD_SCORE_THRESHOLD = 75.0
ADD_PROBABILITY_THRESHOLD = 0.60
SWING_WATCH_SCORE_THRESHOLD = 58.0
SWING_WATCH_FIVE_DAY_PROBABILITY_THRESHOLD = 0.52
SWING_BUY_SCORE_THRESHOLD = 62.0
SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD = 0.56
SWING_STRONG_BUY_SCORE_THRESHOLD = 68.0
SWING_STRONG_FIVE_DAY_PROBABILITY_THRESHOLD = 0.60
SWING_ADD_SCORE_THRESHOLD = 70.0
SWING_ADD_FIVE_DAY_PROBABILITY_THRESHOLD = 0.62
MFI_PERIOD = 14
MFI_NORMAL_MIN = 30.0
MFI_NORMAL_MAX = 85.0
MFI_MAIN_RISE_MIN = 35.0
MFI_LIMIT_UP_MIN = 60.0
_DIRECT_OPENER = build_opener(ProxyHandler({}))
_STOCK_KLINE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STOCK_KLINE_CACHE_LOCK = RLock()
_DAILY_TRADE_SCHEDULER_LOCK = RLock()
_MARKET_INDEX_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_MARKET_INDEX_CACHE_LOCK = RLock()
_AI_SERVICE: Any | None = None
_AI_SERVICE_LOCK = RLock()
_MIAOXIANG_SECTOR_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MIAOXIANG_SECTOR_CACHE_LOCK = RLock()
_MARKET_BREADTH_CACHE: tuple[float, dict[str, Any]] | None = None
_MARKET_BREADTH_CACHE_LOCK = RLock()
_MIAOXIANG_MARKET_CACHE: tuple[float, dict[str, Any]] | None = None
_MIAOXIANG_MARKET_CACHE_LOCK = RLock()
_MIAOXIANG_STOCK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MIAOXIANG_STOCK_CACHE_LOCK = RLock()
_MIAOXIANG_EVENTS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_MIAOXIANG_EVENTS_CACHE_LOCK = RLock()


def _traditional_rule_snapshot() -> dict[str, Any]:
    """Versioned threshold/rule snapshot passed verbatim to the LLM."""
    return {
        "strategy_id": "review_center_momentum_logit_v1",
        "strategy_version": "review_center_momentum_logit_mfi_swing_v4",
        "threshold_version": "review_center_thresholds_2026-09-01_swing_probability",
        "swing_score": {
            "base_weights": {
                "technical_score": 0.45,
                "next_trading_day_probability": 0.20,
                "next_5_trading_days_probability": 0.35,
            },
            "reliability_adjustment": "概率权重按各周期样本外 AUC/Brier/accuracy 折减后重新归一化",
        },
        "thresholds": {
            "watch": {"swing_score_min": SWING_WATCH_SCORE_THRESHOLD, "next_day_probability_min": 0.49, "five_day_probability_min": SWING_WATCH_FIVE_DAY_PROBABILITY_THRESHOLD},
            "buy": {"swing_score_min": SWING_BUY_SCORE_THRESHOLD, "next_day_probability_min": 0.52, "five_day_probability_min": SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD, "price_above": "MA20"},
            "strong_buy": {"swing_score_min": SWING_STRONG_BUY_SCORE_THRESHOLD, "next_day_probability_min": 0.56, "five_day_probability_min": SWING_STRONG_FIVE_DAY_PROBABILITY_THRESHOLD, "price_relation": "close > MA20 > MA60"},
            "add": {"swing_score_min": SWING_ADD_SCORE_THRESHOLD, "next_day_probability_min": 0.58, "five_day_probability_min": SWING_ADD_FIVE_DAY_PROBABILITY_THRESHOLD, "price_relation": "close > MA20 > MA60"},
            "hard_stop": {"loss_pct": 0.08, "exit_ratio": 1.0},
            "mfi14": {
                "normal_trend": {"min": MFI_NORMAL_MIN, "max": MFI_NORMAL_MAX},
                "main_rise": {"min": MFI_MAIN_RISE_MIN, "max": None},
                "limit_up_chain": {"min": MFI_LIMIT_UP_MIN, "max": None},
                "rule": "普通趋势过滤过弱/过热资金流；主升与连板不使用传统 MFI>80 超买否决",
            },
        },
        "holding_rules": {
            "trend_exit": "close < MA60 and momentum20 < 0 => 清仓",
            "model_exit": "next_day_probability <= 0.40 and close < MA20 => 减仓",
            "ordinary_exit_ratio": "本地融合按浮盈和风险计算；止损/清仓固定 100%",
        },
        "price_plan_version": "short_swing_price_v1",
    }


def _traditional_action_summary(analysis: dict[str, Any], holding: bool) -> dict[str, Any]:
    """Expose the exact local threshold decision and its trigger to the LLM."""
    probability = _float(((analysis.get("probabilities") or {}).get("next_trading_day") or {}).get("value"), -1.0)
    five_day_probability = _float(((analysis.get("probabilities") or {}).get("next_5_trading_days") or {}).get("value"), -1.0)
    score = _float(analysis.get("swing_score"), _float(analysis.get("selection_score")))
    close, ma20, ma60 = _float(analysis.get("close")), _float(analysis.get("ma20")), _float(analysis.get("ma60"))
    momentum = _float(analysis.get("momentum20"))
    mfi_filter = dict(analysis.get("mfi_filter") or {})
    mfi_passed = bool(mfi_filter.get("passed", True))
    if holding and close < ma60 and momentum < 0:
        return {"action": "清仓", "trigger": "close < MA60 and momentum20 < 0"}
    if holding and probability >= 0 and probability <= 0.40 and five_day_probability <= 0.46 and close < ma20:
        return {"action": "减仓", "trigger": "1日与5日概率同步转弱且 close < MA20"}
    if holding and score >= SWING_ADD_SCORE_THRESHOLD and probability >= 0.58 and five_day_probability >= SWING_ADD_FIVE_DAY_PROBABILITY_THRESHOLD and close > ma20 > ma60 and mfi_passed:
        return {"action": "加仓", "trigger": "波段综合分、1日/5日概率 + close > MA20 > MA60"}
    if not holding and score >= SWING_STRONG_BUY_SCORE_THRESHOLD and probability >= 0.56 and five_day_probability >= SWING_STRONG_FIVE_DAY_PROBABILITY_THRESHOLD and close > ma20 > ma60 and mfi_passed:
        return {"action": "买入", "trigger": "强势波段阈值 + close > MA20 > MA60"}
    if not holding and score >= SWING_BUY_SCORE_THRESHOLD and probability >= 0.52 and five_day_probability >= SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD and close > ma20 and mfi_passed:
        return {"action": "买入", "trigger": "波段综合分与双周期概率达标 + close > MA20"}
    if not holding and not mfi_passed and score >= SWING_WATCH_SCORE_THRESHOLD and probability >= 0.49 and five_day_probability >= SWING_WATCH_FIVE_DAY_PROBABILITY_THRESHOLD:
        return {"action": "等待买入", "trigger": str(mfi_filter.get("reason") or "MFI filter not confirmed")}
    if not holding and score >= SWING_WATCH_SCORE_THRESHOLD and probability >= 0.49 and five_day_probability >= SWING_WATCH_FIVE_DAY_PROBABILITY_THRESHOLD:
        return {"action": "等待买入", "trigger": "波段综合分与双周期概率达到观察阈值"}
    return {"action": "持有" if holding else "观察", "trigger": "未触发更高优先级阈值"}


def _portfolio_snapshot(
    state: dict[str, Any], *, current_symbol: str, current_price: float
) -> dict[str, Any]:
    """Build an auditable local-account snapshot from the review ledger."""
    initial = REVIEW_INITIAL_EQUITY
    realized = sum(_float(item.get("net_pnl")) for item in state.get("manual_trades") or [])
    cash = _local_available_cash(state)
    market_value = 0.0
    unknown_positions = 0
    for entry in state.get("positions") or []:
        symbol = str(entry.get("symbol") or "")
        qty = _float(entry.get("quantity"))
        if symbol == current_symbol and current_price > 0:
            market_value += qty * current_price
        elif _float(entry.get("current_price")) > 0:
            market_value += qty * _float(entry.get("current_price"))
        else:
            unknown_positions += 1
    return {
        "initial_equity": initial,
        "available_cash": round(cash, 2),
        "cash": round(cash, 2),
        "market_value": round(market_value, 2),
        "total_assets": round(cash + market_value, 2) if unknown_positions == 0 else None,
        "realized_pnl": round(realized, 2),
        "status": "complete" if unknown_positions == 0 else "partial",
        "unknown_position_valuation_count": unknown_positions,
        "source": "local_review_center_state_and_manual_trades",
    }


def _local_available_cash(state: dict[str, Any]) -> float:
    """Return local paper cash, migrating legacy position-only state safely."""
    explicit = state.get("available_cash")
    if explicit is not None:
        return _float(explicit)
    realized = sum(
        _float(item.get("net_pnl")) for item in state.get("manual_trades") or []
    )
    invested_cost = sum(
        _float(item.get("entry_price")) * _float(item.get("quantity"))
        for item in state.get("positions") or []
    )
    return REVIEW_INITIAL_EQUITY + realized - invested_cost


def _load_llm_package() -> Any:
    """Load the source-tree LLM package without importing the native extension."""
    package_name = "_akquant_review_llm"
    if package_name in sys.modules:
        return sys.modules[package_name]
    package_dir = Path(__file__).resolve().parents[1] / "python" / "akquant" / "llm"
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("无法加载复盘中心 LLM 模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _llm_submodule(name: str) -> Any:
    import importlib

    _load_llm_package()
    return importlib.import_module(f"_akquant_review_llm.{name}")


def _get_ai_service() -> Any:
    global _AI_SERVICE
    with _AI_SERVICE_LOCK:
        if _AI_SERVICE is None:
            _AI_SERVICE = _load_llm_package().TradeAnalysisService()
        return _AI_SERVICE


def _float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _repair_text(value: Any, default: str = "") -> str:
    """Repair UTF-8 text decoded through a legacy Windows console code page."""
    text = str(value or default)
    try:
        repaired = text.encode("latin1").decode("utf-8")
        if any("\u4e00" <= char <= "\u9fff" for char in repaired):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mcp_json(result: Any) -> dict[str, Any] | None:
    """Extract the first JSON object returned by a 妙想 MCP tool."""
    for block in list(getattr(result, "content", []) or []):
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _parse_mcp_number(value: Any) -> float | None:
    text = _repair_text(value).replace(",", "").strip()
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier, text = 100000000.0, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 10000.0, text[:-1]
    text = text.replace("%", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return number * multiplier


def _mcp_table_pairs(payload: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    pairs: list[tuple[str, list[Any]]] = []
    for table in list(payload.get("data") or []):
        for row in list(table.get("items") or []):
            if isinstance(row, list) and len(row) >= 2:
                pairs.append((_repair_text(row[0]), list(row[1:])))
    return pairs


def _compact_miaoxiang_tables(
    payload: dict[str, Any], config: Any
) -> tuple[list[dict[str, Any]], bool]:
    """Keep a small, auditable subset of 妙想 tables for the LLM.

    妙想 schemas vary by tool, so columns are selected by semantic names rather
    than by a brittle fixed schema.  The original response is never forwarded.
    """
    max_tables = int(getattr(config, "stock_max_tables", 6))
    max_rows = int(getattr(config, "stock_max_rows", 8))
    max_value_chars = int(getattr(config, "stock_max_value_chars", 80))
    max_total_chars = int(getattr(config, "stock_max_total_chars", 5000))
    preferred_tokens = (
        "日期", "时间", "名称", "代码", "行业", "板块", "价格", "收盘", "涨跌",
        "成交", "换手", "市值", "市盈", "市净", "净流", "风险", "估值", "收益",
    )
    tables: list[dict[str, Any]] = []
    used_dates: set[str] = set()
    total_chars = 0
    truncated = False
    for raw_table in list(payload.get("data") or []):
        if len(tables) >= max_tables:
            truncated = True
            break
        if not isinstance(raw_table, dict):
            continue
        raw_columns = [_repair_text(v) for v in list(raw_table.get("columns") or [])]
        if not raw_columns:
            continue
        # Keep semantically useful fields; if a provider uses opaque columns,
        # retain only the first few rather than dropping the table entirely.
        selected = [
            idx for idx, col in enumerate(raw_columns)
            if any(token in col for token in preferred_tokens)
        ] or list(range(min(len(raw_columns), 8)))
        selected = selected[:8]
        columns = [raw_columns[idx] for idx in selected]
        items: list[list[str]] = []
        seen_rows: set[str] = set()
        for raw_row in list(raw_table.get("items") or []):
            if not isinstance(raw_row, list):
                continue
            values = []
            for idx in selected:
                value = _repair_text(raw_row[idx] if idx < len(raw_row) else "")
                values.append(value[:max_value_chars])
            row_key = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            # Avoid repeating historical rows with the same date/time.
            date_value = next(
                (values[pos] for pos, col in enumerate(columns)
                 if ("日期" in col or "时间" in col) and values[pos]),
                "",
            )
            if date_value and date_value in used_dates:
                continue
            if date_value:
                used_dates.add(date_value)
            projected = len(json.dumps(values, ensure_ascii=False))
            if total_chars + projected > max_total_chars:
                truncated = True
                break
            items.append(values)
            total_chars += projected
            if len(items) >= max_rows:
                if len(list(raw_table.get("items") or [])) > len(items):
                    truncated = True
                break
        if items:
            table = {"columns": columns, "items": items}
            tables.append(table)
            total_chars += len(json.dumps(columns, ensure_ascii=False))
        if total_chars >= max_total_chars:
            truncated = True
            break
    return tables, truncated


def _compact_miaoxiang_events(
    payload: dict[str, Any], config: Any, *, source: str
) -> tuple[list[dict[str, Any]], bool]:
    """Normalize and cap news/notice text before it reaches the prompt."""
    max_items = int(getattr(config, "news_max_items", 3))
    max_chars = int(getattr(config, "news_max_chars", 1200))
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(getattr(config, "news_days", 3)))
    rows: list[dict[str, Any]] = []
    raw_rows: list[Any] = []
    for table in list(payload.get("data") or []):
        if isinstance(table, dict):
            raw_rows.extend(list(table.get("items") or []))
    truncated = len(raw_rows) > max_items
    for raw in raw_rows:
        if len(rows) >= max_items:
            break
        if isinstance(raw, dict):
            values = {str(k): _repair_text(v) for k, v in raw.items()}
        elif isinstance(raw, list):
            values = {f"field_{idx}": _repair_text(value) for idx, value in enumerate(raw)}
        else:
            continue
        title = next((v for k, v in values.items() if any(t in k for t in ("标题", "题目", "title"))), "")
        published = next((v for k, v in values.items() if any(t in k for t in ("日期", "时间", "date", "time"))), "")
        try:
            parsed = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            if parsed and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed and parsed < cutoff:
                continue
        except (TypeError, ValueError):
            pass
        summary = next((v for k, v in values.items() if any(t in k for t in ("摘要", "内容", "正文", "summary", "content"))), "")
        rows.append({
            "title": title[:240],
            "published_at": published[:64],
            "source": source,
            "summary": summary[:max_chars],
            "risk_tags": [],
        })
    return rows, truncated


def _compact_core_performance(
    payload: dict[str, Any], config: Any
) -> dict[str, Any]:
    """Extract at most a few core/leader rows from a sector response."""
    tables: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for raw_table in list(payload.get("data") or []):
        if len(tables) >= 3 or not isinstance(raw_table, dict):
            truncated = True
            break
        columns = [_repair_text(v)[:80] for v in list(raw_table.get("columns") or [])]
        raw_items = list(raw_table.get("items") or [])
        text_blob = " ".join(columns + [_repair_text(v) for row in raw_items[:8] if isinstance(row, list) for v in row[:4]])
        has_stock_hint = any(token in text_blob for token in ("证券简称", "股票名称", "成分股", "核心股", "龙头股", "个股", "代码"))
        has_stock_code = bool(re.search(r"\b\d{6}\b|\.(?:SH|SZ)\b", text_blob, flags=re.IGNORECASE))
        if not (has_stock_hint or has_stock_code):
            continue
        if "BOLL" in text_blob.upper() or ("指数" in text_blob and not has_stock_code):
            continue
        items: list[list[str]] = []
        for row in raw_items[:5]:
            if not isinstance(row, list):
                continue
            values = [_repair_text(v)[: int(getattr(config, "stock_max_value_chars", 80))] for v in row[:8]]
            projected = len(json.dumps(values, ensure_ascii=False))
            if used + projected > 1200:
                truncated = True
                break
            items.append(values)
            used += projected
        if items:
            tables.append({"columns": columns[:8], "items": items})
    return {
        "status": "valid" if tables else "unavailable",
        "tables": tables,
        "truncated": truncated,
        "limits": {"max_tables": 3, "max_rows": 5, "max_total_chars": 1200},
    }


def _capital_flow_category(label: Any) -> str | None:
    """Map provider-specific labels to the three supported flow categories.

    Choice 妙想 has returned several schemas over time: some responses put the
    category in the first cell of a row, some in a column name, and some use
    English/underscore aliases.  We intentionally do not retain medium/small
    orders or an unqualified generic ``资金流向`` field.
    """
    text = _repair_text(label).replace(" ", "").replace("_", "").lower()
    if not text or any(token in text for token in ("中单", "小单", "散户", "mediumorder", "smallorder")):
        return None
    if any(token in text for token in ("超大单", "特大单", "superlarge", "superbig", "superorder")):
        return "超大单"
    if any(token in text for token in ("大单", "bigorder", "largeorder", "bigfund", "largefund")):
        return "大单"
    if any(token in text for token in ("主力", "mainforce", "mainfund", "mainorder")):
        return "主力"
    return None


def _capital_flow_values(values: Any, max_value_chars: int) -> list[str]:
    """Normalize one provider value or value list without dropping numeric zero."""
    if isinstance(values, (list, tuple)):
        source = list(values)
    else:
        source = [values]
    result: list[str] = []
    for value in source[:6]:
        if value is None:
            continue
        text = _repair_text(str(value))[:max_value_chars]
        if text.strip() and text.strip() not in {"-", "—", "--", "暂无", "无", "未知", "N/A", "nan"}:
            result.append(text)
    return result


def _extract_capital_flow(payload: dict[str, Any], config: Any) -> dict[str, Any]:
    """Keep only main, big-order and super-big-order flow fields.

    The function accepts the usual ``data[].items`` table shape and also
    tolerates dictionary rows or schemas where the category is encoded in a
    column name.  Any one of the three categories with a non-empty value is
    sufficient to mark capital flow as available.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    max_chars = min(int(getattr(config, "stock_max_total_chars", 5000)), 1800)
    max_value_chars = int(getattr(config, "stock_max_value_chars", 80))
    used = 0
    truncated = False

    def add(category: str | None, label: Any, values: Any) -> None:
        nonlocal used, truncated
        compact_values = _capital_flow_values(values, max_value_chars)
        if not category or not compact_values:
            return
        metric = _repair_text(label)[:80]
        key = (category, metric, tuple(compact_values))
        if key in seen:
            return
        entry = {"category": category, "metric": metric, "values": compact_values}
        size = len(json.dumps(entry, ensure_ascii=False))
        if used + size > max_chars:
            truncated = True
            return
        seen.add(key)
        rows.append(entry)
        used += size

    for raw_table in list(payload.get("data") or []):
        if not isinstance(raw_table, dict):
            continue
        columns = [_repair_text(value) for value in list(raw_table.get("columns") or [])]
        raw_items = list(raw_table.get("items") or raw_table.get("rows") or [])
        for raw_row in raw_items:
            if isinstance(raw_row, dict):
                for label, value in raw_row.items():
                    add(_capital_flow_category(label), label, value)
                continue
            if not isinstance(raw_row, list):
                continue
            # Row-label schema: ["主力净流入", value, ...]
            if raw_row:
                add(_capital_flow_category(raw_row[0]), raw_row[0], raw_row[1:])
            # Column-labelled schema: [date, main_flow, big_flow, ...]
            for index, column in enumerate(columns):
                if index < len(raw_row):
                    add(_capital_flow_category(column), column, [raw_row[index]])

    categories = list(dict.fromkeys(entry["category"] for entry in rows))
    return {
        "status": "valid" if rows else "unavailable",
        "has_data": bool(rows),
        "categories": categories,
        "rows": rows,
        "truncated": truncated,
    }


def _fetch_miaoxiang_sector(symbol: str, fallback_name: str | None = None) -> dict[str, Any]:
    """Read sector identity and breadth/strength from Choice 妙想."""
    service = _get_ai_service()
    config = service.config.miaoxiang
    if not (config.enabled and config.em_api_key):
        return {"status": "unavailable", "strength": "未知", "source": "妙想未配置"}
    now = time.time()
    with _MIAOXIANG_SECTOR_CACHE_LOCK:
        cached = _MIAOXIANG_SECTOR_CACHE.get(symbol)
        if cached and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return dict(cached[1])
    tool_name = "mx_index_block_finance_data"
    try:
        client = _llm_submodule("miaoxiang").MiaoxiangClient(config)
        identity = asyncio.run(
            client.call_tool(
                tool_name,
                {"query": f"查询证券 {symbol} 当前所属行业板块名称"},
            )
        )
        identity_payload = _mcp_json(identity) or {}
        sector_name = None
        for label, values in _mcp_table_pairs(identity_payload):
            # Choice may return rows such as “申万行业(2016) | 信息服务-通信设备-通信传输设备”
            # rather than a literal “所属行业板块” label.  Use the most specific
            # final segment while retaining the raw classification in the result.
            if "行业" in label and values:
                raw_name = _repair_text(values[0])
                parts = [part.strip() for part in raw_name.split("-") if part.strip()]
                sector_name = parts[-1] if parts else raw_name
                if sector_name:
                    break
        if not sector_name and fallback_name:
            sector_name = _repair_text(fallback_name)
        if not sector_name:
            for _label, values in _mcp_table_pairs(identity_payload):
                for value in values:
                    raw_name = _repair_text(value)
                    if "-" in raw_name and not re.match(r"^\d{4}-\d{2}-\d{2}$", raw_name):
                        parts = [part.strip() for part in raw_name.split("-") if part.strip()]
                        if parts:
                            sector_name = parts[-1]
                            break
                if sector_name:
                    break
        if not sector_name:
            return {"status": "unknown", "strength": "未知", "source": "妙想未返回板块"}
        try:
            detail = asyncio.run(
                client.call_tool(
                    tool_name,
                    {
                        "query": (
                            f"查询{sector_name}行业板块当前涨跌幅、近5个交易日涨跌幅、"
                            "成交额、上涨家数、下跌家数；同时仅列出板块内最多5只核心/龙头个股的"
                            "今日涨跌幅、近5日涨跌幅和成交额"
                        )
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - retain identity if detail is down
            result = {
                "status": "partial",
                "name": sector_name,
                "strength": "未知",
                "source": f"妙想:{tool_name}（仅返回板块归属）",
                "error": type(exc).__name__,
            }
            with _MIAOXIANG_SECTOR_CACHE_LOCK:
                _MIAOXIANG_SECTOR_CACHE[symbol] = (now, result)
            return dict(result)
        payload = _mcp_json(detail) or {}
        pairs = _mcp_table_pairs(payload)
        latest: dict[str, float] = {}
        change_series: list[float] = []
        for label, values in pairs:
            key = next(
                (
                    name
                    for name in ("涨跌幅", "上涨家数", "下跌家数", "成交额")
                    if name in label
                ),
                None,
            )
            numbers = [_parse_mcp_number(value) for value in values]
            numbers = [number for number in numbers if number is not None]
            if not key or not numbers:
                continue
            if "涨跌幅" in key:
                latest.setdefault("change_pct", numbers[0])
                if not change_series:
                    change_series = numbers[:6]
            elif "上涨家数" in key:
                latest["up_count"] = numbers[0]
            elif "下跌家数" in key:
                latest["down_count"] = numbers[0]
            elif "成交额" in key:
                latest["turnover"] = numbers[0]
        change = latest.get("change_pct")
        up_count, down_count = latest.get("up_count"), latest.get("down_count")
        breadth = (
            (up_count - down_count) / (up_count + down_count)
            if up_count is not None and down_count is not None and up_count + down_count
            else None
        )
        if change is None:
            strength = "未知"
        elif (change >= 1.0 and (breadth is None or breadth >= 0.1)) or (
            breadth is not None and breadth >= 0.35
        ):
            strength = "偏强"
        elif change <= -1.0 or (breadth is not None and breadth <= -0.25):
            strength = "偏弱"
        else:
            strength = "中性"
        result = {
            "status": "valid",
            "name": sector_name,
            "strength": strength,
            "source": f"妙想:{tool_name}",
            "as_of": _now_iso(),
            "change_pct": change,
            "change_pct_recent": change_series,
            "up_count": up_count,
            "down_count": down_count,
            "breadth": breadth,
            "turnover": latest.get("turnover"),
            "core_performance": _compact_core_performance(payload, config),
        }
    except Exception as exc:  # noqa: BLE001 - keep LLM analysis available
        result = {
            "status": "degraded",
            "strength": "未知",
            "source": "妙想调用失败",
            "error": type(exc).__name__,
        }
    with _MIAOXIANG_SECTOR_CACHE_LOCK:
        _MIAOXIANG_SECTOR_CACHE[symbol] = (now, result)
    return dict(result)


# Choice/Miaoxiang may not return an identity row for every security.  Keep a
# small local classification fallback so the current pool remains grouped and
# sector aggregation does not collapse into one “未分类” bucket.
_LOCAL_SECTOR_BY_SYMBOL = {
    "603629": "消费电子",
    "002851": "电源设备",
    "300394": "通信设备",
    "300136": "消费电子",
    "002463": "元件",
    "600487": "通信设备",
    "002192": "能源金属",
    "600378": "化学制品",
    "600183": "元件",
    "300476": "元件",
    "300857": "计算机设备",
    "603626": "通用设备",
    "000712": "证券",
    "600547": "贵金属",
    "300604": "半导体设备",
    "301526": "化学纤维",
    "603256": "电子材料",
    "000938": "通信设备",
    "600584": "半导体",
    "301511": "能源金属",
    "300666": "半导体材料",
    "300058": "传媒",
    "301171": "传媒",
    "002353": "专用设备",
    "603283": "专用设备",
    "000725": "光学光电子",
    "601212": "工业金属",
    "603259": "医疗服务",
    "301183": "光学光电子",
    "300456": "半导体",
    "603667": "汽车零部件",
    "002837": "专用设备",
    "002536": "汽车零部件",
    "600105": "通信设备",
    "002882": "电力设备",
    "600362": "工业金属",
}


def _local_sector_name(symbol: str, name: str | None = None) -> str | None:
    code = _code(symbol)
    if code in _LOCAL_SECTOR_BY_SYMBOL:
        return _LOCAL_SECTOR_BY_SYMBOL[code]
    text = f"{name or ''} {code}"
    for keywords, sector in (
        (("黄金", "白银"), "贵金属"),
        (("通信", "光电", "光迅", "天孚"), "通信设备"),
        (("半导体", "芯片", "封测"), "半导体"),
        (("证券", "股份银行", "银行"), "证券"),
        (("铜", "铝", "锌"), "工业金属"),
        (("医药", "药明"), "医疗服务"),
    ):
        if any(keyword in text for keyword in keywords):
            return sector
    return None


def _mean(values: list[float]) -> float:
    """Return a safe arithmetic mean for market feature calculations."""
    return statistics.fmean(values) if values else 0.0


def _clip(value: float, lower: float, upper: float) -> float:
    """Clamp a numeric feature to a stable range."""
    return max(lower, min(upper, value))


def _return(closes: list[float], end: int, lookback: int) -> float:
    """Calculate a lookback return ending at ``end`` without future data."""
    start = end - lookback
    if start < 0 or closes[start] <= 0:
        return 0.0
    return closes[end] / closes[start] - 1.0


def _rsi(closes: list[float], end: int, period: int = 14) -> float:
    """Calculate Wilder-style RSI over the requested historical slice."""
    start = end - period
    if start < 0:
        return 50.0
    gains = 0.0
    losses = 0.0
    for idx in range(start + 1, end + 1):
        change = closes[idx] - closes[idx - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    if losses <= 1e-12:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100.0 - 100.0 / (1.0 + relative_strength)


def _money_flow_index(
    candles: list[dict[str, Any]],
    volumes: list[float],
    end: int | None = None,
    period: int = MFI_PERIOD,
) -> float:
    """Calculate the standard MFI using only bars observable at ``end``."""
    if not candles:
        return 50.0
    end = len(candles) - 1 if end is None else min(end, len(candles) - 1)
    if end < period or end >= len(volumes):
        return 50.0
    positive_flow = 0.0
    negative_flow = 0.0
    start = end - period + 1
    for index in range(start, end + 1):
        current = candles[index]
        previous = candles[index - 1]
        typical = (
            _float(current.get("high"))
            + _float(current.get("low"))
            + _float(current.get("close"))
        ) / 3.0
        previous_typical = (
            _float(previous.get("high"))
            + _float(previous.get("low"))
            + _float(previous.get("close"))
        ) / 3.0
        raw_flow = typical * max(0.0, _float(volumes[index]))
        if typical > previous_typical:
            positive_flow += raw_flow
        elif typical < previous_typical:
            negative_flow += raw_flow
    if negative_flow <= 1e-12:
        return 100.0 if positive_flow > 1e-12 else 50.0
    if positive_flow <= 1e-12:
        return 0.0
    money_ratio = positive_flow / negative_flow
    return 100.0 - 100.0 / (1.0 + money_ratio)


def _mfi_regime(
    closes: list[float],
    highs: list[float],
    volumes: list[float],
    end: int,
    *,
    momentum20: float,
    volume_ratio: float,
) -> str:
    """Separate ordinary trends from main-rise and limit-up-chain setups."""
    if end <= 0:
        return "normal_trend"
    daily_return = _return(closes, end, 1)
    if daily_return >= 0.095:
        return "limit_up_chain"
    recent_high = max(highs[max(0, end - 19) : end + 1])
    near_high = recent_high > 0 and closes[end] >= recent_high * 0.97
    if (momentum20 >= 0.12 and near_high) or (
        daily_return >= 0.055 and volume_ratio >= 1.20
    ):
        return "main_rise"
    return "normal_trend"


def _mfi_filter(mfi14: float, regime: str) -> dict[str, Any]:
    """Return style-aware MFI thresholds and a bounded score adjustment."""
    if regime == "limit_up_chain":
        lower, upper = MFI_LIMIT_UP_MIN, None
        passed = mfi14 >= lower
        score_delta = 5.0 if mfi14 >= 65.0 else (3.0 if passed else -10.0)
        label = "涨停/连板"
    elif regime == "main_rise":
        lower, upper = MFI_MAIN_RISE_MIN, None
        passed = mfi14 >= lower
        score_delta = 3.0 if mfi14 >= 50.0 else (1.0 if passed else -8.0)
        label = "主升"
    else:
        lower, upper = MFI_NORMAL_MIN, MFI_NORMAL_MAX
        passed = lower <= mfi14 <= upper
        score_delta = (
            3.0
            if 45.0 <= mfi14 <= 75.0
            else (1.0 if passed else (-8.0 if mfi14 < lower else -5.0))
        )
        label = "普通趋势"
    upper_text = "无上限" if upper is None else f"{upper:.0f}"
    return {
        "passed": passed,
        "regime": regime,
        "regime_label": label,
        "period": MFI_PERIOD,
        "value": round(mfi14, 2),
        "minimum": lower,
        "maximum": upper,
        "score_delta": score_delta,
        "reason": (
            f"{label} MFI14={mfi14:.1f}，阈值 {lower:.0f}–{upper_text}"
            + ("，资金流确认" if passed else "，资金流未确认")
        ),
    }


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Calculate a simple ATR used only for stop/take-profit reference levels."""
    if len(candles) < 2:
        return 0.0
    start = max(1, len(candles) - period)
    true_ranges: list[float] = []
    for idx in range(start, len(candles)):
        high = _float(candles[idx].get("high"))
        low = _float(candles[idx].get("low"))
        previous_close = _float(candles[idx - 1].get("close"))
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return _mean(true_ranges)


def _feature_row(
    closes: list[float], volumes: list[float], end: int
) -> list[float] | None:
    """Build leakage-free price/volume features for one historical day."""
    if end < 60 or closes[end] <= 0:
        return None
    daily_returns = [
        closes[idx] / closes[idx - 1] - 1.0
        for idx in range(end - 9, end + 1)
        if closes[idx - 1] > 0
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


def _predict_up_probability(
    closes: list[float], volumes: list[float]
) -> tuple[float | None, float | None, int]:
    """Compatibility wrapper over the dual-horizon walk-forward model."""
    result = _llm_submodule("traditional").predict_horizon(closes, volumes, horizon=1)
    validation = result.get("validation") or {}
    return (
        result.get("value"),
        validation.get("accuracy"),
        int(result.get("training_samples") or 0),
    )


def _analyze_series(series: dict[str, Any]) -> dict[str, Any]:
    """Build selection, prediction and risk features from one daily K-line series."""
    candles = [
        candle
        for candle in list(series.get("candles") or [])
        if _float(candle.get("close")) > 0
    ]
    if len(candles) < 20:
        return {"available": False, "reason": "历史数据不足"}

    volume_by_time = {
        str(item.get("time")): _float(item.get("value"))
        for item in list(series.get("volume") or [])
    }
    closes = [_float(candle.get("close")) for candle in candles]
    highs = [_float(candle.get("high")) for candle in candles]
    lows = [_float(candle.get("low")) for candle in candles]
    volumes = [volume_by_time.get(str(candle.get("time")), 0.0) for candle in candles]
    latest_feature_values = _feature_row(closes, volumes, len(closes) - 1)
    feature_names = list(_llm_submodule("traditional").FEATURE_NAMES)
    close = closes[-1]
    ma5 = _mean(closes[-5:])
    ma20 = _mean(closes[-20:])
    ma60 = _mean(closes[-60:]) if len(closes) >= 60 else ma20
    momentum20 = _return(closes, len(closes) - 1, min(20, len(closes) - 1))
    momentum60 = _return(closes, len(closes) - 1, min(60, len(closes) - 1))
    rsi14 = _rsi(closes, len(closes) - 1)
    volume5 = _mean(volumes[-5:])
    volume20 = _mean(volumes[-20:])
    volume_ratio = volume5 / volume20 if volume20 else 1.0
    mfi14 = _money_flow_index(candles, volumes)
    mfi_regime = _mfi_regime(
        closes,
        highs,
        volumes,
        len(closes) - 1,
        momentum20=momentum20,
        volume_ratio=volume_ratio,
    )
    mfi_filter = _mfi_filter(mfi14, mfi_regime)
    recent_high20 = max(closes[-20:]) if len(closes) >= 20 else close
    recent_low20 = min(closes[-20:]) if len(closes) >= 20 else close
    returns20 = [
        closes[idx] / closes[idx - 1] - 1.0
        for idx in range(max(1, len(closes) - 20), len(closes))
        if closes[idx - 1] > 0
    ]
    volatility20 = statistics.pstdev(returns20) if len(returns20) > 1 else 0.0
    volume_std20 = statistics.pstdev(volumes[-20:]) if len(volumes) >= 20 else 0.0
    volume_zscore20 = (
        (volumes[-1] - volume20) / volume_std20 if volume_std20 > 1e-12 else 0.0
    )
    probability_result = _llm_submodule("traditional").predict_all_horizons(
        closes, volumes
    )
    next_day = probability_result["probabilities"]["next_trading_day"]
    next_five_days = probability_result["probabilities"]["next_5_trading_days"]
    probability = next_day.get("value")
    validation_accuracy = (next_day.get("validation") or {}).get("accuracy")
    training_samples = next_day.get("training_samples", 0)

    score = 50.0
    score += _clip(momentum20 * 150.0, -20.0, 20.0)
    score += 10.0 if close > ma20 else -10.0
    score += 8.0 if ma20 > ma60 else -8.0
    score += 5.0 if 45.0 <= rsi14 <= 70.0 else (-6.0 if rsi14 >= 80.0 else 0.0)
    score += _clip((volume_ratio - 1.0) * 5.0, -5.0, 5.0)
    score += _float(mfi_filter.get("score_delta"))
    score = round(_clip(score, 0.0, 100.0), 1)
    swing_score = _llm_submodule("traditional").swing_composite_score(
        score,
        probability,
        next_five_days.get("value"),
        next_day.get("validation"),
        next_five_days.get("validation"),
    )
    probability_reliability = {
        "next_trading_day": _llm_submodule("traditional").probability_reliability(
            next_day.get("validation")
        ),
        "next_5_trading_days": _llm_submodule("traditional").probability_reliability(
            next_five_days.get("validation")
        ),
    }
    atr14 = _atr(candles)
    recent_high20 = max(highs[-20:])
    recent_low20 = min(lows[-20:])
    limit_up_days_5 = sum(
        1
        for index in range(max(1, len(closes) - 5), len(closes))
        if _return(closes, index, 1) >= 0.095
    )
    resistance_candidates = [
        level for level in (ma5, ma20, ma60, recent_high20) if level > close * 1.001
    ]
    support_candidates = [
        level for level in (ma5, ma20, ma60, recent_low20) if 0 < level < close * 0.999
    ]
    resistance_price = (
        min(resistance_candidates)
        if resistance_candidates
        else close + max(2.0 * atr14, close * 0.05)
    )
    support_price = (
        max(support_candidates)
        if support_candidates
        else max(0.0, close - max(2.0 * atr14, close * 0.05))
    )
    trend = (
        "多头排列"
        if close > ma20 > ma60
        else ("空头排列" if close < ma20 < ma60 else "趋势混合")
    )
    if close > ma20 > ma60 and momentum20 > 0:
        trend_direction = "上升"
    elif close < ma20 < ma60 and momentum20 < 0:
        trend_direction = "下降"
    elif close >= ma20 and momentum20 >= 0:
        trend_direction = "偏强"
    elif close <= ma20 and momentum20 <= 0:
        trend_direction = "偏弱"
    else:
        trend_direction = "震荡"
    return {
        "available": True,
        "as_of": candles[-1].get("time"),
        "close": close,
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "momentum20": momentum20,
        "momentum60": momentum60,
        "rsi14": rsi14,
        "volume_ratio": volume_ratio,
        "mfi14": mfi14,
        "mfi_regime": mfi_regime,
        "mfi_filter": mfi_filter,
        "limit_up_days_5": limit_up_days_5,
        "trend_strength": ma20 / ma60 - 1.0 if ma60 else 0.0,
        "volatility20": volatility20,
        "breakout20": close / recent_high20 - 1.0 if recent_high20 else 0.0,
        "pullback20": close / recent_low20 - 1.0 if recent_low20 else 0.0,
        "volume_zscore20": volume_zscore20,
        "atr14": atr14,
        "resistance_price": resistance_price,
        "support_price": support_price,
        "selection_score": score,
        "swing_score": swing_score,
        "probability_reliability": probability_reliability,
        "up_probability": probability,
        "probabilities": probability_result["probabilities"],
        "assessment_status": probability_result["assessment_status"],
        "unavailable_reason": next_day.get("unavailable_reason"),
        "validation_accuracy": validation_accuracy,
        "training_samples": training_samples,
        "model_features": dict(zip(feature_names, latest_feature_values or [])),
        "trend": trend,
        "trend_direction": trend_direction,
    }


def _pool_signals(
    state: dict[str, Any], quotes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Convert pool analysis into ranked selection and executable-action advice."""
    positions = {str(item.get("symbol")): item for item in state["positions"]}
    allowed_symbols = {
        str(item.get("symbol"))
        for item in list(state.get("watchlist") or [])
        + list(state.get("positions") or [])
        if item.get("symbol")
    }
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in quotes:
        symbol = str(item.get("symbol", ""))
        if (
            not symbol
            or symbol not in allowed_symbols
            or symbol in seen
            or item.get("quote_error")
        ):
            continue
        seen.add(symbol)
        analysis = dict(item.get("analysis") or {})
        if not analysis.get("available"):
            continue
        candidates.append({"item": item, "analysis": analysis})

    candidates.sort(
        key=lambda candidate: _float(
            candidate["analysis"].get("swing_score"),
            _float(candidate["analysis"].get("selection_score")),
        ),
        reverse=True,
    )
    sector_scores: dict[str, list[float]] = {}
    for candidate in candidates:
        item = candidate["item"]
        sector = str(
            item.get("sector_name")
            or item.get("sector")
            or _local_sector_name(str(item.get("symbol") or ""), item.get("name"))
            or "未分类"
        )
        sector_scores.setdefault(sector, []).append(
            _float(
                candidate["analysis"].get("swing_score"),
                _float(candidate["analysis"].get("selection_score")),
            )
        )
    sector_strength = {
        sector: round(statistics.fmean(values), 3)
        for sector, values in sector_scores.items()
        if values
    }
    signals: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates, start=1):
        item = candidate["item"]
        analysis = candidate["analysis"]
        symbol = str(item["symbol"])
        position = positions.get(symbol)
        holding = position is not None and _float(position.get("quantity")) > 0
        close = _float(analysis.get("close"), _float(item.get("current_price")))
        current_price = _float(item.get("current_price"), close) or close
        score = _float(analysis.get("selection_score"))
        probabilities = analysis.get("probabilities") or {}
        probability_value = analysis.get("up_probability")
        probability = (
            _float(probability_value) if probability_value is not None else None
        )
        five_day_value = (probabilities.get("next_5_trading_days") or {}).get("value")
        five_day_probability = (
            _float(five_day_value) if five_day_value is not None else None
        )
        swing_score = _float(
            analysis.get("swing_score"),
            _llm_submodule("traditional").swing_composite_score(
                score, probability, five_day_probability
            ),
        )
        momentum20 = _float(analysis.get("momentum20"))
        ma5 = _float(analysis.get("ma5"))
        ma20 = _float(analysis.get("ma20"))
        ma60 = _float(analysis.get("ma60"))
        atr14 = _float(analysis.get("atr14"))
        mfi14 = _float(analysis.get("mfi14"), 50.0)
        mfi_filter = dict(analysis.get("mfi_filter") or {})
        mfi_passed = bool(mfi_filter.get("passed", True))
        pre_mfi_score = score - _float(mfi_filter.get("score_delta"))
        pre_mfi_swing_score = _llm_submodule("traditional").swing_composite_score(
            pre_mfi_score, probability, five_day_probability
        )
        entry_price = _float(position.get("entry_price")) if position else 0.0
        position_return = current_price / entry_price - 1.0 if entry_price > 0 else 0.0

        action = "观察"
        trigger = "等待评分、预测概率与趋势形成共振"
        if holding:
            hard_stop = entry_price > 0 and position_return <= -0.08
            trend_exit = current_price < ma60 and momentum20 < 0
            model_exit = (
                probability is not None
                and five_day_probability is not None
                and probability <= 0.40
                and five_day_probability <= 0.46
                and current_price < ma20
            )
            if hard_stop or trend_exit or model_exit:
                action = "止损" if hard_stop else ("清仓" if trend_exit else "减仓")
                trigger = (
                    "触发 8% 持仓止损"
                    if hard_stop
                    else (
                        "跌破 MA60 且 20 日动量转负"
                        if trend_exit
                        else "预测转弱且跌破 MA20"
                    )
                )
            elif (
                swing_score >= SWING_ADD_SCORE_THRESHOLD
                and probability is not None
                and probability >= 0.58
                and five_day_probability is not None
                and five_day_probability >= SWING_ADD_FIVE_DAY_PROBABILITY_THRESHOLD
                and current_price > ma20 > ma60
                and mfi_passed
            ):
                action = "加仓"
                trigger = "波段综合分、1日/5日上涨概率、多头排列与 MFI 共振"
            else:
                action = "持有"
                trigger = "持仓未触发止损或加仓条件"
        elif (
            swing_score >= SWING_STRONG_BUY_SCORE_THRESHOLD
            and probability is not None
            and probability >= 0.56
            and five_day_probability is not None
            and five_day_probability >= SWING_STRONG_FIVE_DAY_PROBABILITY_THRESHOLD
            and current_price > ma20 > ma60
            and mfi_passed
        ):
            action = "买入"
            trigger = "达到强势波段阈值，综合分、双周期概率、多头排列与 MFI 共振"
        elif (
            swing_score >= SWING_BUY_SCORE_THRESHOLD
            and probability is not None
            and probability >= 0.52
            and five_day_probability is not None
            and five_day_probability >= SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD
            and current_price > ma20
            and mfi_passed
        ):
            action = "买入"
            trigger = "达到普通波段阈值，综合分、1日/5日概率与 MFI 同步转强"
        elif (
            not mfi_passed
            and pre_mfi_swing_score >= SWING_BUY_SCORE_THRESHOLD
            and probability is not None
            and probability >= 0.52
            and five_day_probability is not None
            and five_day_probability >= SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD
            and current_price > ma20
        ):
            action = "等待买入"
            trigger = f"价格与概率达标，但 {mfi_filter.get('reason') or 'MFI 资金流未确认'}"
        elif (
            swing_score >= SWING_WATCH_SCORE_THRESHOLD
            and probability is not None
            and probability >= 0.49
            and five_day_probability is not None
            and five_day_probability >= SWING_WATCH_FIVE_DAY_PROBABILITY_THRESHOLD
        ):
            action = "等待买入"
            trigger = "双周期概率已转强，等待价格趋势进一步确认"

        validation_accuracy = analysis.get("validation_accuracy")
        validation_text = (
            f" · 验证{_float(validation_accuracy) * 100:.0f}%"
            if validation_accuracy is not None
            else ""
        )
        stop_price = max(0.0, current_price - max(2.0 * atr14, current_price * 0.06))
        take_profit = current_price + max(3.0 * atr14, current_price * 0.10)
        reason = (
            f"{trigger}；波段综合分 {swing_score:.1f}（技术分 {score:.1f}）"
            f" · 1日/5日概率 {probability if probability is not None else '—'}/"
            f"{five_day_probability if five_day_probability is not None else '—'}"
            f" · 20日动量 {momentum20:+.1%} · {analysis.get('trend', '趋势未知')}"
            f" · MFI14 {mfi14:.1f}{validation_text} · 风险参考 {stop_price:.2f}/{take_profit:.2f}"
        )
        resistance_price = _float(analysis.get("resistance_price"), take_profit)
        support_price = _float(analysis.get("support_price"), stop_price)
        execution_signal: dict[str, Any] | None = None
        suggested_price: float | None = None
        quantity = 0.0
        if action in {"买入", "加仓", "减仓", "卖出", "止损", "清仓"}:
            if action in {"减仓", "卖出", "止损", "清仓"}:
                held_quantity = _float(position.get("quantity")) if position else 0.0
                exit_ratio = 1.0 if action in {"止损", "清仓"} else 0.5
                quantity = math.floor(held_quantity * exit_ratio / 100) * 100
                side = "sell"
                suggested_price = current_price
            else:
                # Prefer a pullback around MA5/MA20 instead of presenting the
                # stored position cost or the latest quote as a buy suggestion.
                pullback_price = ma5 if ma5 > 0 else ma20
                suggested_price = min(current_price, pullback_price or current_price)
                # Advisory sizing: one board lot or roughly 10% of review capital.
                board_lots = math.floor(
                    (REVIEW_INITIAL_EQUITY * 0.10) / suggested_price / 100
                )
                quantity = float(max(1, board_lots) * 100)
                side = "buy"
            if quantity > 0:
                signal_date = str(
                    analysis.get("as_of") or item.get("updated_at") or "latest"
                )
                execution_signal = {
                    "signal_id": f"review-{symbol}-{signal_date}-{side}",
                    "symbol": symbol,
                    "action": side,
                    "quantity": quantity,
                    "price": round(suggested_price, 3),
                    "strategy_id": "review_center_momentum_logit_v1",
                    "tag": f"swing_score={swing_score:.1f};technical_score={score:.1f};up_probability_1d={probability if probability is not None else 'null'};up_probability_5d={five_day_probability if five_day_probability is not None else 'null'};mfi14={mfi14:.1f}",
                }
        direction = (
            analysis.get("trend_direction") or analysis.get("trend") or "趋势未知"
        )
        evaluation_parts = [
            trigger,
            f"20日动量 {momentum20:+.1%}，趋势{direction}",
        ]
        if suggested_price is not None:
            advice_label = (
                "建议卖出价"
                if action in {"减仓", "卖出", "止损", "清仓"}
                else "建议买入价"
            )
            quantity_text = f"，建议数量 {quantity:g}" if quantity > 0 else ""
            evaluation_parts.append(
                f"{advice_label} {suggested_price:.2f}{quantity_text}"
            )
        else:
            evaluation_parts.append(f"现价 {current_price:.2f}，继续观察")
        evaluation = "；".join(evaluation_parts)
        latest_ai: dict[str, Any] | None = None
        try:
            latest_ai = _get_ai_service().storage.latest(symbol)
        except Exception:  # noqa: BLE001 - the traditional table remains usable
            latest_ai = None
        signal = {
            "symbol": symbol,
            "name": item.get("name", symbol),
            "sector_name": str(
                item.get("sector_name")
                or item.get("sector")
                or _local_sector_name(symbol, item.get("name"))
                or "未分类"
            ),
            "pool": "持仓" if holding else "观察",
            "action": action,
            "current_price": round(current_price, 3),
            "suggested_price": (
                round(suggested_price, 3) if suggested_price is not None else None
            ),
            "selection_rank": rank,
            "selection_score": score,
            "swing_score": swing_score,
            "sector_strength": sector_strength.get(
                str(
                    item.get("sector_name")
                    or item.get("sector")
                    or _local_sector_name(symbol, item.get("name"))
                    or "未分类"
                ),
                0.0,
            ),
            "up_probability": probability,
            "probabilities": probabilities,
            "assessment_status": analysis.get("assessment_status"),
            "unavailable_reason": analysis.get("unavailable_reason"),
            "validation_accuracy": validation_accuracy,
            "momentum20": momentum20,
            "momentum60": _float(analysis.get("momentum60")),
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "rsi14": _float(analysis.get("rsi14")),
            "volume_ratio": _float(analysis.get("volume_ratio")),
            "mfi14": mfi14,
            "mfi_regime": analysis.get("mfi_regime"),
            "mfi_filter": mfi_filter,
            "limit_up_days_5": int(_float(analysis.get("limit_up_days_5"))),
            "trend_strength": _float(analysis.get("trend_strength")),
            "volatility20": _float(analysis.get("volatility20")),
            "breakout20": _float(analysis.get("breakout20")),
            "pullback20": _float(analysis.get("pullback20")),
            "volume_zscore20": _float(analysis.get("volume_zscore20")),
            "atr14": atr14,
            "trend": analysis.get("trend"),
            "trend_direction": analysis.get("trend_direction"),
            "trigger": trigger,
            "resistance_price": round(resistance_price, 3),
            "support_price": round(support_price, 3),
            "evaluation": evaluation,
            "stop_price": round(stop_price, 3),
            "take_profit_price": round(take_profit, 3),
            "execution_signal": execution_signal,
            "reason": reason,
            "updated_at": analysis.get("as_of") or item.get("updated_at") or _now_iso(),
        }
        current_day = str(analysis.get("as_of") or item.get("updated_at") or "")[:10]
        latest_ai_day = str((latest_ai or {}).get("as_of") or "")[:10]
        if latest_ai and latest_ai_day == current_day:
            fusion = latest_ai.get("fusion") or {}
            instrument = latest_ai.get("instrument") or {}
            probabilities = fusion.get("final_up_probabilities") or {}
            signal.update(
                {
                    "ai_analysis_id": latest_ai.get("analysis_id"),
                    "ai_as_of": latest_ai.get("as_of"),
                    "fusion": fusion,
                    "risk": latest_ai.get("risk") or fusion.get("risk"),
                    "market_context": latest_ai.get("market_context"),
                    "sector_context": latest_ai.get("sector_context"),
                    "llm": latest_ai.get("llm"),
                    "final_action": fusion.get("final_action"),
                    "final_score": fusion.get("final_score"),
                    "final_next_day_probability": (
                        probabilities.get("next_trading_day") or {}
                    ).get("value"),
                    "final_five_day_probability": (
                        probabilities.get("next_5_trading_days") or {}
                    ).get("value"),
                    "operation_advice": fusion.get("summary"),
                    "position_return": (instrument.get("position") or {}).get(
                        "return", position_return
                    ),
                }
            )
        elif latest_ai:
            signal["ai_stale_as_of"] = latest_ai.get("as_of")
        signals.append(signal)

    action_priority = {
        "止损": 0,
        "清仓": 1,
        "卖出": 2,
        "减仓": 3,
        "买入": 4,
        "加仓": 5,
        "持有": 6,
        "等待买入": 7,
        "观察": 8,
    }
    return sorted(
        signals,
        key=lambda signal: (
            action_priority.get(str(signal.get("action")), 9),
            int(signal.get("selection_rank", 9999)),
        ),
    )


def _code(symbol: str) -> str:
    digits = re.sub(r"\D", "", str(symbol))
    if len(digits) >= 6:
        return digits[-6:]
    return str(symbol).strip().upper()


def _miaoxiang_quota_message(value: Any) -> str | None:
    """Extract a user-actionable 妙想 quota/credit exhaustion message."""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if message := _miaoxiang_quota_message(item):
                return message
        return None
    if isinstance(value, dict):
        for item in value.values():
            if message := _miaoxiang_quota_message(item):
                return message
        return None
    text = str(value or "")
    if not text:
        return None
    lowered = text.lower()
    markers = ("积分已用完", "积分不足", "额度已用完", "额度不足", "购买套餐", "quota", "credit")
    return text if any(marker in text or marker in lowered for marker in markers) else None


def _secid(symbol: str) -> str:
    code = _code(symbol)
    # 沪市股票/ETF 以 1 开头,深市/北交所以 0 开头,足够覆盖 A 股票池查询。
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode({key: str(value) for key, value in params.items()})
    target = f"{url}?{query}"
    is_tencent = "gtimg.cn" in url or "qq.com" in url
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    referer = "https://gu.qq.com/" if is_tencent else "https://quote.eastmoney.com/"
    # curl reaches Tencent and most Eastmoney endpoints much faster than a
    # fresh PowerShell process.  Retain PowerShell as a Windows fallback for
    # endpoints which occasionally close curl's TLS connection.
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        try:
            completed = subprocess.run(
                [
                    curl, "-sS", "-L", "--max-time", "15",
                    "-A", user_agent,
                    "-H", f"Referer: {referer}",
                    "-H", "Accept: application/json,text/plain,*/*",
                    target,
                ],
                capture_output=True,
                check=True,
                timeout=18,
            )
            return json.loads(completed.stdout.decode("utf-8"))
        except Exception:  # noqa: BLE001 - fall through to PowerShell/urllib
            pass
    if os.name == "nt":
        try:
            command = (
                "$OutputEncoding = [Console]::OutputEncoding = "
                "(New-Object System.Text.UTF8Encoding); "
                "$headers=@{'User-Agent'='" + user_agent + "';"
                "'Referer'='" + referer + "';"
                "'Accept'='application/json,text/plain,*/*'}; "
                "(Invoke-WebRequest -UseBasicParsing "
                f"-Uri '{target}' -Headers $headers -TimeoutSec 15).Content"
            )
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                capture_output=True,
                check=True,
                timeout=18,
            )
            return json.loads(completed.stdout.decode("utf-8"))
        except Exception:  # noqa: BLE001 - fall through to the standard library client
            pass
    last_error: Exception | None = None
    for attempt in range(3):
        request = Request(
            target,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": referer,
            },
        )
        try:
            with _DIRECT_OPENER.open(request, timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry transient public API failures
            last_error = exc
            if attempt < 2:
                time.sleep(0.35 * (attempt + 1))
    raise last_error or RuntimeError("上游行情请求失败")


def stock_search(query: str) -> list[dict[str, Any]]:
    """Query Eastmoney's public A-share suggestion endpoint."""
    if not query.strip():
        return []
    payload = _get_json(
        EASTMONEY_SEARCH,
        {"input": query.strip(), "type": 14, "token": ""},
    )
    rows = payload.get("QuotationCodeTable", {}).get("Data", [])
    result: list[dict[str, Any]] = []
    for row in rows:
        code = _code(row.get("Code", ""))
        if len(code) != 6:
            continue
        result.append(
            {
                "symbol": code,
                "name": _repair_text(row.get("Name", code), code),
                "market": _repair_text(row.get("SecurityTypeName", "A股"), "A股"),
            }
        )
    return result[:20]


def _stock_kline_eastmoney(symbol: str) -> dict[str, Any]:
    """Fetch daily OHLCV and derive the quote fields used by the watchlist."""
    begin = (datetime.now(timezone.utc) - timedelta(days=KLINE_HISTORY_DAYS)).strftime(
        "%Y%m%d"
    )
    quote_future = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        quote_future = executor.submit(
            _get_json,
            EASTMONEY_QUOTE,
            {
                "secid": _secid(symbol),
                "fields": "f57,f58,f43,f47,f48,f60,f170,f127,f128,f129",
            },
        )
        payload = _get_json(
            EASTMONEY_KLINE,
            {
                "secid": _secid(symbol),
                "klt": 101,
                "fqt": 1,
                "beg": begin,
                "end": 20991231,
                "fields1": "f1,f2,f3,f4",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
        )
        try:
            quote = (quote_future.result() or {}).get("data") or {}
        except Exception:  # noqa: BLE001 - K line is still useful when quote is unavailable
            quote = {}
    data = payload.get("data") or {}
    rows: list[dict[str, Any]] = []
    for raw in data.get("klines") or []:
        parts = str(raw).split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "time": parts[0],
                "open": _float(parts[1]),
                "close": _float(parts[2]),
                "high": _float(parts[3]),
                "low": _float(parts[4]),
                "value": _float(parts[5]),
                "change_pct": _float(parts[8]) if len(parts) > 8 else 0.0,
            }
        )
    if not rows:
        raise ValueError(f"未找到 {symbol} 的日线行情")
    last = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    previous_volume = _float(previous["value"]) if previous else 0.0
    volume_change = (
        (last["value"] / previous_volume - 1.0) * 100.0 if previous_volume else 0.0
    )
    code = _code(symbol)
    name = _repair_text(quote.get("name") or data.get("name") or code, code)
    if name == code or name.isdigit():
        try:
            matches = stock_search(code)
            name = next(
                (item["name"] for item in matches if item["symbol"] == code), name
            )
        except Exception:
            pass
    return {
        "symbol": code,
        "name": name,
        "current_price": _float(quote.get("f43"), last["close"] * 100.0) / 100.0,
        "previous_price": (
            _float(quote.get("f60")) / 100.0
            if quote.get("f60") is not None
            else (previous["close"] if previous else 0.0)
        ),
        "change_pct": last["change_pct"],
        "volume": last["value"],
        "volume_change_pct": volume_change,
        "sector_name": _repair_text(quote.get("f127")) or None,
        "region_name": _repair_text(quote.get("f128")) or None,
        "concept_names": _repair_text(quote.get("f129")) or None,
        "updated_at": last["time"],
        "quote_source": "Eastmoney public market API",
        "series": {
            "symbol": code,
            "candles": [
                {
                    "time": row["time"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                }
                for row in rows
            ],
            "volume": [
                {
                    "time": row["time"],
                    "value": row["value"],
                    "up": row["close"] >= row["open"],
                }
                for row in rows
            ],
            "markers": [],
        },
    }


def _stock_kline_tencent(symbol: str) -> dict[str, Any]:
    """Fallback daily K line source used when Eastmoney closes the connection."""
    code = _code(symbol)
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    payload = _get_json(
        TENCENT_KLINE,
        {"param": f"{market}{code},day,,,{KLINE_HISTORY_DAYS},qfq"},
    )
    data = (payload.get("data") or {}).get(f"{market}{code}") or {}
    raw_rows = data.get("qfqday") or data.get("day") or []
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if len(raw) < 6:
            continue
        rows.append(
            {
                "time": str(raw[0]),
                "open": _float(raw[1]),
                "close": _float(raw[2]),
                "high": _float(raw[3]),
                "low": _float(raw[4]),
                "value": _float(raw[5]),
            }
        )
    if not rows:
        raise ValueError(f"未找到 {symbol} 的日线行情")
    last = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    previous_price = previous["close"] if previous else 0.0
    previous_volume = previous["value"] if previous else 0.0
    # Tencent includes a compact quote array in the same response.  Reading it
    # avoids coupling this fallback back to Eastmoney, which can itself be the
    # failed provider that caused us to reach Tencent.
    quote_values = list((data.get("qt") or {}).get(f"{market}{code}") or [])
    quote = {
        "name": quote_values[1] if len(quote_values) > 1 else None,
        "current": quote_values[3] if len(quote_values) > 3 else None,
        "previous": quote_values[4] if len(quote_values) > 4 else None,
    }
    name = _repair_text(quote.get("name") or code, code)
    if name == code or name.isdigit():
        try:
            matches = stock_search(code)
            name = next(
                (item["name"] for item in matches if item["symbol"] == code), name
            )
        except Exception:
            pass
    return {
        "symbol": code,
        "name": name,
        "current_price": _float(quote.get("current"), last["close"]),
        "previous_price": (
            _float(quote.get("previous"))
            if quote.get("previous") is not None
            else previous_price
        ),
        "change_pct": (
            (last["close"] / previous_price - 1.0) * 100.0
            if previous_price
            else 0.0
        ),
        "volume": last["value"],
        "volume_change_pct": (
            (last["value"] / previous_volume - 1.0) * 100.0 if previous_volume else 0.0
        ),
        "sector_name": None,
        "region_name": None,
        "concept_names": None,
        "updated_at": last["time"],
        "quote_source": "Tencent fqkline API",
        "series": {
            "symbol": code,
            "candles": [
                {
                    "time": row["time"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                }
                for row in rows
            ],
            "volume": [
                {
                    "time": row["time"],
                    "value": row["value"],
                    "up": row["close"] >= row["open"],
                }
                for row in rows
            ],
            "markers": [],
        },
    }


def _stock_kline_miaoxiang(symbol: str) -> dict[str, Any]:
    """Fetch daily OHLCV from the first-priority 妙想 read-only MCP tool.

    The MCP response is a semantic table: dates are columns and OHLCV fields
    are rows.  We normalize that table into the same series shape used by the
    market-data fallbacks.  If 妙想 is unavailable or returns no complete
    OHLCV table, the caller deliberately falls through to Tencent.
    """
    service = _get_ai_service()
    config = service.config.miaoxiang
    tool_name = "mx_ashare_finance_data"
    if not (config.enabled and config.em_api_key and tool_name in config.read_tool_allowlist):
        raise RuntimeError("妙想 MCP 未启用或个股行情工具不在只读白名单")
    code = _code(symbol)
    client = _llm_submodule("miaoxiang").MiaoxiangClient(config)
    last_error: Exception | None = None
    response = None
    for attempt in range(2):
        try:
            response = asyncio.run(client.call_tool(
                tool_name,
                {"query": f"查询{code}最近360个交易日的日线开盘价、最高价、最低价、收盘价、成交量"},
            ))
            break
        except Exception as exc:  # noqa: BLE001 - retry transient MCP disconnects
            last_error = exc
            if attempt == 0:
                time.sleep(0.25)
    if response is None:
        raise last_error or RuntimeError("妙想 MCP 未返回个股行情")
    payload = _mcp_json(response) or {}
    # 妙想 may return a successful MCP envelope containing a quota/permission
    # message instead of a data table. Preserve that actionable reason rather
    # than collapsing it into the misleading generic "OHLCV incomplete" error.
    message = str(payload.get("message") or "").strip()
    if message:
        raise RuntimeError(f"妙想行情不可用: {message}")
    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")

    def number(value: Any) -> float | None:
        text = _repair_text(value).replace(",", "").strip()
        if not text:
            return None
        multiplier = 1.0
        if "亿" in text:
            multiplier = 100000000.0
        elif "万" in text:
            multiplier = 10000.0
        match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
        return float(match.group(0)) * multiplier if match else None

    field_aliases = {
        "open": ("开盘价", "开盘"),
        "high": ("最高价", "最高"),
        "low": ("最低价", "最低"),
        "close": ("收盘价", "收盘"),
        "value": ("成交量", "成交额", "成交"),
    }
    rows_by_date: dict[str, dict[str, float]] = {}
    for table in list(payload.get("data") or []):
        if not isinstance(table, dict):
            continue
        columns = [_repair_text(v) for v in list(table.get("columns") or [])]
        date_positions = {
            idx: match.group(1)
            for idx, column in enumerate(columns)
            if (match := date_re.search(column))
        }
        if len(date_positions) < 20:
            continue
        for raw_row in list(table.get("items") or []):
            if not isinstance(raw_row, list) or not raw_row:
                continue
            label = _repair_text(raw_row[0])
            field = next(
                (name for name, aliases in field_aliases.items()
                 if any(alias in label for alias in aliases)),
                None,
            )
            if field is None:
                continue
            for index, date in date_positions.items():
                parsed = number(raw_row[index]) if index < len(raw_row) else None
                if parsed is not None:
                    rows_by_date.setdefault(date, {})[field] = parsed
    rows = []
    for date, values in sorted(rows_by_date.items()):
        if not all(key in values for key in ("open", "high", "low", "close", "value")):
            continue
        rows.append({"time": date, **values, "change_pct": 0.0})
    if len(rows) < 30:
        raise ValueError(f"妙想未返回 {code} 的完整日线 OHLCV")
    for index in range(1, len(rows)):
        previous = rows[index - 1]["close"]
        rows[index]["change_pct"] = (rows[index]["close"] / previous - 1.0) * 100.0 if previous else 0.0
    last, previous = rows[-1], rows[-2]
    return {
        "symbol": code,
        "name": code,
        "current_price": last["close"],
        "previous_price": previous["close"],
        "change_pct": last["change_pct"],
        "volume": last["value"],
        "volume_change_pct": (last["value"] / previous["value"] - 1.0) * 100.0 if previous["value"] else 0.0,
        "sector_name": None,
        "updated_at": last["time"],
        "quote_source": "妙想:mx_ashare_finance_data",
        "series": {
            "symbol": code,
            "candles": [{"time": row["time"], "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]} for row in rows],
            "volume": [{"time": row["time"], "value": row["value"], "up": row["close"] >= row["open"]} for row in rows],
            "markers": [],
        },
    }


def _stock_kline_sina(symbol: str) -> dict[str, Any]:
    """Fetch the full daily OHLCV window from Sina's public API.

    Tencent currently returns a WAF 501 page and Eastmoney may close TLS
    connections under burst load. Sina provides the same full daily window,
    so this repairs provider availability without reducing the universe or
    using synthetic/shortened data.
    """
    code = _code(symbol)
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    payload = _get_json(
        SINA_KLINE,
        {"symbol": f"{market}{code}", "scale": 240, "ma": "no", "datalen": KLINE_HISTORY_DAYS},
    )
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"未找到 {symbol} 的 Sina 日线行情")
    rows = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        row = {
            "time": str(raw.get("day") or ""),
            "open": _float(raw.get("open")), "close": _float(raw.get("close")),
            "high": _float(raw.get("high")), "low": _float(raw.get("low")),
            "value": _float(raw.get("volume")), "change_pct": 0.0,
        }
        if row["time"] and row["close"] > 0:
            rows.append(row)
    if not rows:
        raise ValueError(f"未找到 {symbol} 的有效 Sina 日线行情")
    for index in range(1, len(rows)):
        previous = rows[index - 1]["close"]
        rows[index]["change_pct"] = (rows[index]["close"] / previous - 1.0) * 100.0 if previous else 0.0
    last = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    previous_volume = _float(previous["value"]) if previous else 0.0
    return {
        "symbol": code, "name": code,
        "current_price": last["close"],
        "previous_price": previous["close"] if previous else last["close"],
        "change_pct": last["change_pct"], "volume": last["value"],
        "volume_change_pct": (last["value"] / previous_volume - 1.0) * 100.0 if previous_volume else 0.0,
        "sector_name": None, "updated_at": last["time"],
        "quote_source": "Sina CN_MarketData API",
        "series": {
            "symbol": code,
            "candles": [{"time": r["time"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]} for r in rows],
            "volume": [{"time": r["time"], "value": r["value"], "up": r["close"] >= r["open"]} for r in rows],
            "markers": [],
        },
    }


def stock_kline(
    symbol: str,
    refresh: bool = False,
    allowed_providers: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return a short-lived cached quote/K-line payload for one A-share."""
    code = _code(symbol)
    now = time.monotonic()
    if not refresh:
        with _STOCK_KLINE_CACHE_LOCK:
            cached = _STOCK_KLINE_CACHE.get(code)
        if cached and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            cached_result = cached[1]
            if allowed_providers is None:
                return cached_result
            source = str(cached_result.get("quote_source") or "").lower()
            source_ok = any(
                (name == "miaoxiang" and source.startswith("妙想:"))
                or (name == "tencent" and source.startswith("tencent"))
                or (name == "eastmoney" and "eastmoney" in source)
                or (name == "sina" and "sina" in source)
                for name in allowed_providers
            )
            if source_ok:
                return cached_result
    errors: list[str] = []
    provider_map = {
        "miaoxiang": _stock_kline_miaoxiang,
        "tencent": _stock_kline_tencent,
        "eastmoney": _stock_kline_eastmoney,
        "sina": _stock_kline_sina,
    }
    provider_order = (
        tuple(provider_map[name] for name in allowed_providers if name in provider_map)
        if allowed_providers is not None
        else (_stock_kline_miaoxiang, _stock_kline_tencent, _stock_kline_eastmoney, _stock_kline_sina)
    )
    # 妙想 MCP is authoritative. Research callers may restrict the chain to
    # 妙想→腾讯 so an outage never silently creates a downgraded snapshot.
    for provider in provider_order:
        try:
            result = provider(code)
            break
        except Exception as exc:  # noqa: BLE001 - retain provider diagnostics
            errors.append(f"{provider.__name__}: {exc}")
    else:
        raise RuntimeError(f"行情接口均失败({code}): {' | '.join(errors)}")
    if errors:
        # Keep fallback use explicit in the payload so the UI/report can show
        # why a lower-priority source was selected instead of implying that
        # the authoritative source served the data.
        result = dict(result)
        result["degraded"] = True
        result["provider_diagnostics"] = errors
        result["requested_provider_order"] = [
            name for name in (allowed_providers or ("miaoxiang", "tencent", "eastmoney", "sina"))
        ]
    with _STOCK_KLINE_CACHE_LOCK:
        _STOCK_KLINE_CACHE[code] = (now, result)
    return result


def stock_intraday(symbol: str, max_days: int = 3) -> dict[str, Any]:
    """Fetch up to three trading days and select a compact interval locally."""
    payload = _get_json(
        EASTMONEY_TRENDS,
        {
            "secid": _secid(symbol),
            "ndays": min(3, max(1, max_days)),
            "iscr": 0,
            "iscca": 0,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        },
    )
    raw_rows = (payload.get("data") or {}).get("trends") or []
    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        parts = str(raw).split(",")
        if len(parts) < 6:
            continue
        timestamp = parts[0]
        clock = timestamp[-5:]
        if clock < "09:30" or clock > "15:00":
            continue
        rows.append(
            {
                "time": timestamp,
                "open": _float(parts[1]),
                "close": _float(parts[2]),
                "high": _float(parts[3]),
                "low": _float(parts[4]),
                "volume": _float(parts[5]),
            }
        )
    if not rows:
        return {
            "status": "unavailable",
            "source_interval_minutes": 1,
            "selected_interval_minutes": None,
            "coverage_trading_days": 0,
            "rows": [],
        }
    # Keep the payload below roughly 160 bars: use actual 1/5/15-minute
    # aggregation according to the fetched row count, and audit the choice.
    interval = 1 if len(rows) <= 160 else (5 if len(rows) <= 600 else 15)
    aggregated: list[dict[str, Any]] = []
    for row in rows:
        stamp = datetime.strptime(row["time"], "%Y-%m-%d %H:%M")
        minute = (stamp.minute // interval) * interval
        bucket = stamp.replace(minute=minute).strftime("%Y-%m-%d %H:%M")
        if aggregated and aggregated[-1]["time"] == bucket:
            target = aggregated[-1]
            target["close"] = row["close"]
            target["high"] = max(target["high"], row["high"])
            target["low"] = min(target["low"], row["low"])
            target["volume"] += row["volume"]
        else:
            aggregated.append({**row, "time": bucket})
    dates = {row["time"][:10] for row in rows}
    return {
        "status": "valid",
        "source_interval_minutes": 1,
        "selected_interval_minutes": interval,
        "coverage_trading_days": len(dates),
        "session_start": "09:30",
        "session_end": "refresh_time",
        "rows": aggregated,
    }


def _market_breadth_context(refresh: bool = False) -> dict[str, Any]:
    """Fetch a verifiable whole-market breadth snapshot from Eastmoney.

    If the public endpoint is unavailable, return an explicit unavailable state;
    no synthetic 0.5/neutral value is emitted.
    """
    global _MARKET_BREADTH_CACHE
    now = time.monotonic()
    if not refresh:
        with _MARKET_BREADTH_CACHE_LOCK:
            cached = _MARKET_BREADTH_CACHE
        if cached and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return dict(cached[1])
    try:
        payload = _get_json(
            EASTMONEY_CLIST,
            {
                "pn": 1,
                "pz": 100,
                "po": 1,
                "np": 1,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2",
                "fields": "f2,f3,f12,f14",
            },
        )
        rows = list(((payload.get("data") or {}).get("diff") or []))
        # Eastmoney clist encodes percentage changes in hundredths of a percent.
        changes = [_float(row.get("f3"), 0.0) / 100.0 for row in rows if row.get("f3") is not None]
        up = sum(value > 0 for value in changes)
        down = sum(value < 0 for value in changes)
        flat = max(0, len(changes) - up - down)
        if not changes:
            raise ValueError("行情列表未返回涨跌幅")
        breadth = (up - down) / len(changes)
        limit_up = sum(value >= 9.5 for value in changes)
        limit_down = sum(value <= -9.5 for value in changes)
        avg_change = _mean(changes)
        if breadth >= 0.25 and avg_change >= 0.5:
            sample_sentiment = "偏强"
        elif breadth <= -0.25 and avg_change <= -0.5:
            sample_sentiment = "偏弱"
        else:
            sample_sentiment = "中性"
        sentiment = sample_sentiment if len(changes) >= 500 else "未知"
        result = {
            "status": "valid" if len(changes) >= 500 else "partial",
            "source": "Eastmoney clist",
            "as_of": _now_iso(),
            "sample_count": len(changes),
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "breadth": round(breadth, 6),
            "average_change_pct": round(avg_change, 4),
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "sentiment": sentiment,
            "sample_sentiment": sample_sentiment,
        }
    except Exception as exc:  # noqa: BLE001
        result = {
            "status": "unavailable",
            "source": "Eastmoney clist",
            "as_of": _now_iso(),
            "reason": f"无法获取全市场涨跌列表: {type(exc).__name__}",
            "sentiment": "未知",
            "breadth": None,
        }
    with _MARKET_BREADTH_CACHE_LOCK:
        _MARKET_BREADTH_CACHE = (now, result)
    return dict(result)


def _fetch_miaoxiang_market(refresh: bool = False) -> dict[str, Any]:
    """Query whole-market sentiment/breadth through the verified Choice tool."""
    global _MIAOXIANG_MARKET_CACHE
    service = _get_ai_service()
    config = service.config.miaoxiang
    if not (config.enabled and config.em_api_key):
        return {"status": "unavailable", "source": "妙想未配置", "sentiment": "未知"}
    now = time.time()
    with _MIAOXIANG_MARKET_CACHE_LOCK:
        cached = _MIAOXIANG_MARKET_CACHE
        if cached and not refresh and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return dict(cached[1])
    try:
        client = _llm_submodule("miaoxiang").MiaoxiangClient(config)
        response = None
        last_error: Exception | None = None
        for query in ("A股市场上涨家数下跌家数", "沪深两市今日上涨家数和下跌家数"):
            try:
                response = asyncio.run(client.call_tool(
                    "mx_index_block_finance_data", {"query": query}
                ))
                break
            except Exception as exc:  # noqa: BLE001 - retry with a simpler query
                last_error = exc
        if response is None:
            raise last_error or RuntimeError("妙想未返回市场宽度")
        pairs = _mcp_table_pairs(_mcp_json(response) or {})
        values: dict[str, float] = {}
        for label, row_values in pairs:
            number = next((_parse_mcp_number(value) for value in row_values if _parse_mcp_number(value) is not None), None)
            if number is None:
                continue
            for key in ("上涨家数", "下跌家数", "涨停家数", "跌停家数", "成交额", "涨跌幅"):
                if key in label:
                    values[key] = number
                    break
        up, down = values.get("上涨家数"), values.get("下跌家数")
        breadth = (up - down) / (up + down) if up is not None and down is not None and up + down else None
        change = values.get("涨跌幅")
        sentiment = "未知" if breadth is None else ("偏强" if breadth >= 0.25 else ("偏弱" if breadth <= -0.25 else "中性"))
        result = {"status": "valid" if breadth is not None else "partial", "source": "妙想:mx_index_block_finance_data", "as_of": _now_iso(), "up_count": up, "down_count": down, "limit_up_count": values.get("涨停家数"), "limit_down_count": values.get("跌停家数"), "turnover": values.get("成交额"), "change_pct": change, "breadth": breadth, "sentiment": sentiment}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "unavailable", "source": "妙想调用失败", "reason": type(exc).__name__, "sentiment": "未知"}
    with _MIAOXIANG_MARKET_CACHE_LOCK:
        _MIAOXIANG_MARKET_CACHE = (now, result)
    return dict(result)


def _fetch_miaoxiang_stock(symbol: str, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch compact, read-only individual-stock facts from Choice 妙想."""
    global _MIAOXIANG_STOCK_CACHE
    service = _get_ai_service()
    config = service.config.miaoxiang
    tool_name = "mx_ashare_finance_data"
    if not (config.enabled and config.em_api_key and tool_name in config.read_tool_allowlist):
        return {
            "status": "unavailable",
            "source": "妙想个股工具未加入只读白名单",
            "facts": [],
            "truncated": False,
        }
    now = time.time()
    with _MIAOXIANG_STOCK_CACHE_LOCK:
        cached = _MIAOXIANG_STOCK_CACHE.get(symbol)
        if cached and not refresh and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return dict(cached[1])
    try:
        client = _llm_submodule("miaoxiang").MiaoxiangClient(config)
        response = asyncio.run(client.call_tool(
            tool_name,
            {"query": f"查询{symbol}当前行情、换手率、成交额、主力/超大单/大单/中单/小单资金净流入及流入流出明细、估值、所属行业和风险指标"},
        ))
        payload = _mcp_json(response) or {}
        tables, truncated = _compact_miaoxiang_tables(payload, config)
        result = {
            "status": "valid" if tables else "partial",
            "source": f"妙想:{tool_name}",
            "as_of": _now_iso(),
            "tables": tables,
            "truncated": truncated,
            "limits": {
                "max_tables": config.stock_max_tables,
                "max_rows": config.stock_max_rows,
                "max_value_chars": config.stock_max_value_chars,
                "max_total_chars": config.stock_max_total_chars,
            },
            "capital_flow": _extract_capital_flow(payload, config),
        }
        with _MIAOXIANG_STOCK_CACHE_LOCK:
            _MIAOXIANG_STOCK_CACHE[symbol] = (now, result)
        return result
    except Exception as exc:  # noqa: BLE001
        result = {"status": "unavailable", "source": f"妙想:{tool_name}", "reason": type(exc).__name__, "facts": [], "truncated": False}
        with _MIAOXIANG_STOCK_CACHE_LOCK:
            _MIAOXIANG_STOCK_CACHE[symbol] = (now, result)
        return result


def _fetch_miaoxiang_events(symbol: str, *, refresh: bool = False) -> dict[str, Any]:
    """Optionally fetch a tiny recent news/notice digest.

    Disabled by default because external text is token-expensive and can leak
    historical context into replay.  Tools must be explicitly allowlisted.
    """
    global _MIAOXIANG_EVENTS_CACHE
    service = _get_ai_service()
    config = service.config.miaoxiang
    if not (config.enabled and config.em_api_key and config.news_enabled):
        return {"status": "unavailable", "source": "妙想新闻/公告未启用", "items": [], "truncated": False}
    tools = [
        ("mx_finance_search_news", "新闻"),
        ("mx_finance_search_notice", "公告"),
    ]
    allowed = [(name, label) for name, label in tools if name in config.read_tool_allowlist]
    if not allowed:
        return {"status": "unavailable", "source": "新闻/公告工具未加入只读白名单", "items": [], "truncated": False}
    now = time.time()
    with _MIAOXIANG_EVENTS_CACHE_LOCK:
        cached = _MIAOXIANG_EVENTS_CACHE.get(symbol)
        if cached and not refresh and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return dict(cached[1])
    items: list[dict[str, Any]] = []
    truncated = False
    try:
        client = _llm_submodule("miaoxiang").MiaoxiangClient(config)
        for tool_name, label in allowed:
            response = asyncio.run(client.call_tool(tool_name, {"query": f"查询{symbol}最近{config.news_days}天{label}，仅返回标题、日期、来源和摘要"}))
            payload = _mcp_json(response) or {}
            compact, tool_truncated = _compact_miaoxiang_events(payload, config, source=f"妙想:{tool_name}")
            items.extend(compact)
            truncated = truncated or tool_truncated
            if len(items) >= config.news_max_items:
                truncated = True
                break
        # De-duplicate identical headlines returned by news and notice tools,
        # then enforce a single global text budget across both calls.
        deduped: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        used_chars = 0
        for item in items:
            title = str(item.get("title") or "")
            if title and title in seen_titles:
                continue
            item_chars = len(json.dumps(item, ensure_ascii=False))
            if used_chars + item_chars > config.news_max_chars:
                truncated = True
                break
            if title:
                seen_titles.add(title)
            deduped.append(item)
            used_chars += item_chars
            if len(deduped) >= config.news_max_items:
                break
        items = deduped
        result = {"status": "valid" if items else "partial", "source": "妙想新闻/公告", "as_of": _now_iso(), "items": items, "truncated": truncated, "limits": {"days": config.news_days, "max_items": config.news_max_items, "max_chars": config.news_max_chars}}
    except Exception as exc:  # noqa: BLE001
        result = {"status": "unavailable", "source": "妙想新闻/公告调用失败", "reason": type(exc).__name__, "items": [], "truncated": False}
    with _MIAOXIANG_EVENTS_CACHE_LOCK:
        _MIAOXIANG_EVENTS_CACHE[symbol] = (now, result)
    return dict(result)


def _market_context(indices: list[dict[str, Any]], *, refresh: bool = False) -> dict[str, Any]:
    valid = [item for item in indices if not item.get("quote_error")]
    changes = [_float(item.get("change_pct")) for item in valid]
    average = _mean(changes)
    regime = "偏强" if average >= 0.5 else ("偏弱" if average <= -0.5 else "震荡")
    breadth = _market_breadth_context(refresh=refresh)
    miaoxiang_market = _fetch_miaoxiang_market(refresh=refresh)
    if miaoxiang_market.get("status") in {"valid", "partial"}:
        breadth = miaoxiang_market
    return {
        "as_of": _now_iso(),
        "regime": regime,
        "index_average_change_pct": round(average, 3),
        "indices": valid,
        "market_sentiment": breadth.get("sentiment", "未知"),
        "market_breadth": breadth,
    }


def _build_ai_context(
    item: dict[str, Any], state: dict[str, Any], indices: list[dict[str, Any]]
) -> dict[str, Any]:
    symbol = str(item.get("symbol") or "")
    position = next(
        (
            entry
            for entry in state.get("positions", [])
            if str(entry.get("symbol")) == symbol
        ),
        None,
    )
    analysis = dict(item.get("analysis") or {})
    series = dict(item.get("series") or {})
    candles = list(series.get("candles") or [])[-10:]
    volume_map = {
        str(row.get("time")): _float(row.get("value"))
        for row in list(series.get("volume") or [])
    }
    daily_rows = [
        [
            row.get("time"),
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            volume_map.get(str(row.get("time")), 0.0),
        ]
        for row in candles
    ]
    try:
        intraday = stock_intraday(symbol, max_days=3)
    except Exception as exc:  # noqa: BLE001
        intraday = {"status": "unavailable", "reason": str(exc), "rows": []}
    current_price = _float(item.get("current_price"), _float(analysis.get("close")))
    entry_price = _float((position or {}).get("entry_price"))
    quantity = _float((position or {}).get("quantity"))
    position_payload = (
        {
            "quantity": quantity,
            "available_quantity": _float((position or {}).get("available_quantity"), quantity),
            "entry_price": entry_price,
            "current_price": current_price,
            "market_value": current_price * quantity,
            "return": current_price / entry_price - 1 if entry_price else None,
            "portfolio_weight": current_price * quantity / REVIEW_INITIAL_EQUITY,
            "source": "local_review_center_state",
        }
        if position
        else None
    )
    sector_context = _fetch_miaoxiang_sector(symbol, item.get("sector_name"))
    miaoxiang_stock = _fetch_miaoxiang_stock(symbol)
    external_events = _fetch_miaoxiang_events(symbol)
    missing = []
    if intraday.get("status") != "valid":
        missing.append("intraday_k")
    if sector_context.get("status") != "valid":
        missing.append("sector_strength")
    if miaoxiang_stock.get("status") not in {"valid", "partial"}:
        missing.append("miaoxiang_stock_facts")
    market = _market_context(indices)
    if (market.get("market_breadth") or {}).get("status") not in {"valid", "partial"}:
        missing.extend(["market_breadth", "market_sentiment"])
    all_positions = []
    for entry in state.get("positions") or []:
        qty = _float(entry.get("quantity"))
        cost = _float(entry.get("entry_price"))
        entry_symbol = str(entry.get("symbol") or "")
        entry_current = current_price if entry_symbol == symbol else _float(entry.get("current_price"))
        if entry_current <= 0 and entry_symbol and entry_symbol != symbol:
            try:
                entry_current = _float(stock_kline(entry_symbol).get("current_price"))
            except Exception:  # noqa: BLE001 - keep other holdings explicitly unknown
                entry_current = 0.0
        all_positions.append({
            "symbol": entry_symbol,
            "name": entry.get("name"),
            "quantity": qty,
            "available_quantity": _float(entry.get("available_quantity"), qty),
            "entry_price": cost,
            "current_price": entry_current or None,
            "portfolio_weight": (entry_current * qty / REVIEW_INITIAL_EQUITY) if entry_current > 0 else None,
        })
    watch_items = [
        {"symbol": str(entry.get("symbol") or ""), "name": entry.get("name"),
         "observed_price": _float(entry.get("self_price")) or None,
         "note": entry.get("note") or ""}
        for entry in state.get("watchlist") or []
    ]
    traditional_payload = {**analysis, "rules": _traditional_rule_snapshot()}
    traditional_payload["decision"] = _traditional_action_summary(analysis, bool(position))
    previous_close = _float(candles[-2].get("close")) if len(candles) >= 2 else None
    ma60 = _float(analysis.get("ma60"))
    traditional_payload["position_flags"] = {
        "close_below_ma60": bool(current_price > 0 and ma60 > 0 and current_price < ma60),
        "ma60_break_is_new": bool(
            previous_close is not None and ma60 > 0 and current_price < ma60 <= previous_close
        ),
        "condition_semantics": "当前已在 MA60 下方时，不应把‘跌破 MA60’描述为新的未来触发；只有重新站上后再次跌破才是新破位。",
    }
    account_snapshot = _portfolio_snapshot(
        state, current_symbol=symbol, current_price=current_price
    )
    return {
        "as_of": str(analysis.get("as_of") or item.get("updated_at") or _now_iso()),
        "instrument": {
            "symbol": symbol,
            "name": item.get("name", symbol),
            "market": "A股",
            "pool": "持仓" if position else "观察",
            "current_price": current_price,
            "position": position_payload,
        },
        "data_quality": {
            "status": "partial" if missing else "complete",
            "score": max(55, 100 - len(missing) * 12),
            "stale": False,
            "model_fallback_probability": False,
            "missing_fields": missing,
            "conflicts": [],
        },
        "market_context": {**market, "market_breadth2": market.get("market_breadth")},
        "sector_context": sector_context,
        "portfolio_context": {
            "account_equity": account_snapshot.get("total_assets") or REVIEW_INITIAL_EQUITY,
            "account_snapshot": account_snapshot,
            "position_count": len(state.get("positions") or []),
            "consecutive_negative_feedback": None,
            "positions": all_positions,
            "watchlist": watch_items,
            "recent_manual_trades": list(state.get("manual_trades") or [])[-10:],
            "cash": account_snapshot.get("cash"),
            "cash_status": account_snapshot.get("status"),
        },
        "stock_context": {
            "technical": analysis,
            "daily_recent_bars": {
                "expected_rows": 10,
                "fields": ["date", "o", "h", "l", "c", "v"],
                "rows": daily_rows,
            },
            "intraday_recent_bars": intraday,
            "miaoxiang_facts": miaoxiang_stock,
        },
        "traditional": traditional_payload,
        "external_events": external_events,
    }


def _update_outcome_labels(item: dict[str, Any]) -> None:
    symbol = str(item.get("symbol") or "")
    candles = list((item.get("_ai_series") or {}).get("candles") or [])
    if not symbol or not candles:
        return
    storage = _get_ai_service().storage
    for pending in storage.pending_labels(symbol):
        as_of_day = str(pending.get("as_of") or "")[:10]
        future = [
            row for row in candles if str(row.get("time") or "")[:10] > as_of_day
        ][:5]
        if not future:
            continue
        result = pending.get("result") or {}
        stop_price = (
            ((result.get("plans") or {}).get("stop_loss") or {}).get("price_zone") or {}
        ).get("trigger_price")
        storage.label_outcome(
            str(pending["analysis_id"]), future, stop_price=_float(stop_price) or None
        )


def market_indices(refresh: bool = False) -> list[dict[str, Any]]:
    """Return short-lived cached daily changes for the main A-share indices."""
    global _MARKET_INDEX_CACHE
    now = time.monotonic()
    if not refresh:
        with _MARKET_INDEX_CACHE_LOCK:
            cached = _MARKET_INDEX_CACHE
        if cached and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return cached[1]

    targets = (
        ("上证指数", "1.000001"),
        ("创业板指数", "0.399006"),
    )

    def fetch(name: str, secid: str) -> dict[str, Any]:
        try:
            data = _get_json(
                EASTMONEY_QUOTE,
                {"secid": secid, "fields": "f57,f58,f43,f60,f170"},
            ).get("data") or {}
            if not data.get("f43") or not data.get("f60"):
                raise ValueError("东方财富指数报价为空")
            return {
                "name": name,
                "symbol": str(data.get("f57") or secid.split(".")[-1]),
                "current_price": _float(data.get("f43")) / 100.0,
                "previous_price": _float(data.get("f60")) / 100.0,
                "change_pct": _float(data.get("f170")) / 100.0,
                "source": "东方财富",
            }
        except Exception as exc:  # noqa: BLE001 - index failure must not block the review page
            # Eastmoney occasionally closes the TLS connection for index
            # quotes. Tencent's public K-line endpoint carries the same live
            # quote in its ``qt`` payload, so use it as a transparent fallback.
            try:
                code = secid.split(".")[-1]
                market = "sh" if secid.startswith("1.") else "sz"
                payload = _get_json(
                    TENCENT_KLINE,
                    {"param": f"{market}{code},day,,,5,qfq"},
                )
                block = (payload.get("data") or {}).get(f"{market}{code}") or {}
                quote = (block.get("qt") or {}).get(f"{market}{code}") or []
                if len(quote) < 33:
                    raise ValueError("腾讯指数报价为空")
                return {
                    "name": _repair_text(quote[1], name) or name,
                    "symbol": code,
                    "current_price": _float(quote[3]),
                    "previous_price": _float(quote[4]),
                    "change_pct": _float(quote[32]),
                    "source": "腾讯",
                }
            except Exception as fallback_exc:  # noqa: BLE001
                return {
                    "name": name,
                    "symbol": secid.split(".")[-1],
                    "quote_error": f"{type(exc).__name__}; fallback {type(fallback_exc).__name__}",
                }

    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        result = list(executor.map(lambda target: fetch(*target), targets))
    with _MARKET_INDEX_CACHE_LOCK:
        _MARKET_INDEX_CACHE = (now, result)
    return result


def _review_baseline_backtest(
    state: dict[str, Any], *, calendar_days: int = 60
) -> dict[str, Any]:
    """Simulate a two-month short-term trend baseline from current pool symbols."""
    raw_symbols = [
        str(item.get("symbol") or "")
        for item in list(state.get("positions") or [])
        + list(state.get("watchlist") or [])
        if item.get("symbol")
    ]
    symbols = list(dict.fromkeys(_code(value) for value in raw_symbols if _code(value)))
    if not symbols:
        return {"status": "insufficient_data", "reason": "当前持仓和观察池没有股票", "trades": [], "equity_curve": []}
    series_by_symbol: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for symbol in symbols:
        try:
            candles = list((stock_kline(symbol).get("series") or {}).get("candles") or [])
            normalized = [
                {"time": str(row.get("time") or "")[:10], "high": _float(row.get("high")), "low": _float(row.get("low")), "close": _float(row.get("close"))}
                for row in candles
                if str(row.get("time") or "") and _float(row.get("close")) > 0
            ]
            normalized.sort(key=lambda row: row["time"])
            if len(normalized) >= 21:
                series_by_symbol[symbol] = normalized
            else:
                errors[symbol] = "日 K 数据不足 21 根"
        except Exception as exc:  # noqa: BLE001
            errors[symbol] = type(exc).__name__
    if not series_by_symbol:
        return {"status": "insufficient_data", "reason": "没有可用的个股日 K 数据", "errors": errors, "trades": [], "equity_curve": []}
    dates = sorted({row["time"] for rows in series_by_symbol.values() for row in rows})
    latest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    start = latest - timedelta(days=max(30, int(calendar_days)))
    dates = [value for value in dates if datetime.strptime(value, "%Y-%m-%d").date() >= start]
    by_date = {symbol: {row["time"]: row for row in rows} for symbol, rows in series_by_symbol.items()}
    initial = float(REVIEW_INITIAL_EQUITY)
    cash = initial
    holdings: dict[str, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    commission_rate, stamp_tax_rate, slippage_rate = 0.0003, 0.0005, 0.001

    def sell_cost(gross: float) -> float:
        return max(5.0, gross * commission_rate) + gross * stamp_tax_rate

    for as_of in dates:
        for symbol in list(holdings):
            row = by_date[symbol].get(as_of)
            bars = [item for item in series_by_symbol[symbol] if item["time"] <= as_of]
            if not row or len(bars) < 21:
                continue
            ma20 = sum(item["close"] for item in bars[-20:]) / 20.0
            momentum20 = row["close"] / bars[-21]["close"] - 1.0 if bars[-21]["close"] > 0 else 0.0
            if row["close"] >= ma20 and momentum20 >= 0:
                continue
            position = holdings.pop(symbol)
            quantity = int(position["quantity"])
            execution_price = row["close"] * (1.0 - slippage_rate)
            gross = execution_price * quantity
            costs = sell_cost(gross)
            cash += gross - costs
            pnl = gross - costs - position["cost"]
            trades.append({"time": as_of, "symbol": symbol, "action": "sell", "price": round(execution_price, 3), "quantity": quantity, "net_pnl": round(pnl, 2), "fee": round(costs, 2), "reason": "跌破 MA20 或 20 日动量转负"})
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for symbol, rows in series_by_symbol.items():
            if symbol in holdings:
                continue
            row = by_date[symbol].get(as_of)
            bars = [item for item in rows if item["time"] <= as_of]
            if not row or len(bars) < 21:
                continue
            ma5 = sum(item["close"] for item in bars[-5:]) / 5.0
            ma20 = sum(item["close"] for item in bars[-20:]) / 20.0
            momentum20 = row["close"] / bars[-21]["close"] - 1.0 if bars[-21]["close"] > 0 else 0.0
            # Compact historical replay of the live ensemble: trend is the
            # hard gate, while breakout/pullback or 3-day momentum confirms
            # timing.  This intentionally remains short/ultra-short.
            recent_high20 = max(item["high"] for item in bars[-20:])
            breakout_ok = row["close"] >= recent_high20 * (1.0 - 0.02)
            pullback_ok = ma5 > ma20 and ma5 <= row["close"] <= ma5 * (1.0 + 0.05)
            regime_momentum = row["close"] / bars[-4]["close"] - 1.0 if len(bars) >= 4 and bars[-4]["close"] > 0 else 0.0
            momentum_ok = regime_momentum >= 0.0
            vote_score = 0.40 + (0.30 if breakout_ok or pullback_ok else 0.0) + (0.30 if momentum_ok else 0.0)
            if row["close"] > ma5 > ma20 and momentum20 > 0 and vote_score >= MODEL_ENSEMBLE_ENTRY_THRESHOLD and (breakout_ok or pullback_ok or momentum_ok):
                candidates.append((momentum20, symbol, row))
        candidates.sort(reverse=True)
        for _, symbol, row in candidates:
            if len(holdings) >= MAX_LOCAL_POSITIONS:
                break
            equity_now = cash + sum(by_date[item].get(as_of, {}).get("close", position["price"]) * position["quantity"] for item, position in holdings.items())
            execution_price = row["close"] * (1.0 + slippage_rate)
            quantity = int((equity_now * 0.10) / execution_price / 100) * 100
            gross = execution_price * quantity
            costs = max(5.0, gross * commission_rate)
            if quantity <= 0 or cash < gross + costs:
                continue
            cash -= gross + costs
            holdings[symbol] = {"quantity": quantity, "price": execution_price, "cost": gross + costs}
            trades.append({"time": as_of, "symbol": symbol, "action": "buy", "price": round(execution_price, 3), "quantity": quantity, "net_pnl": 0.0, "fee": round(costs, 2), "reason": "close > MA5 > MA20 且 20 日动量为正"})
        market_value = sum(by_date[symbol].get(as_of, {}).get("close", position["price"]) * position["quantity"] for symbol, position in holdings.items())
        curve.append({"time": as_of, "value": round(cash + market_value, 2), "cash": round(cash, 2), "market_value": round(market_value, 2), "position_count": len(holdings)})
    if not curve:
        return {"status": "insufficient_data", "reason": "回测窗口没有有效交易日", "errors": errors, "trades": [], "equity_curve": []}
    peak_for_curve = curve[0]["value"]
    for point in curve:
        peak_for_curve = max(peak_for_curve, point["value"])
        point["drawdown_pct"] = round((point["value"] / peak_for_curve - 1.0) * 100.0, 4) if peak_for_curve else 0.0
    values = [float(item["value"]) for item in curve]
    returns = [values[index] / values[index - 1] - 1.0 for index in range(1, len(values)) if values[index - 1] > 0]
    mean_return = statistics.fmean(returns) if returns else 0.0
    volatility = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = math.sqrt(statistics.fmean([value * value for value in downside])) if downside else 0.0
    peak, max_drawdown = values[0], 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0 if peak else 0.0)
    periods = max(1, len(returns))
    annual_return = (values[-1] / initial) ** (252.0 / periods) - 1.0 if values[-1] > 0 else -1.0
    sells = [item for item in trades if item["action"] == "sell"]
    summary = {"initial_equity": round(initial, 2), "final_equity": round(values[-1], 2), "total_return_pct": round((values[-1] / initial - 1.0) * 100.0, 4), "annualized_return_pct": round(annual_return * 100.0, 4), "max_drawdown_pct": round(max_drawdown * 100.0, 4), "sharpe_ratio": round((mean_return / volatility) * math.sqrt(252.0), 4) if volatility > 1e-12 else 0.0, "sortino_ratio": round((mean_return / downside_deviation) * math.sqrt(252.0), 4) if downside_deviation > 1e-12 else 0.0, "calmar_ratio": round(annual_return / abs(max_drawdown), 4) if max_drawdown < -1e-12 else 0.0, "volatility_pct": round(volatility * math.sqrt(252.0) * 100.0, 4), "trade_count": len(trades), "completed_trade_count": len(sells), "win_rate": round(sum(1 for item in sells if item["net_pnl"] > 0) / max(1, len(sells)) * 100.0, 4)}
    return {"status": "valid", "strategy": "short_term_trend_baseline_v1", "model_mode": "weighted_vote", "model_ensemble_version": MODEL_ENSEMBLE_VERSION, "window": {"start": dates[0], "end": dates[-1], "calendar_days": calendar_days}, "symbols": symbols, "errors": errors, "assumptions": {"initial_equity": initial, "entry_weight": 0.10, "max_positions": MAX_LOCAL_POSITIONS, "commission_rate": commission_rate, "stamp_tax_rate": stamp_tax_rate, "slippage_rate": slippage_rate, "lot_size": 100, "ensemble_weights": dict(MODEL_ENSEMBLE_WEIGHTS), "ensemble_entry_threshold": MODEL_ENSEMBLE_ENTRY_THRESHOLD, "trend_required": True, "confirmation_required": 1}, "summary": summary, "equity_curve": curve, "trades": trades}


def _research_pool_items(
    state: dict[str, Any], *, refresh: bool = False
) -> list[dict[str, Any]]:
    """Load the current local watchlist/positions for research jobs.

    The local pool is intentionally the only account source. No broker or
    external account endpoint is consulted.
    """
    # A local execution request should be fast and deterministic.  Reuse the
    # latest pool snapshot when refresh=False instead of hitting every market
    # data source again.  The cached public payload contains all fields needed
    # for signal generation (analysis/current_price/execution metadata).
    if not refresh:
        cached_path = Path.cwd() / POOL_CACHE_FILE
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8")) if cached_path.exists() else None
        except Exception:  # noqa: BLE001 - fall back to live loading
            cached = None
        if isinstance(cached, dict) and cached.get("_cache_version") == POOL_CACHE_VERSION:
            cached_items = list(cached.get("positions") or []) + list(cached.get("watchlist") or [])
            if cached_items:
                return [dict(item) for item in cached_items if isinstance(item, dict)]

    sources = list(state.get("positions") or []) + list(state.get("watchlist") or [])
    dedup: dict[str, dict[str, Any]] = {}
    for item in sources:
        symbol = _code(str(item.get("symbol") or ""))
        if symbol:
            dedup[symbol] = {**item, "symbol": symbol}
    rendered: list[dict[str, Any]] = []
    for item in dedup.values():
        try:
            quote = stock_kline(item["symbol"], refresh=refresh)
            series = dict(quote.get("series") or {})
            rendered.append(
                {
                    **item,
                    **{k: v for k, v in quote.items() if k != "series"},
                    "_ai_series": series,
                    "analysis": _analyze_series(series),
                }
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must not stop a run
            rendered.append({**item, "quote_error": str(exc)})
    return rendered


def _csi300_constituents() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the current CSI 300 membership with an auditable effective date."""
    import akshare as ak

    frame = ak.index_stock_cons_csindex(symbol="000300")
    if frame is None or frame.empty:
        raise ValueError("沪深300成分股接口未返回数据")
    symbol_index: int | None = None
    best_distinct = 0
    for index in range(len(frame.columns)):
        values = [str(value).strip() for value in frame.iloc[:, index].tolist()]
        valid = [value for value in values if re.fullmatch(r"\d{6}", value)]
        distinct = len(set(valid))
        if len(valid) >= max(250, int(len(values) * 0.9)) and distinct > best_distinct:
            symbol_index = index
            best_distinct = distinct
    if symbol_index is None:
        raise ValueError("无法识别沪深300成分股代码列")
    name_index = min(symbol_index + 1, len(frame.columns) - 1)
    items: list[dict[str, Any]] = []
    for row_index in range(len(frame)):
        symbol = _code(str(frame.iloc[row_index, symbol_index]))
        if not symbol:
            continue
        name = _repair_text(frame.iloc[row_index, name_index], symbol)
        items.append(
            {
                "symbol": symbol,
                "name": name,
                "sector_name": "沪深300成分股",
                "universe": "csi300",
            }
        )
    items = list({item["symbol"]: item for item in items}.values())
    if len(items) < 250:
        raise ValueError(f"沪深300成分股有效代码不足: {len(items)}")
    effective_date = str(frame.iloc[0, 0])[:10] if len(frame.columns) else None
    return items, {
        "name": "沪深300",
        "code": "000300",
        "source": "AKShare index_stock_cons_csindex",
        "effective_date": effective_date,
        "constituent_count": len(items),
        "membership_mode": "current_membership_applied_to_recent_window",
    }


def _csi300_research_items(
    *, refresh: bool = True, allowed_providers: tuple[str, ...] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch CSI 300 daily K-lines concurrently for historical ML replay."""
    constituents, metadata = _csi300_constituents()
    if allowed_providers:
        # Fail fast on an unavailable authoritative chain before launching
        # hundreds of concurrent requests. This is especially important when
        # 妙想 returns a quota message: a research run must not spend minutes
        # collecting lower-priority fallback data that will be rejected later.
        probe_symbol = str(constituents[0]["symbol"])
        stock_kline(
            probe_symbol, refresh=refresh, allowed_providers=allowed_providers
        )

    def load_item(item: dict[str, Any]) -> dict[str, Any]:
        try:
            quote = stock_kline(
                str(item["symbol"]), refresh=refresh,
                allowed_providers=allowed_providers,
            )
            return {
                **item,
                "name": _repair_text(quote.get("name"), str(item.get("name") or item["symbol"])),
                "_ai_series": dict(quote.get("series") or {}),
            }
        except Exception as exc:  # noqa: BLE001 - retain per-symbol audit
            return {**item, "quote_error": str(exc)}

    with ThreadPoolExecutor(max_workers=12) as executor:
        rendered = list(executor.map(load_item, constituents))
    metadata["loaded_count"] = sum(not item.get("quote_error") for item in rendered)
    metadata["error_count"] = sum(bool(item.get("quote_error")) for item in rendered)
    return rendered, metadata


def _csi300_plus_watchlist_research_items(
    state: dict[str, Any], *, refresh: bool = True,
    allowed_providers: tuple[str, ...] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return the de-duplicated union of current CSI 300 and local watchlist.

    The watchlist is deliberately additive: current CSI 300 membership remains
    the broad research universe while locally curated names are retained even
    when they are outside the index.  Every item is fetched from the same
    point-in-time daily K-line source so downstream replay and simulation use
    one consistent data contract.
    """
    csi_items, csi_metadata = _csi300_research_items(
        refresh=refresh, allowed_providers=allowed_providers
    )
    merged: dict[str, dict[str, Any]] = {
        str(item.get("symbol")): dict(item)
        for item in csi_items
        if item.get("symbol")
    }
    watchlist_symbols = {
        _code(str(item.get("symbol") or "")): dict(item)
        for item in (state.get("watchlist") or [])
        if _code(str(item.get("symbol") or ""))
    }
    extra_symbols = [symbol for symbol in watchlist_symbols if symbol not in merged]

    def load_extra(symbol: str) -> dict[str, Any]:
        source = watchlist_symbols[symbol]
        try:
            quote = stock_kline(
                symbol, refresh=refresh, allowed_providers=allowed_providers
            )
            return {
                **source,
                "symbol": symbol,
                "name": _repair_text(quote.get("name"), str(source.get("name") or symbol)),
                "sector_name": source.get("sector_name") or quote.get("sector_name"),
                "universe": "watchlist",
                "_ai_series": dict(quote.get("series") or {}),
            }
        except Exception as exc:  # noqa: BLE001 - retain per-symbol audit
            return {**source, "symbol": symbol, "universe": "watchlist", "quote_error": str(exc)}

    if extra_symbols:
        with ThreadPoolExecutor(max_workers=min(12, len(extra_symbols))) as executor:
            for item in executor.map(load_extra, extra_symbols):
                merged[str(item.get("symbol"))] = item

    metadata = {
        **csi_metadata,
        "name": "沪深300+观察池",
        "code": "csi300_plus_watchlist",
        "source": "AKShare index_stock_cons_csindex + local_review_center_state.watchlist",
        "membership_mode": "current_csi300_union_local_watchlist",
        "watchlist_count": len(watchlist_symbols),
        "added_watchlist_count": len(extra_symbols),
        "constituent_count": len(merged),
        "loaded_count": sum(not item.get("quote_error") for item in merged.values()),
        "error_count": sum(bool(item.get("quote_error")) for item in merged.values()),
    }
    return list(merged.values()), metadata


def _historical_selection_score(
    candles: list[dict[str, Any]], volumes: list[float], end: int
) -> float:
    """Recreate the live selection score using only data observable at ``end``."""
    closes = [_float(row.get("close")) for row in candles]
    highs = [_float(row.get("high")) for row in candles]
    close = closes[end]
    ma20 = _mean(closes[end - 19 : end + 1])
    ma60 = _mean(closes[end - 59 : end + 1])
    momentum20 = _return(closes, end, 20)
    rsi14 = _rsi(closes, end)
    volume5 = _mean(volumes[end - 4 : end + 1])
    volume20 = _mean(volumes[end - 19 : end + 1])
    volume_ratio = volume5 / volume20 if volume20 else 1.0
    mfi14 = _money_flow_index(candles, volumes, end)
    mfi_regime = _mfi_regime(
        closes,
        highs,
        volumes,
        end,
        momentum20=momentum20,
        volume_ratio=volume_ratio,
    )
    mfi_filter = _mfi_filter(mfi14, mfi_regime)
    score = 50.0
    score += _clip(momentum20 * 150.0, -20.0, 20.0)
    score += 10.0 if close > ma20 else -10.0
    score += 8.0 if ma20 > ma60 else -8.0
    score += 5.0 if 45.0 <= rsi14 <= 70.0 else (-6.0 if rsi14 >= 80.0 else 0.0)
    score += _clip((volume_ratio - 1.0) * 5.0, -5.0, 5.0)
    score += _float(mfi_filter.get("score_delta"))
    return round(_clip(score, 0.0, 100.0), 1)


def _backfill_historical_ml_samples(
    state: dict[str, Any],
    *,
    calendar_days: int = 60,
    refresh: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str = "pool",
) -> dict[str, Any]:
    """Replay recent real market bars into the local ML evaluation database.

    This is a point-in-time replay, not synthetic price generation. Features
    are calculated from the prediction date and earlier, while labels come
    from the next one/five trading bars. External LLM calls are deliberately
    excluded so the bootstrap is deterministic and does not consume quota.
    """
    storage = _get_ai_service().storage
    requested_start = datetime.fromisoformat(start_date).date() if start_date else None
    requested_end = datetime.fromisoformat(end_date).date() if end_date else None
    if requested_start and requested_end and requested_start > requested_end:
        raise ValueError("历史回放开始日期不能晚于结束日期")
    normalized_universe = str(universe or "pool").strip().lower()
    if normalized_universe in {"csi300", "hs300", "沪深300"}:
        normalized_universe = "csi300"
        items, universe_metadata = _csi300_research_items(
            refresh=refresh, allowed_providers=RESEARCH_ALLOWED_PROVIDERS
        )
    elif normalized_universe in {
        "csi300_plus_watchlist",
        "csi300+watchlist",
        "hs300_plus_watchlist",
        "沪深300+观察池",
        "沪深300加观察池",
        "combined",
    }:
        normalized_universe = "csi300_plus_watchlist"
        items, universe_metadata = _csi300_plus_watchlist_research_items(
            state, refresh=refresh, allowed_providers=RESEARCH_ALLOWED_PROVIDERS
        )
    elif normalized_universe == "pool":
        items = _research_pool_items(state, refresh=refresh)
        universe_metadata = {
            "name": "当前观察池与持仓池",
            "code": "local_pool",
            "source": "local_review_center_state",
            "constituent_count": len(items),
            "membership_mode": "local_pool_snapshot",
        }
    else:
        raise ValueError(f"不支持的历史训练股票池: {universe}")
    if normalized_universe in {"csi300", "csi300_plus_watchlist"}:
        loaded = sum(1 for item in items if item.get("_ai_series") and not item.get("quote_error"))
        if loaded < 250:
            raise RuntimeError(
                f"历史训练股票池数据不足: loaded={loaded}, required>=250, "
                f"universe={normalized_universe}, errors={universe_metadata.get('error_count', 0)}"
            )
    probability_replay = _llm_submodule("traditional").historical_probability_rows
    feature_names = list(_llm_submodule("traditional").FEATURE_NAMES)
    sample_count = 0
    per_symbol: dict[str, int] = {}
    errors: dict[str, str] = {}
    earliest: str | None = None
    latest: str | None = None
    for item in items:
        symbol = str(item.get("symbol") or "")
        if item.get("quote_error"):
            errors[symbol] = str(item.get("quote_error"))
            continue
        series = dict(item.get("_ai_series") or {})
        candles = [
            dict(row)
            for row in list(series.get("candles") or [])
            if _float(row.get("close")) > 0
        ]
        if len(candles) < 96:
            errors[symbol] = "至少需要 96 根日 K 才能进行历史时点回放"
            continue
        volume_by_time = {
            str(row.get("time")): _float(row.get("value"))
            for row in list(series.get("volume") or [])
        }
        closes = [_float(row.get("close")) for row in candles]
        volumes = [volume_by_time.get(str(row.get("time")), 0.0) for row in candles]
        latest_market_date = datetime.fromisoformat(str(candles[-1]["time"])[:10]).date()
        cutoff = requested_start or (
            latest_market_date - timedelta(days=max(1, calendar_days) - 1)
        )
        range_end = requested_end or latest_market_date
        targets = [
            index
            for index, candle in enumerate(candles)
            if index >= 60
            and index + 1 < len(candles)
            and cutoff
            <= datetime.fromisoformat(str(candle.get("time"))[:10]).date()
            <= range_end
        ]
        replay_rows = probability_replay(closes, volumes, targets)
        saved_for_symbol = 0
        symbol_records: list[
            tuple[dict[str, Any], list[dict[str, Any] | float]]
        ] = []
        for index in targets:
            probabilities = replay_rows.get(index) or {}
            next_day = dict(probabilities.get("next_trading_day") or {})
            next_five = dict(probabilities.get("next_5_trading_days") or {})
            if next_day.get("value") is None and next_five.get("value") is None:
                continue
            as_of_date = str(candles[index].get("time"))[:10]
            selection_score = _historical_selection_score(candles, volumes, index)
            swing_score = _llm_submodule("traditional").swing_composite_score(
                selection_score,
                next_day.get("value"),
                next_five.get("value"),
            )
            momentum20 = _return(closes, index, 20)
            volume5 = _mean(volumes[index - 4 : index + 1])
            volume20 = _mean(volumes[index - 19 : index + 1])
            volume_ratio = volume5 / volume20 if volume20 else 1.0
            mfi14 = _money_flow_index(candles, volumes, index)
            mfi_regime = _mfi_regime(
                closes,
                [_float(row.get("high")) for row in candles],
                volumes,
                index,
                momentum20=momentum20,
                volume_ratio=volume_ratio,
            )
            mfi_filter = _mfi_filter(mfi14, mfi_regime)
            feature_values = _feature_row(closes, volumes, index) or []
            analysis_id = f"historical-replay-{symbol}-{as_of_date}-v1"
            final_action = (
                "buy"
                if swing_score >= SWING_BUY_SCORE_THRESHOLD
                and _float(next_day.get("value"), 0.5) >= 0.52
                and _float(next_five.get("value"), 0.5)
                >= SWING_BUY_FIVE_DAY_PROBABILITY_THRESHOLD
                and bool(mfi_filter.get("passed"))
                else "observe"
            )
            result = {
                "schema_version": "historical-market-replay-v3-swing-score",
                "analysis_id": analysis_id,
                "as_of": f"{as_of_date}T15:00:00+08:00",
                "instrument": {
                    "symbol": symbol,
                    "name": item.get("name") or symbol,
                    "sector_name": item.get("sector_name"),
                    "current_price": closes[index],
                },
                "data_quality": {
                    "score": 100,
                    "status": "valid",
                    "source": "real_daily_kline_point_in_time_replay",
                    "synthetic_prices": False,
                },
                "traditional": {
                    "strategy_id": "historical_market_replay_swing_v3",
                    "selection_score": selection_score,
                    "swing_score": swing_score,
                    "close": closes[index],
                    "mfi14": mfi14,
                    "mfi_regime": mfi_regime,
                    "mfi_filter": mfi_filter,
                    "model_features": dict(zip(feature_names, feature_values)),
                    "probabilities": {
                        "next_trading_day": next_day,
                        "next_5_trading_days": next_five,
                    },
                },
                "fusion": {
                    "assessment_status": "historical_replay",
                    "final_action": final_action,
                    "final_score": swing_score,
                    "final_up_probabilities": {
                        "next_trading_day": next_day,
                        "next_5_trading_days": next_five,
                    },
                },
                "audit": {
                    "provider_model": "traditional_point_in_time_replay",
                    "training_universe": normalized_universe,
                    "universe_effective_date": universe_metadata.get("effective_date"),
                    "universe_membership_mode": universe_metadata.get("membership_mode"),
                    "replay_window_calendar_days": calendar_days,
                    "replay_window_start": start_date,
                    "replay_window_end": end_date,
                    "feature_rule": "t_and_earlier_only",
                    "label_rule": "strictly_future_1_and_5_trading_bars",
                    "llm_called": False,
                },
            }
            symbol_records.append(
                (result, candles[index + 1 : index + 6])
            )
            sample_count += 1
            saved_for_symbol += 1
            earliest = min(earliest, as_of_date) if earliest else as_of_date
            latest = max(latest, as_of_date) if latest else as_of_date
        storage.save_labeled_batch(symbol_records)
        per_symbol[symbol] = saved_for_symbol

    performance = storage.performance_report()
    training = storage.train_candidate_models(
        universe=(
            normalized_universe
            if normalized_universe in {"csi300", "csi300_plus_watchlist"}
            else None
        ),
        start_date=start_date,
        end_date=end_date,
    )
    result = {
        "status": "completed" if sample_count else "insufficient_data",
        "source": "real_daily_kline_point_in_time_replay",
        "synthetic_prices": False,
        "window": {
            "start": earliest,
            "end": latest,
            "requested_start": start_date,
            "requested_end": end_date,
            "calendar_days": calendar_days,
        },
        "sample_count": sample_count,
        "universe": universe_metadata,
        "per_symbol": per_symbol,
        "errors": errors,
        "performance": performance,
        "training": training,
        "leakage_control": {
            "features": "prediction bar t and earlier",
            "labels": "future 1/5 trading bars",
            "llm_calls": 0,
        },
    }
    artifact = persist_artifact(
        Path.cwd() / "research_artifacts",
        f"historical-train-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        result,
    )
    result["artifact_path"] = artifact
    return result


def _run_research_action(
    action: str,
    state: dict[str, Any],
    *,
    calendar_days: int = 60,
    refresh: bool = True,
    start_date: str | None = None,
    end_date: str | None = None,
    universe: str = "pool",
) -> dict[str, Any]:
    """Run a local, auditable research action against the pool ledger."""
    storage = _get_ai_service().storage
    if action == "historical_train":
        return _backfill_historical_ml_samples(
            state,
            calendar_days=calendar_days,
            refresh=refresh,
            start_date=start_date,
            end_date=end_date,
            universe=universe,
        )
    if action in {"backtest", "labels", "metrics", "optimize", "full"}:
        if str(universe).lower() == "csi300":
            items, universe_meta = _csi300_research_items(
                refresh=refresh, allowed_providers=RESEARCH_ALLOWED_PROVIDERS
            )
        elif str(universe).lower() == "csi300_plus_watchlist":
            items, universe_meta = _csi300_plus_watchlist_research_items(
                state, refresh=refresh, allowed_providers=RESEARCH_ALLOWED_PROVIDERS
            )
        else:
            items = _research_pool_items(state, refresh=refresh)
            universe_meta = {"loaded_count": len(items), "error_count": 0}
        series_by_symbol = {
            str(item.get("symbol")): item.get("_ai_series")
            for item in items
            if item.get("symbol") and item.get("_ai_series")
        }
        # Never create an apparently valid research/model artifact from a
        # partially loaded broad universe.  Upstream quote endpoints can
        # transiently close connections; proceeding with 7/327 symbols would
        # produce misleading CPCV metrics and pollute the Challenger registry.
        broad_universe = str(universe).lower() in {"csi300", "csi300_plus_watchlist"}
        minimum_symbols = 250 if broad_universe else 1
        if len(series_by_symbol) < minimum_symbols:
            raise RuntimeError(
                f"研究股票池数据不足: loaded={len(series_by_symbol)}, "
                f"required>={minimum_symbols}, universe={universe}, "
                f"errors={universe_meta.get('error_count', 0)}"
            )
        dataset = build_dataset_bundle(series_by_symbol)
        dataset_meta = {
            "snapshot": dataset.get("snapshot"),
            "data_quality": dataset.get("data_quality"),
            "feature_version": dataset.get("feature_version"),
            "label_version": dataset.get("label_version"),
            "feature_names": dataset.get("feature_names", []),
            "label_names": dataset.get("label_names", []),
            "row_counts": dataset.get("row_counts", {}),
        }
        labeled = 0
        errors: dict[str, str] = {}
        if action in {"labels", "full"}:
            for item in items:
                symbol = str(item.get("symbol") or "")
                if item.get("quote_error"):
                    errors[symbol] = str(item.get("quote_error"))
                    continue
                before = len(storage.pending_labels(symbol))
                _update_outcome_labels(item)
                after = len(storage.pending_labels(symbol))
                labeled += max(0, before - after)
        if action == "labels":
            result = {"status": "completed", "dataset": dataset_meta, "labeled_count": labeled, "errors": errors}
            artifact = persist_artifact(Path.cwd() / "research_artifacts", f"labels-{dataset['snapshot']['version']}", result)
            result["artifact_path"] = artifact
            return result
        if action == "metrics":
            result = {"status": "completed", "dataset": dataset_meta, "performance": storage.performance_report(), "training_dataset": storage.training_dataset_summary()}
            artifact = persist_artifact(Path.cwd() / "research_artifacts", f"metrics-{dataset['snapshot']['version']}", result)
            result["artifact_path"] = artifact
            return result
        strategies: dict[str, Any] = {}
        wfo: dict[str, Any] = {}
        base_params = {
            "initial_cash": REVIEW_INITIAL_EQUITY,
            "max_positions": MAX_LOCAL_POSITIONS,
            "entry_weight": 0.10,
            "commission_rate": 0.0003,
            "stamp_tax_rate": 0.0005,
            "slippage_rate": 0.001,
        }
        standard_param_grid = {
            "fast_window": [3, 5, 8],
            "slow_window": [30, 20, 15],
            "momentum_window": [20, 10],
            "volatility_cap": [0.045, 0.035, 0.025, 0.055],
            "momentum_vol_weight": [0.5, 1.0, 1.5],
            "trailing_atr_multiple": [2.5, 2.0, 1.5, 3.0],
            "max_holding_bars": [15, 20, 10, 30],
            "momentum_exit_threshold": [-0.01, 0.0, -0.02],
            "max_entry_momentum": [0.25, 0.18, 0.35],
        }
        breakout_param_grid = {
            **standard_param_grid,
            "breakout_tolerance": [0.01, 0.0, 0.02, 0.03],
            "pullback_tolerance": [0.02, 0.01, 0.03, 0.05],
            "min_volume_ratio": [1.0, 0.9, 1.2, 1.5],
            "breakout_entry_mode": [
                "strict_breakout_or_pullback",
                "breakout_only",
                "pullback_only",
            ],
            "breakout_rank_bonus": [0.10, 0.25, 0.50],
        }
        # Momentum regime is deliberately constrained to the user's short-term
        # trend/swing horizon.  The previous shared grid allowed slow 30-bar
        # trend filters and long holding periods to win the in-sample score;
        # on the 327-name universe those choices were unstable after regime
        # changes.  Keep the search broad enough to discover robust settings,
        # but exclude medium/long-horizon candidates that are outside scope.
        momentum_param_grid = {
            "fast_window": [3, 5, 8],
            "slow_window": [15, 20],
            "momentum_window": [10, 20],
            "volatility_cap": [0.035, 0.045, 0.055],
            "momentum_vol_weight": [0.5, 1.0, 1.5],
            "trailing_atr_multiple": [2.0, 2.5, 3.0],
            "max_holding_bars": [10, 15, 20],
            "momentum_exit_threshold": [-0.02, -0.01, 0.0],
            "max_entry_momentum": [0.18, 0.25, 0.35],
            "regime_window": [3, 5, 10],
            "regime_momentum_threshold": [0.0, 0.01, 0.02],
            "regime_acceleration_threshold": [-0.005, 0.0, 0.005],
            "regime_exit_threshold": [-0.02, -0.01, 0.0],
            "min_market_breadth": [0.30, 0.40, 0.50, 0.60],
        }
        standard_validation_grid = {
            "fast_window": [5],
            "slow_window": [20],
            "momentum_window": [20],
            "volatility_cap": [0.045],
            "momentum_vol_weight": [1.0],
            "trailing_atr_multiple": [2.5],
            "max_holding_bars": [15],
            "momentum_exit_threshold": [-0.01, 0.0],
            "max_entry_momentum": [0.25],
        }
        breakout_validation_grid = {
            "fast_window": [3],
            "slow_window": [30],
            "momentum_window": [20],
            "volatility_cap": [0.045],
            "momentum_vol_weight": [0.5],
            "trailing_atr_multiple": [2.5],
            "max_holding_bars": [15],
            "momentum_exit_threshold": [-0.01],
            "max_entry_momentum": [0.25],
            "breakout_tolerance": [0.01, 0.03],
            "pullback_tolerance": [0.02, 0.05],
            "min_volume_ratio": [0.9],
            "breakout_entry_mode": [
                "strict_breakout_or_pullback",
                "breakout_only",
                "pullback_only",
            ],
            "breakout_rank_bonus": [0.10],
        }
        # CPCV validation uses a compact, style-consistent grid.  In
        # particular, do not let a single training fold select slow_window=30
        # and then label that unstable choice as the candidate's robustness.
        momentum_validation_grid = {
            "fast_window": [5, 8],
            "slow_window": [20],
            "momentum_window": [20],
            "volatility_cap": [0.035, 0.045],
            "momentum_vol_weight": [1.0],
            "trailing_atr_multiple": [2.5],
            "max_holding_bars": [15],
            "momentum_exit_threshold": [-0.01],
            "max_entry_momentum": [0.18],
            "regime_window": [3, 5],
            "regime_momentum_threshold": [0.0],
            "regime_acceleration_threshold": [0.0],
            "regime_exit_threshold": [-0.01],
            "min_market_breadth": [0.30, 0.40, 0.50],
        }
        optimization_grids = {
            "trend_swing": standard_param_grid,
            "breakout_pullback": breakout_param_grid,
            "momentum_regime": momentum_param_grid,
        }
        validation_grids = {
            "trend_swing": standard_validation_grid,
            "breakout_pullback": breakout_validation_grid,
            "momentum_regime": momentum_validation_grid,
        }
        for strategy_name in ("trend_swing", "breakout_pullback", "momentum_regime"):
            strategies[strategy_name] = simulate_strategy(series_by_symbol, strategy=strategy_name, **base_params)
            wfo[strategy_name] = walk_forward_optimize(
                series_by_symbol,
                validation_grids[strategy_name],
                strategy=strategy_name,
                initial_cash=REVIEW_INITIAL_EQUITY,
                train_bars=120,
                test_bars=30,
            )
        optuna_storage = Path.cwd() / "research_artifacts" / "optuna_trials.sqlite3"
        optimization = {
            name: multi_objective_optimize(
                series_by_symbol,
                optimization_grids[name],
                strategy=name,
                initial_cash=REVIEW_INITIAL_EQUITY,
                max_trials=90 if name == "momentum_regime" else 60,
                storage_path=optuna_storage,
                study_name=(
                    f"akquant-{SIMULATOR_VERSION}-{dataset['snapshot']['version']}-{name}"
                    + ("-shortgrid2" if name == "momentum_regime" else "")
                ),
            )
            for name in strategies
        }
        optimized_strategy_runs = {
            name: simulate_strategy(
                series_by_symbol,
                strategy=name,
                **base_params,
                **dict(((optimization[name].get("best") or {}).get("params") or {})),
            )
            for name in strategies
        }
        strategy_distinctness = _strategy_distinctness_audit(
            optimized_strategy_runs
        )
        purged = {
            name: purged_walk_forward_optimize(
                series_by_symbol, validation_grids[name], strategy=name,
                initial_cash=REVIEW_INITIAL_EQUITY, train_bars=120,
                test_bars=30, purge_bars=5, embargo_bars=2,
            )
            for name in strategies
        }
        robustness = {}
        for name, value in strategies.items():
            summary = value.get("summary") or {}
            trial_scores = [
                float(item.get("sharpe_ratio") or 0.0)
                for item in (optimization[name].get("trials") or [])
                if item.get("state") == "complete"
            ]
            robustness[name] = {
                "deflated_sharpe_ratio": deflated_sharpe_ratio(float(summary.get("sharpe_ratio") or 0.0), max(1, optimization[name].get("trial_count", 1))),
                "pbo": probability_of_backtest_overfitting(trial_scores),
                "purged_walk_forward": {"status": purged[name].get("status"), "summary": purged[name].get("summary", {})},
            }
        best_name = max(strategies, key=lambda name: float((strategies[name].get("summary") or {}).get("sharpe_ratio") or -999.0)) if strategies else None
        if best_name:
            model_version = f"{dataset['snapshot']['version']}-{SIMULATOR_VERSION}"
            # Each standalone optimization is a new Challenger round.  Keep
            # the dataset/simulator lineage but add a run nonce so a prior
            # superseded row is never silently reused or relabeled.
            if action == "optimize":
                model_version = f"{model_version}-tuned-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            baseline_model_id = f"champion-baseline-{model_version}"
            active_champion = storage.active_champion()
            if active_champion is None or (
                active_champion.get("model_id") == baseline_model_id
                and not active_champion.get("published_at")
            ):
                storage.register_model(
                    baseline_model_id,
                    model_name=best_name, role="champion", status="active_paper",
                    version=model_version,
                    metrics={
                        **(strategies[best_name].get("summary") or {}),
                        "simulator_version": SIMULATOR_VERSION,
                    },
                )
            for strategy_name, candidate in optimization.items():
                candidate_metrics = {
                    "best": candidate.get("best") or {},
                    "completed_trial_count": candidate.get("completed_trial_count", 0),
                    "pruned_trial_count": candidate.get("pruned_trial_count", 0),
                    "study_name": candidate.get("study_name"),
                    "storage": candidate.get("storage"),
                    "simulator_version": SIMULATOR_VERSION,
                    "cpcv_status": purged[strategy_name].get("status"),
                    "cpcv": {
                        "status": purged[strategy_name].get("status"),
                        "summary": purged[strategy_name].get("summary", {}),
                        "validation": purged[strategy_name].get("validation", {}),
                    },
                    "robustness": robustness[strategy_name],
                    "pbo": robustness[strategy_name].get("pbo"),
                    "strategy_distinctness": strategy_distinctness[strategy_name],
                }
                storage.register_model(
                    f"challenger-{strategy_name}-{model_version}",
                    model_name=strategy_name, role="challenger", status="evaluated",
                    version=model_version, metrics=candidate_metrics,
                )
        if action == "optimize":
            result = {"status": "completed", "dataset": dataset_meta, "optimization": optimization, "purged_walk_forward": purged, "robustness": robustness, "strategy_distinctness": strategy_distinctness, "models": storage.list_models()}
            artifact = persist_artifact(Path.cwd() / "research_artifacts", f"optimize-{dataset['snapshot']['version']}", result)
            result["artifact_path"] = artifact
            return result
        backtest = _review_baseline_backtest(state, calendar_days=calendar_days)
        (Path.cwd() / BACKTEST_CACHE_FILE).write_text(
            json.dumps(backtest, ensure_ascii=False, default=str), encoding="utf-8"
        )
        backtest["research_strategies"] = {
            name: {"status": value.get("status"), "summary": value.get("summary", {})}
            for name, value in strategies.items()
        }
        backtest["walk_forward"] = {
            name: {"status": value.get("status"), "summary": value.get("summary", {}), "window_count": len(value.get("windows", []))}
            for name, value in wfo.items()
        }
        if action == "backtest":
            result = {"status": "completed", "dataset": dataset_meta, "backtest": backtest, "strategies": strategies, "walk_forward": wfo}
            artifact = persist_artifact(Path.cwd() / "research_artifacts", f"backtest-{dataset['snapshot']['version']}", result)
            result["artifact_path"] = artifact
            return result
        performance = storage.performance_report()
        training = storage.train_candidate_models(
            universe=universe if universe in {"csi300", "csi300_plus_watchlist"} else None
        )
        result = {
            "status": "completed",
            "dataset": dataset_meta,
            "labels": {"labeled_count": labeled, "errors": errors},
            "backtest": backtest,
            "strategies": {name: {"summary": value.get("summary", {}), "trade_count": len(value.get("trades", []))} for name, value in strategies.items()},
            "walk_forward": wfo,
            "optimization": optimization,
            "purged_walk_forward": purged,
            "robustness": robustness,
            "strategy_distinctness": strategy_distinctness,
            "models": storage.list_models(),
            "performance": performance,
            "training": training,
        }
        artifact = persist_artifact(Path.cwd() / "research_artifacts", f"full-{dataset['snapshot']['version']}", result)
        result["artifact_path"] = artifact
        return result
    if action == "train":
        result = storage.train_candidate_models(
            universe=universe if universe == "csi300" else None,
            start_date=start_date,
            end_date=end_date,
        )
        artifact = persist_artifact(Path.cwd() / "research_artifacts", f"train-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}", result)
        result["artifact_path"] = artifact
        return result
    raise ValueError(f"不支持的研究动作: {action}")


def _backtest_trade_metrics(backtest: dict[str, Any]) -> dict[str, Any]:
    """Derive decision-useful trade diagnostics from one compact backtest."""
    trades = list(backtest.get("trades") or [])
    sells = [item for item in trades if str(item.get("action") or "").lower() == "sell"]
    pnl = [_float(item.get("net_pnl")) for item in sells]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    consecutive = 0
    max_consecutive_losses = 0
    for value in pnl:
        consecutive = consecutive + 1 if value < 0 else 0
        max_consecutive_losses = max(max_consecutive_losses, consecutive)
    return {
        "completed_trade_count": len(sells),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "average_win": round(gross_profit / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "payoff_ratio": round((gross_profit / len(wins)) / abs(sum(losses) / len(losses)), 4)
        if wins and losses and abs(sum(losses)) > 1e-12
        else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 1e-12 else None,
        "expectancy": round(sum(pnl) / len(pnl), 2) if pnl else None,
        "max_consecutive_losses": max_consecutive_losses,
    }


def _strategy_distinctness_audit(
    optimized_runs: dict[str, dict[str, Any]], *, maximum_overlap: float = 0.85
) -> dict[str, dict[str, Any]]:
    """Measure whether optimized strategies actually produce distinct trades."""

    def events(result: dict[str, Any], action: str | None = None) -> set[tuple[str, str, str]]:
        return {
            (
                str(item.get("time") or "")[:10],
                str(item.get("symbol") or ""),
                str(item.get("action") or "").lower(),
            )
            for item in list(result.get("trades") or [])
            if (action is None or str(item.get("action") or "").lower() == action)
        }

    def overlap(left: set[tuple[str, str, str]], right: set[tuple[str, str, str]]) -> float:
        union = left | right
        if not union:
            return 1.0
        return len(left & right) / len(union)

    all_events = {name: events(result) for name, result in optimized_runs.items()}
    buy_events = {
        name: events(result, "buy") for name, result in optimized_runs.items()
    }
    audit: dict[str, dict[str, Any]] = {}
    for name in optimized_runs:
        peers: dict[str, dict[str, float]] = {}
        for peer in optimized_runs:
            if peer == name:
                continue
            peers[peer] = {
                "trade_event_overlap": round(
                    overlap(all_events[name], all_events[peer]), 6
                ),
                "entry_event_overlap": round(
                    overlap(buy_events[name], buy_events[peer]), 6
                ),
            }
        max_trade_overlap = max(
            (item["trade_event_overlap"] for item in peers.values()), default=0.0
        )
        max_entry_overlap = max(
            (item["entry_event_overlap"] for item in peers.values()), default=0.0
        )
        audit[name] = {
            "status": "valid",
            "passed": max_trade_overlap <= maximum_overlap,
            "maximum_allowed_overlap": maximum_overlap,
            "max_peer_trade_overlap": max_trade_overlap,
            "max_peer_entry_overlap": max_entry_overlap,
            "trade_event_count": len(all_events[name]),
            "entry_event_count": len(buy_events[name]),
            "peers": peers,
        }
    return audit


def _backtest_dashboard_payload(
    state: dict[str, Any],
    pool_payload: dict[str, Any] | None = None,
    *,
    storage: Any | None = None,
    ai_status: dict[str, Any] | None = None,
    force_current_backtest: bool = False,
) -> dict[str, Any]:
    """Build one bounded, source-backed payload for the analysis dashboard.

    The report used to fetch the pool, baseline backtest, model registry and a
    several-hundred-kilobyte research run independently.  This compact view
    reconciles those sources on the server and exposes only the fields required
    for analysis, which materially reduces page load and avoids metric drift.
    """
    service = _get_ai_service() if storage is None or ai_status is None else None
    storage = storage or service.storage
    ai_status = dict(ai_status or service.status())
    pool_payload = dict(pool_payload or {})

    # Load only lightweight run metadata first.  The newest optimize result
    # can be hundreds of megabytes and decoding it on every report refresh
    # blocks the HTTP response.  Fetch the single selected artifact lazily.
    try:
        runs = storage.list_research_runs(limit=20, include_results=False)
    except TypeError:  # backwards-compatible storage/test doubles
        runs = storage.list_research_runs(limit=20)
    completed_full = next(
        (
            item for item in runs
            if item.get("status") == "completed" and item.get("action") == "full"
        ),
        None,
    )
    completed_other = next(
        (
            item for item in runs
            if item.get("status") == "completed"
            and item.get("action") in {"full", "optimize", "backtest", "daily_auto"}
        ),
        None,
    )
    selected_run_id = str((completed_full or completed_other or {}).get("run_id") or "")
    if selected_run_id and hasattr(storage, "get_research_run"):
        selected = storage.get_research_run(selected_run_id, include_result=True)
        if selected:
            runs = [selected] + [item for item in runs if item.get("run_id") != selected_run_id]
    # Prefer a complete research artifact for report-wide fields.  An
    # ``optimize`` run is intentionally compact and omits dataset quality and
    # Walk-Forward summaries; selecting it first makes the dashboard display
    # null quality and falsely mark every strategy as WFO-regressed.
    latest_complete_run = next(
        (
            item
            for item in runs
            if item.get("status") == "completed"
            and isinstance(item.get("result"), dict)
            and item.get("action") == "full"
            and isinstance((item.get("result") or {}).get("dataset"), dict)
            and isinstance((item.get("result") or {}).get("walk_forward"), dict)
        ),
        None,
    )
    latest_run = latest_complete_run or next(
        (
            item
            for item in runs
            if item.get("status") == "completed"
            and isinstance(item.get("result"), dict)
            and item.get("action") in {"full", "optimize", "backtest", "daily_auto"}
        ),
        None,
    )
    latest_replay_run = next(
        (
            item
            for item in runs
            if item.get("status") == "completed"
            and item.get("action") == "historical_train"
            and isinstance(item.get("result"), dict)
        ),
        None,
    )
    latest_training_run = next(
        (
            item
            for item in runs
            if item.get("status") == "completed"
            and item.get("action") in {"historical_train", "train"}
            and isinstance(item.get("result"), dict)
        ),
        None,
    )
    # Dataset lineage and backtest metrics are not always produced by the
    # same research action.  In particular, ``historical_train`` records the
    # full CSI300+watchlist scope, while an older ``full`` artifact may carry
    # a much smaller 40–42 symbol snapshot.  Keep the complete artifact for
    # backtest/WFO fields, but prefer the newest explicit training scope for
    # dataset cards and source metadata.
    latest_dataset_run = next(
        (
            item
            for item in runs
            if item.get("status") == "completed"
            and isinstance(item.get("result"), dict)
            and isinstance((item.get("result") or {}).get("dataset"), dict)
            and isinstance(((item.get("result") or {}).get("dataset") or {}).get("snapshot"), dict)
        ),
        None,
    )
    dataset_source_run = latest_dataset_run
    dataset_scope = dict(
        (((latest_training_run or {}).get("result") or {}).get("training") or {}).get("dataset_scope")
        or {}
    )
    if latest_training_run and dataset_scope.get("symbol_count"):
        def _run_time(item: dict[str, Any] | None) -> str:
            return str((item or {}).get("started_at") or "")

        dataset_quality = dict(
            (((latest_dataset_run or {}).get("result") or {}).get("dataset") or {}).get("data_quality")
            or {}
        )
        dataset_symbols = int(_float(dataset_quality.get("symbol_count"), 0.0))
        scope_symbols = int(_float(dataset_scope.get("symbol_count"), 0.0))
        # Never let a newer but narrower artifact (for example a local
        # 40–42-symbol full run) hide the broad universe used for training.
        if (
            not latest_dataset_run
            or dataset_symbols < scope_symbols
            or _run_time(latest_training_run) >= _run_time(latest_dataset_run)
        ):
            dataset_source_run = latest_training_run
    # The historical-training run is the authoritative lineage for the
    # user's requested six-month CSI300+watchlist sample.  An optimize run
    # may reuse the same 327 symbols but persist a wider market-history
    # snapshot for simulator warm-up; using that snapshot's dates in the
    # dashboard made it look as if training had ignored the six-month window.
    dataset_lineage_run = (
        latest_training_run if latest_training_run and dataset_scope.get("symbol_count") else dataset_source_run
    )
    raw_result = dict((latest_run or {}).get("result") or {})
    if isinstance(raw_result.get("research"), dict):
        raw_result = dict(raw_result["research"])

    backtest = {} if force_current_backtest else dict(raw_result.get("backtest") or {})
    # A historical research artifact can legitimately contain an
    # ``insufficient_data`` placeholder from an earlier provider outage.  It
    # is not a usable result and must not block rebuilding the current report
    # from the live pool.  Previously this truthy placeholder was treated as
    # a complete backtest, leaving the dashboard with empty recent returns
    # and drawdown charts even though /api/backtest/review-baseline could
    # produce a valid curve.
    needs_current_backtest = (
        force_current_backtest
        or not backtest
        or str(backtest.get("status") or "") == "insufficient_data"
    )
    if needs_current_backtest:
        try:
            cached = json.loads((Path.cwd() / BACKTEST_CACHE_FILE).read_text(encoding="utf-8"))
            backtest = cached if isinstance(cached, dict) else {}
        except (OSError, json.JSONDecodeError):
            backtest = {}
    # A pool refresh invalidates the compact cache.  Rebuild the report from
    # the current local ledger when no research artifact is available so the
    # dashboard never presents an empty or stale backtest after a refresh.
    if (
        not backtest
        or str(backtest.get("status") or "") == "insufficient_data"
    ):
        backtest = _review_baseline_backtest(state, calendar_days=60)
        if force_current_backtest and backtest:
            try:
                Path.cwd().joinpath(BACKTEST_CACHE_FILE).write_text(
                    json.dumps(backtest, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            except OSError:
                pass
    backtest_summary = dict(backtest.get("summary") or {})
    symbol_names: dict[str, str] = {}
    for source in (
        list(state.get("positions") or [])
        + list(state.get("watchlist") or [])
        + list(state.get("manual_trades") or [])
        + list(pool_payload.get("positions") or [])
        + list(pool_payload.get("watchlist") or [])
    ):
        symbol = str(source.get("symbol") or "")
        name = str(source.get("name") or "").strip()
        if symbol and name and name != symbol:
            symbol_names[symbol] = name
    equity_curve = [
        {
            "time": str(item.get("time") or "")[:10],
            "value": round(_float(item.get("value")), 2),
            "drawdown_pct": round(_float(item.get("drawdown_pct", item.get("drawdown"))), 4),
            "position_count": int(_float(item.get("position_count"))),
        }
        for item in list(backtest.get("equity_curve") or [])[-240:]
    ]
    backtest_trades = [
        {
            "time": str(item.get("time") or ""),
            "symbol": str(item.get("symbol") or ""),
            "name": str(
                item.get("name")
                or symbol_names.get(str(item.get("symbol") or ""))
                or "历史标的"
            ),
            "action": str(item.get("action") or ""),
            "price": round(_float(item.get("price")), 3),
            "quantity": int(_float(item.get("quantity"))),
            "net_pnl": round(_float(item.get("net_pnl")), 2),
            "reason": str(item.get("reason") or ""),
        }
        for item in list(backtest.get("trades") or [])[-200:]
    ]

    try:
        models = storage.list_models(include_snapshots=False)
    except TypeError:  # backwards-compatible storage/test doubles
        models = storage.list_models()
    try:
        active_champion = storage.active_champion(include_snapshot=False)
    except TypeError:  # backwards-compatible storage/test doubles
        active_champion = storage.active_champion()
    # ``list_models`` is newest-first.  For the report, prefer the newest
    # published component actually used by the live weighted-vote ensemble;
    # falling back to a Challenger is only appropriate when a strategy has no
    # published component yet.  Previously this always selected the newest
    # *unpublished* Challenger, so a report displayed "发布门槛未通过" even
    # though the strategy was already live in the ensemble.
    published_by_name: dict[str, dict[str, Any]] = {}
    challenger_by_name: dict[str, dict[str, Any]] = {}
    for item in models:
        name = str(item.get("model_name") or "")
        if (
            item.get("published_at")
            and item.get("status") in {"active_paper", "superseded", "rolled_back"}
            and name
        ):
            previous = published_by_name.get(name)
            if previous is None or str(item.get("published_at")) > str(previous.get("published_at")):
                published_by_name[name] = item
        if item.get("role") == "challenger" and name:
            challenger_by_name.setdefault(name, item)
    strategy_results = dict(raw_result.get("strategies") or {})
    optimization = dict(raw_result.get("optimization") or {})
    cpcv_results = dict(raw_result.get("purged_walk_forward") or {})
    robustness_results = dict(raw_result.get("robustness") or {})
    walk_forward = dict(raw_result.get("walk_forward") or {})
    preferred = ["trend_swing", "breakout_pullback", "momentum_regime"]
    strategy_names = preferred + sorted(
        (set(strategy_results) | set(optimization) | set(challenger_by_name)) - set(preferred)
    )
    strategy_rows: list[dict[str, Any]] = []
    for name in strategy_names:
        model = published_by_name.get(name) or challenger_by_name.get(name) or {}
        live_published = bool(
            model.get("published_at")
            and model.get("status") in {"active_paper", "superseded", "rolled_back"}
        )
        metrics = dict(model.get("metrics") or {})
        baseline = dict((strategy_results.get(name) or {}).get("summary") or {})
        best = dict(metrics.get("best") or (optimization.get(name) or {}).get("best") or {})
        cpcv = dict(
            ((metrics.get("cpcv") or {}).get("summary") or {})
            or ((cpcv_results.get(name) or {}).get("summary") or {})
        )
        robustness = dict(metrics.get("robustness") or robustness_results.get(name) or {})
        distinctness = dict(metrics.get("strategy_distinctness") or {})
        wfo = dict((walk_forward.get(name) or {}).get("summary") or {})
        if live_published:
            # A published component has already passed the release workflow.
            # It is not a Challenger and must not be re-evaluated against the
            # current Champion (which would produce a misleading failure).
            gate = {
                "passed": True,
                "mode": "published_ensemble_component",
                "message": "该组件已有审计发布记录，当前作为组合组件运行",
                "checks": [],
            }
        else:
            try:
                gate = storage.evaluate_release_gate(str(model.get("model_id"))) if model else None
            except ValueError:
                gate = None
        if not any((baseline, best, cpcv, robustness, wfo, model)):
            continue
        strategy_rows.append(
            {
                "strategy": name,
                "baseline": baseline,
                "optimized": {
                    key: best.get(key)
                    for key in (
                        "sharpe_ratio",
                        "total_return_pct",
                        "max_drawdown_pct",
                        "trade_count",
                        "completed_trade_count",
                        "win_rate",
                    )
                },
                "best_params": dict(best.get("params") or {}),
                "optimization": {
                    "completed_trials": int(
                        _float(metrics.get("completed_trial_count", (optimization.get(name) or {}).get("completed_trial_count")))
                    ),
                    "pruned_trials": int(
                        _float(metrics.get("pruned_trial_count", (optimization.get(name) or {}).get("pruned_trial_count")))
                    ),
                },
                "cpcv": cpcv,
                "walk_forward": wfo,
                "robustness": {
                    "deflated_sharpe_ratio": robustness.get("deflated_sharpe_ratio"),
                    "pbo": metrics.get("pbo", robustness.get("pbo")),
                },
                "model": {
                    "model_id": model.get("model_id"),
                    "status": model.get("status"),
                    "version": model.get("version"),
                    "live_published": live_published,
                    "published_at": model.get("published_at"),
                    "simulator_version": metrics.get("simulator_version"),
                    "release_gate_passed": bool((gate or {}).get("passed")),
                    "strategy_distinctness_passed": bool(
                        distinctness.get("passed")
                    ),
                    "max_peer_trade_overlap": distinctness.get(
                        "max_peer_trade_overlap"
                    ),
                    "max_peer_entry_overlap": distinctness.get(
                        "max_peer_entry_overlap"
                    ),
                },
            }
        )
    robust_candidates = [
        row for row in strategy_rows if row["model"].get("release_gate_passed")
    ] or strategy_rows
    best_strategy = max(
        robust_candidates,
        key=lambda row: (
            _float(row.get("cpcv", {}).get("mean_test_sharpe"), -999.0),
            _float(row.get("optimized", {}).get("sharpe_ratio"), -999.0),
        ),
        default=None,
    )

    positions = list(pool_payload.get("positions") or state.get("positions") or [])
    watchlist = list(pool_payload.get("watchlist") or state.get("watchlist") or [])
    manual_trades = list(pool_payload.get("manual_trades") or state.get("manual_trades") or [])
    items_by_symbol = {
        str(item.get("symbol") or ""): item for item in positions + watchlist
    }
    signals: list[dict[str, Any]] = []
    for signal in list(pool_payload.get("signals") or []):
        symbol = str(signal.get("symbol") or "")
        source = items_by_symbol.get(symbol) or {}
        current = _float(signal.get("current_price"))
        stop = _float(signal.get("stop_price"))
        take = _float(signal.get("take_profit_price"))
        risk = max(0.0, current - stop) if current and stop else 0.0
        reward = max(0.0, take - current) if current and take else 0.0
        probabilities = signal.get("probabilities") or {}
        signals.append(
            {
                "symbol": symbol,
                "name": str(signal.get("name") or source.get("name") or symbol),
                "pool": str(signal.get("pool") or ""),
                "sector": str(
                    source.get("sector_name")
                    or source.get("sector")
                    or _local_sector_name(symbol, source.get("name"))
                    or "未分类"
                ),
                "action": str(signal.get("action") or "观察"),
                "selection_rank": int(_float(signal.get("selection_rank"))),
                "selection_score": round(_float(signal.get("selection_score")), 2),
                "swing_score": round(
                    _float(signal.get("swing_score"), _float(signal.get("selection_score"))),
                    2,
                ),
                "up_probability_1d": signal.get("up_probability"),
                "up_probability_5d": (probabilities.get("next_5_trading_days") or {}).get("value"),
                "validation_accuracy": signal.get("validation_accuracy"),
                "assessment_status": signal.get("assessment_status"),
                "current_price": current,
                "suggested_price": signal.get("suggested_price"),
                "momentum20": signal.get("momentum20"),
                "mfi14": signal.get("mfi14"),
                "mfi_regime": signal.get("mfi_regime"),
                "mfi_filter_passed": bool((signal.get("mfi_filter") or {}).get("passed", True)),
                "trend": signal.get("trend"),
                "trend_direction": signal.get("trend_direction"),
                "stop_price": signal.get("stop_price"),
                "take_profit_price": signal.get("take_profit_price"),
                "risk_reward_ratio": round(reward / risk, 3) if risk > 1e-12 else None,
                "execution_ready": bool(signal.get("execution_signal")),
                "reason": str(signal.get("reason") or signal.get("evaluation") or ""),
                "active_model": signal.get("active_model"),
            }
        )
    signals.sort(
        key=lambda item: (
            item.get("selection_rank") or 9999,
            -_float(item.get("swing_score"), _float(item.get("selection_score"))),
        )
    )
    action_counts: dict[str, int] = {}
    sector_aggregate: dict[str, dict[str, Any]] = {}
    for item in signals:
        action_counts[item["action"]] = action_counts.get(item["action"], 0) + 1
        sector = sector_aggregate.setdefault(
            item["sector"], {"sector": item["sector"], "count": 0, "score_total": 0.0, "buy_candidates": 0}
        )
        sector["count"] += 1
        sector["score_total"] += _float(
            item.get("swing_score"), _float(item.get("selection_score"))
        )
        sector["buy_candidates"] += int(item.get("action") in {"买入", "等待买入"})
    sector_rows = [
        {
            "sector": item["sector"],
            "count": item["count"],
            "buy_candidates": item["buy_candidates"],
            "average_score": round(item["score_total"] / max(1, item["count"]), 2),
        }
        for item in sector_aggregate.values()
    ]
    sector_rows.sort(key=lambda item: (item["buy_candidates"], item["average_score"]), reverse=True)

    cash = _float(pool_payload.get("available_cash"), _local_available_cash(state))
    market_value = sum(
        _float(item.get("current_price", item.get("entry_price"))) * _float(item.get("quantity"))
        for item in positions
    )
    unrealized = sum(
        (_float(item.get("current_price", item.get("entry_price"))) - _float(item.get("entry_price")))
        * _float(item.get("quantity"))
        for item in positions
    )
    compact_positions = [
        {
            "symbol": str(item.get("symbol") or ""),
            "name": str(item.get("name") or item.get("symbol") or ""),
            "sector": str(
                item.get("sector_name")
                or item.get("sector")
                or _local_sector_name(str(item.get("symbol") or ""), item.get("name"))
                or "未分类"
            ),
            "quantity": _float(item.get("quantity")),
            "entry_price": _float(item.get("entry_price")),
            "current_price": _float(item.get("current_price", item.get("entry_price"))),
            "change_pct": item.get("change_pct"),
            "market_value": round(_float(item.get("current_price", item.get("entry_price"))) * _float(item.get("quantity")), 2),
            "unrealized_pnl": round(
                (_float(item.get("current_price", item.get("entry_price"))) - _float(item.get("entry_price")))
                * _float(item.get("quantity")),
                2,
            ),
        }
        for item in positions
    ]

    performance = storage.performance_report()
    training = storage.training_dataset_summary()
    dataset = dict(raw_result.get("dataset") or {})
    # Keep dataset content aligned with the dataset source run.  The report
    # intentionally uses a complete ``full`` artifact for backtest/WFO, but
    # its embedded dataset may be stale or narrower than a later optimize or
    # historical-training run.
    if dataset_source_run and dataset_source_run is not latest_run:
        source_result = dict((dataset_source_run or {}).get("result") or {})
        source_dataset = source_result.get("dataset")
        if isinstance(source_dataset, dict):
            dataset = dict(source_dataset)
    # Align the displayed dataset window and row count with the explicit
    # historical-training scope while retaining the validated quality and
    # feature/label metadata from the corresponding optimize artifact.
    if latest_training_run and dataset_scope.get("symbol_count"):
        scope_start = dataset_scope.get("requested_start") or ((latest_training_run or {}).get("result") or {}).get("window", {}).get("start")
        scope_end = dataset_scope.get("requested_end") or ((latest_training_run or {}).get("result") or {}).get("window", {}).get("end")
        dataset = {
            "snapshot": {
                "version": str((dataset.get("snapshot") or {}).get("version") or f"training-scope-{(latest_training_run or {}).get('run_id')}"),
                "start": scope_start,
                "end": scope_end,
                "row_count": dataset_scope.get("row_count"),
                "symbol_count": dataset_scope.get("symbol_count"),
            },
            "data_quality": {
                "status": str((dataset.get("data_quality") or {}).get("status") or "valid"),
                "symbol_count": dataset_scope.get("symbol_count"),
                "row_count": dataset_scope.get("row_count"),
                "total_errors": (dataset.get("data_quality") or {}).get("total_errors", 0),
                "total_warnings": (dataset.get("data_quality") or {}).get("total_warnings", 0),
                "future_leakage_check": (dataset.get("data_quality") or {}).get("future_leakage_check") or {"status": "not_run_in_historical_train"},
                "scope_source": "historical_train.training.dataset_scope",
            },
            "training_scope": dict(dataset_scope),
            "feature_version": dataset.get("feature_version"),
            "label_version": dataset.get("label_version"),
            "feature_names": list(dataset.get("feature_names") or []),
            "label_names": list(dataset.get("label_names") or []),
        }
    else:
        dataset_source_run = latest_dataset_run or latest_run
    quality = dict(dataset.get("data_quality") or {})
    snapshot = dict(dataset.get("snapshot") or {})
    alerts: list[dict[str, str]] = []
    if not ai_status.get("ready"):
        alerts.append({"level": "warning", "title": "LLM 尚未就绪", "message": "当前报告可使用传统指标和本地机器学习信号，但外部 LLM 推理未参与最新决策。"})
    if training.get("status") == "insufficient_data":
        alerts.append({"level": "warning", "title": "机器学习样本仍不足", "message": "方向概率可作为辅助排序，暂不应单独作为发布或满仓依据。"})
    if quality and quality.get("status") not in {"valid", "scope_only"}:
        alerts.append({"level": "danger", "title": "研究数据质量异常", "message": "请先处理数据错误，再解释优化和交叉验证结果。"})
    if best_strategy and _float(backtest_summary.get("total_return_pct")) < 0 < _float(best_strategy.get("optimized", {}).get("total_return_pct")):
        alerts.append({"level": "info", "title": "长短样本结论分化", "message": "完整研究期优化结果为正，但最近两个月基准回测为负；当前环境适配性需要优先观察。"})
    weak_wfo = [
        row["strategy"]
        for row in strategy_rows
        if _float(row.get("optimized", {}).get("total_return_pct")) > 0
        and _float(row.get("walk_forward", {}).get("total_return_pct")) <= 0
    ]
    if weak_wfo:
        alerts.append({"level": "info", "title": "Walk-Forward 稳健性分化", "message": "部分策略的滚动窗口收益未同步改善：" + "、".join(weak_wfo) + "。"})

    return {
        "status": "ready" if latest_run and backtest else "partial",
        "generated_at": _now_iso(),
        "sources": {
            "pool_as_of": pool_payload.get("as_of"),
            "research_run_id": (latest_run or {}).get("run_id"),
            "research_started_at": (latest_run or {}).get("started_at"),
            "research_finished_at": (latest_run or {}).get("finished_at"),
            "dataset_run_id": (dataset_lineage_run or {}).get("run_id"),
            "dataset_source": quality.get("scope_source") or (dataset_lineage_run or {}).get("action"),
            "dataset_version": snapshot.get("version"),
            "dataset_start": snapshot.get("start"),
            "dataset_end": snapshot.get("end"),
            "simulator_version": (active_champion or {}).get("metrics", {}).get("simulator_version"),
            "ensemble_version": MODEL_ENSEMBLE_VERSION,
            "ensemble_component_count": len(_active_ensemble_runtime_policy(storage).get("policies") or {}),
        },
        "verdict": {
            "best_robust_strategy": (best_strategy or {}).get("strategy"),
            "release_gate_passed_count": sum(bool(row["model"].get("release_gate_passed")) for row in strategy_rows),
            "strategy_count": len(strategy_rows),
            "active_champion": {
                "model_name": (active_champion or {}).get("model_name"),
                "model_id": (active_champion or {}).get("model_id"),
                "published_at": (active_champion or {}).get("published_at"),
            },
            "model_ensemble": _active_ensemble_runtime_policy(storage),
            "alerts": alerts,
        },
        "backtest": {
            "status": backtest.get("status"),
            "reason": backtest.get("reason"),
            "errors": dict(backtest.get("errors") or {}),
            "symbols": list(backtest.get("symbols") or []),
            "strategy": backtest.get("strategy"),
            "window": backtest.get("window"),
            "assumptions": backtest.get("assumptions"),
            # The historical artifact may not have recorded component model
            # IDs.  Expose the policy actually used by the current signal and
            # paper-trading runtime so the report cannot imply that a pending
            # Challenger was part of live backtesting.
            "runtime_model_ensemble": _active_ensemble_runtime_policy(storage),
            "summary": backtest_summary,
            "trade_metrics": _backtest_trade_metrics(backtest),
            "equity_curve": equity_curve,
            "trades": backtest_trades,
        },
        "strategies": strategy_rows,
        "selection": {
            "signals": signals,
            "action_counts": action_counts,
            "executable_count": sum(bool(item.get("execution_ready")) for item in signals),
            "average_score": round(statistics.mean([item["swing_score"] for item in signals]), 2) if signals else None,
            "average_up_probability": round(statistics.mean([_float(item["up_probability_1d"]) for item in signals if item.get("up_probability_1d") is not None]), 4)
            if any(item.get("up_probability_1d") is not None for item in signals)
            else None,
            "sector_strength": sector_rows,
        },
        "portfolio": {
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "equity": round(cash + market_value, 2),
            "exposure_pct": round(market_value / (cash + market_value) * 100.0, 2) if cash + market_value > 0 else 0.0,
            "unrealized_pnl": round(unrealized, 2),
            "positions": compact_positions,
            "manual_trade_count": len(manual_trades),
        },
        "models": {
            "ensemble": _active_ensemble_runtime_policy(storage),
            "performance": performance,
            "training": training,
            "candidate_training": dict(
                (
                    ((latest_training_run or {}).get("result") or {}).get("training")
                    if (latest_training_run or {}).get("action") == "historical_train"
                    else (latest_training_run or {}).get("result")
                )
                or {}
            ),
            "historical_replay": {
                key: ((latest_replay_run or {}).get("result") or {}).get(key)
                for key in ("status", "source", "synthetic_prices", "window", "sample_count")
            },
            "llm": {
                key: ai_status.get(key)
                for key in ("enabled", "ready", "provider", "model", "prompt_version", "knowledge_version", "miaoxiang_ready", "missing")
            },
        },
        "dataset": {
            "snapshot": snapshot,
            "quality": {
                key: quality.get(key)
                for key in ("status", "symbol_count", "row_count", "total_errors", "total_warnings", "future_leakage_check", "scope_source")
            },
            "feature_version": dataset.get("feature_version"),
            "label_version": dataset.get("label_version"),
            "feature_count": len(dataset.get("feature_names") or []),
            "label_count": len(dataset.get("label_names") or []),
        },
    }


def _sector_strength_value(signal: dict[str, Any]) -> float:
    context = signal.get("sector_context") or signal.get("market_context") or {}
    raw = context.get("strength") if isinstance(context, dict) else None
    if raw is None:
        raw = signal.get("sector_strength")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").lower()
    return 3.0 if any(token in text for token in ("强", "strong", "hot", "上升")) else (1.0 if any(token in text for token in ("弱", "weak", "冷", "下降")) else 2.0)


MAX_LOCAL_POSITIONS = 4
# Training/research must not silently use a lower-priority source.  The live
# UI may still use the full resilience chain, but reproducible model artifacts
# accept only the user's requested 妙想→腾讯 order.
RESEARCH_ALLOWED_PROVIDERS = ("miaoxiang", "tencent")


def _active_champion_runtime_policy(storage: Any | None = None) -> dict[str, Any] | None:
    """Return only the published Champion fields required by live paper trading."""
    storage = storage or _get_ai_service().storage
    champion = storage.active_champion(include_snapshot=False)
    if not champion or champion.get("role") != "champion":
        return None
    if champion.get("status") != "active_paper" or not champion.get("published_at"):
        return None
    metrics = dict(champion.get("metrics") or {})
    best = dict(metrics.get("best") or {})
    return {
        "model_id": champion.get("model_id"),
        "model_name": champion.get("model_name"),
        "version": champion.get("version"),
        "published_at": champion.get("published_at"),
        "simulator_version": metrics.get("simulator_version"),
        "params": dict(best.get("params") or {}),
    }


def _model_runtime_policy(model: dict[str, Any]) -> dict[str, Any]:
    """Extract the immutable runtime fields shared by champion/candidates."""
    metrics = dict(model.get("metrics") or {})
    best = dict(metrics.get("best") or {})
    return {
        "model_id": model.get("model_id"),
        "model_name": model.get("model_name"),
        "version": model.get("version"),
        "published_at": model.get("published_at"),
        "simulator_version": metrics.get("simulator_version"),
        "params": dict(best.get("params") or {}),
        "status": model.get("status"),
    }


def _active_ensemble_runtime_policy(storage: Any | None = None) -> dict[str, Any]:
    """Build the three-model policy used by the short-swing signal center.

    Registry rows are newest-first.  A strategy may have only historical
    published rows (for example after another Champion superseded it), so we
    keep the newest usable row per strategy and expose its immutable params.
    The live decision still requires the trend model plus at least one
    confirmation model, with a weighted score for ranking.
    """
    storage = storage or _get_ai_service().storage
    by_strategy: dict[str, dict[str, Any]] = {}
    try:
        rows = storage.list_models(include_snapshots=False)
    except Exception:  # noqa: BLE001 - signal center remains usable without registry
        rows = []
    # An evaluated/release-requested Challenger is research-only.  It must
    # never feed live signals or paper trading before an auditable publish
    # event exists.  Published historical components remain eligible for the
    # three-model ensemble even when their individual registry role is now
    # archived/superseded.
    usable_statuses = {"active_paper", "superseded", "rolled_back"}
    eligible: list[dict[str, Any]] = []
    for row in rows:
        strategy = str(row.get("model_name") or "")
        if strategy not in MODEL_ENSEMBLE_WEIGHTS:
            continue
        if row.get("status") not in usable_statuses or not row.get("published_at"):
            continue
        policy = _model_runtime_policy(row)
        # A registry row without optimized params cannot provide a distinct
        # model signal; skip it and let the next historical row fill the slot.
        if policy["params"]:
            eligible.append(policy)
    # Select one complete published cohort.  During staged publication of the
    # three components, this keeps the live ensemble on the previous complete
    # cohort instead of mixing old and new strategy versions.
    cohorts: dict[str, dict[str, dict[str, Any]]] = {}
    for policy in eligible:
        cohorts.setdefault(str(policy.get("version") or ""), {})[
            str(policy.get("model_name") or "")
        ] = policy
    complete = [
        (version, cohort)
        for version, cohort in cohorts.items()
        if all(name in cohort for name in MODEL_ENSEMBLE_WEIGHTS)
    ]
    if complete:
        _, selected = max(
            complete,
            key=lambda item: max(
                str(item[1][name].get("published_at") or "")
                for name in MODEL_ENSEMBLE_WEIGHTS
            ),
        )
        by_strategy = selected
    return {
        "mode": "weighted_vote",
        "model_name": "short_swing_ensemble",
        "version": MODEL_ENSEMBLE_VERSION,
        "ensemble_version": MODEL_ENSEMBLE_VERSION,
        "style": "short_term_trend_swing_ultra_short_allowed",
        "hard_gate_strategy": "trend_swing",
        "primary_confirmation_strategy": "breakout_pullback",
        "secondary_confirmation_strategy": "momentum_regime",
        "weights": dict(MODEL_ENSEMBLE_WEIGHTS),
        "entry_threshold": MODEL_ENSEMBLE_ENTRY_THRESHOLD,
        "trend_required": True,
        "confirmation_required": 1,
        "policies": by_strategy,
    }


def _active_model_signals(
    signals: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    state: dict[str, Any],
    model: dict[str, Any],
) -> list[dict[str, Any]]:
    """Gate rule signals with the currently published strategy parameters.

    This is inference only: it reads the immutable published parameters and
    today's OHLCV snapshot. It never trains, optimizes, publishes, calls an
    LLM, or invokes an Agent.
    """
    params = dict(model.get("params") or {})
    strategy = str(model.get("model_name") or "trend_swing")
    fast_window = max(2, int(params.get("fast_window") or 5))
    slow_window = max(fast_window + 1, int(params.get("slow_window") or 20))
    momentum_window = max(1, int(params.get("momentum_window") or 20))
    volatility_cap = _float(params.get("volatility_cap"), 0.045)
    momentum_vol_weight = max(0.0, _float(params.get("momentum_vol_weight"), 1.0))
    momentum_exit_threshold = _float(params.get("momentum_exit_threshold"), -0.01)
    max_entry_momentum = _float(params.get("max_entry_momentum"), 0.35)
    trailing_atr_multiple = max(0.0, _float(params.get("trailing_atr_multiple"), 2.5))
    max_holding_bars = max(1, int(params.get("max_holding_bars") or 20))
    breakout_tolerance = max(0.0, _float(params.get("breakout_tolerance"), 0.02))
    pullback_tolerance = max(0.0, _float(params.get("pullback_tolerance"), 0.03))
    min_volume_ratio = _float(params.get("min_volume_ratio"), 0.90)
    breakout_entry_mode = str(
        params.get("breakout_entry_mode") or "strict_breakout_or_pullback"
    )
    simulator_version = str(model.get("simulator_version") or "")
    distinct_v7_rules = simulator_version.endswith("_v7") or any(
        key in params
        for key in (
            "regime_window",
            "regime_momentum_threshold",
            "min_market_breadth",
        )
    )
    regime_window = max(1, int(params.get("regime_window") or 5))
    regime_momentum_threshold = _float(
        params.get("regime_momentum_threshold"), 0.015
    )
    regime_acceleration_threshold = _float(
        params.get("regime_acceleration_threshold"), 0.0
    )
    regime_exit_threshold = _float(params.get("regime_exit_threshold"), -0.01)
    min_market_breadth = _clip(
        _float(params.get("min_market_breadth"), 0.50), 0.0, 1.0
    )
    quote_by_symbol = {str(item.get("symbol") or ""): item for item in quotes}
    position_by_symbol = {
        str(item.get("symbol") or ""): item for item in state.get("positions") or []
    }
    market_eligible = 0
    market_positive = 0
    if distinct_v7_rules and strategy == "momentum_regime":
        for quote in quotes:
            market_candles = [
                row
                for row in list((quote.get("_ai_series") or {}).get("candles") or [])
                if _float(row.get("close")) > 0
            ]
            if len(market_candles) < max(slow_window, momentum_window) + 1:
                continue
            market_closes = [_float(row.get("close")) for row in market_candles]
            market_close = market_closes[-1]
            market_slow = _mean(market_closes[-slow_window:])
            market_momentum = (
                market_close / market_closes[-momentum_window - 1] - 1.0
            )
            market_eligible += 1
            market_positive += int(
                market_close > market_slow and market_momentum > 0
            )
    market_breadth = (
        market_positive / market_eligible if market_eligible else 0.0
    )

    for signal in signals:
        symbol = str(signal.get("symbol") or "")
        quote = quote_by_symbol.get(symbol) or {}
        candles = [
            row
            for row in list((quote.get("_ai_series") or {}).get("candles") or [])
            if _float(row.get("close")) > 0
        ]
        decision = {
            "model_id": model.get("model_id"),
            "model_name": strategy,
            "version": model.get("version"),
            "params": params,
            "entry_allowed": False,
            "exit_required": False,
            "reason": "模型运行所需行情不足",
        }
        signal["active_model"] = decision
        if len(candles) < max(slow_window, momentum_window, regime_window, 20) + 1:
            if (signal.get("execution_signal") or {}).get("action") == "buy":
                signal["execution_signal"] = None
            continue

        closes = [_float(row.get("close")) for row in candles]
        close = closes[-1]
        ma_fast = _mean(closes[-fast_window:])
        ma_slow = _mean(closes[-slow_window:])
        momentum = close / closes[-momentum_window - 1] - 1.0
        momentum20 = close / closes[-21] - 1.0
        regime_momentum = close / closes[-regime_window - 1] - 1.0
        normalized_long_momentum = momentum * regime_window / momentum_window
        momentum_acceleration = regime_momentum - normalized_long_momentum
        recent_returns = [
            closes[index] / closes[index - 1] - 1.0
            for index in range(max(1, len(closes) - 20), len(closes))
            if closes[index - 1] > 0
        ]
        volatility = (
            statistics.stdev(recent_returns) if len(recent_returns) > 1 else 0.0
        )
        mfi_passed = bool((signal.get("mfi_filter") or {}).get("passed", True))
        risk_scale = max(volatility, 0.005)
        ranking_score = momentum / (risk_scale**momentum_vol_weight)
        entry_allowed = close > ma_fast > ma_slow and momentum > 0 and mfi_passed
        if strategy == "breakout_pullback":
            recent_high = max(_float(row.get("high")) for row in candles[-20:])
            volume_ratio = _float(signal.get("volume_ratio"), 1.0)
            breakout_setup = (
                close >= recent_high * (1.0 - breakout_tolerance)
                and volume_ratio >= min_volume_ratio
            )
            pullback_setup = (
                ma_fast > ma_slow
                and close >= ma_fast
                and close <= ma_fast * (1.0 + pullback_tolerance)
            )
            if distinct_v7_rules and breakout_entry_mode == "breakout_only":
                pattern_setup = breakout_setup
            elif distinct_v7_rules and breakout_entry_mode == "pullback_only":
                pattern_setup = pullback_setup
            elif distinct_v7_rules:
                pattern_setup = breakout_setup or pullback_setup
            elif breakout_entry_mode == "trend_with_breakout_overlay":
                entry_allowed = close > ma_fast > ma_slow
            else:
                pattern_setup = breakout_setup or pullback_setup
            if distinct_v7_rules or breakout_entry_mode != "trend_with_breakout_overlay":
                entry_allowed = close > ma_slow and pattern_setup
            entry_allowed = entry_allowed and momentum > 0 and mfi_passed
        elif strategy == "momentum_regime" and distinct_v7_rules:
            entry_allowed = (
                close > ma_fast > ma_slow
                and momentum > 0
                and regime_momentum >= regime_momentum_threshold
                and momentum_acceleration >= regime_acceleration_threshold
                and market_breadth >= min_market_breadth
                and mfi_passed
            )
            ranking_score += max(0.0, momentum_acceleration) / risk_scale
        entry_allowed = (
            entry_allowed
            and volatility <= volatility_cap
            and momentum20 <= max_entry_momentum
        )

        position = position_by_symbol.get(symbol)
        exit_required = False
        exit_reasons: list[str] = []
        if position is not None:
            market_date = str(signal.get("updated_at") or "")[:10]
            peak_price = max(_float(position.get("model_peak_price"), close), close)
            position["model_peak_price"] = round(peak_price, 4)
            if market_date and position.get("model_last_evaluated_date") != market_date:
                position["model_holding_bars"] = int(
                    _float(position.get("model_holding_bars"))
                ) + 1
                position["model_last_evaluated_date"] = market_date
            holding_bars = int(_float(position.get("model_holding_bars")))
            atr14 = _float(signal.get("atr14"), close * 0.02)
            trailing_stop = peak_price - trailing_atr_multiple * atr14
            if strategy == "momentum_regime" and distinct_v7_rules:
                if close < ma_fast:
                    exit_reasons.append(f"收盘价跌破 MA{fast_window}")
                if regime_momentum < regime_exit_threshold:
                    exit_reasons.append("短周期动量状态转弱")
                if market_breadth < min_market_breadth * 0.80:
                    exit_reasons.append("市场动量宽度低于退出阈值")
            else:
                if close < ma_slow:
                    exit_reasons.append(f"收盘价跌破 MA{slow_window}")
                if momentum < momentum_exit_threshold:
                    exit_reasons.append("动量跌破发布参数阈值")
            if close < trailing_stop:
                exit_reasons.append("跌破 ATR 移动止损")
            if holding_bars >= max_holding_bars:
                exit_reasons.append("达到最大持仓周期")
            if volatility > volatility_cap * 1.35:
                exit_reasons.append("波动率超过退出上限")
            if strategy == "breakout_pullback":
                prior_low = min(_float(row.get("low")) for row in candles[-21:-1])
                if close < prior_low:
                    exit_reasons.append("跌破前 20 日低点")
            exit_required = bool(exit_reasons)

        decision.update(
            {
                "entry_allowed": entry_allowed,
                "exit_required": exit_required,
                "ranking_score": round(ranking_score, 6),
                "ma_fast": round(ma_fast, 4),
                "ma_slow": round(ma_slow, 4),
                "momentum": round(momentum, 6),
                "regime_momentum": round(regime_momentum, 6),
                "momentum_acceleration": round(momentum_acceleration, 6),
                "market_breadth": round(market_breadth, 6),
                "volatility20": round(volatility, 6),
                "reason": "；".join(exit_reasons)
                if exit_reasons
                else ("发布模型入场条件通过" if entry_allowed else "发布模型入场条件未通过"),
            }
        )
        execution = signal.get("execution_signal") or {}
        if execution.get("action") == "buy" and not entry_allowed:
            signal["execution_signal"] = None
            if signal.get("action") in {"买入", "加仓"}:
                signal["action"] = "等待买入"
                signal["trigger"] = "选股规则已达标，但当前发布模型未通过入场门控"
        if position is not None and exit_required:
            quantity = math.floor(_float(position.get("quantity")) / 100) * 100
            same_day_entry = str(position.get("model_entry_date") or "") == str(
                signal.get("updated_at") or ""
            )[:10]
            if quantity > 0 and not same_day_entry:
                signal["action"] = "模型卖出"
                signal["trigger"] = decision["reason"]
                signal["execution_signal"] = {
                    "signal_id": (
                        f"champion-{model.get('version')}-{symbol}-"
                        f"{str(signal.get('updated_at') or 'latest')[:10]}-sell"
                    ),
                    "symbol": symbol,
                    "action": "sell",
                    "quantity": float(quantity),
                    "price": round(close, 3),
                    "strategy_id": str(model.get("model_id") or strategy),
                }
    return signals


def _active_ensemble_signals(
    signals: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    state: dict[str, Any],
    ensemble: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fuse the three strategy gates into one short-swing execution signal.

    The trend model is a hard style gate.  Breakout/pullback and momentum
    regime are timing confirmations; at least one must agree.  A weighted
    vote is retained for ranking and auditability, while exits are fail-safe:
    any published model's risk exit is sufficient to request a sell.
    """
    policies = dict(ensemble.get("policies") or {})
    weights = dict(MODEL_ENSEMBLE_WEIGHTS)
    threshold = _float(ensemble.get("entry_threshold"), MODEL_ENSEMBLE_ENTRY_THRESHOLD)
    output: list[dict[str, Any]] = []
    for original in signals:
        decisions: dict[str, dict[str, Any]] = {}
        for strategy in MODEL_ENSEMBLE_WEIGHTS:
            policy = policies.get(strategy)
            if not policy:
                continue
            # Each model owns its own holding-bar bookkeeping; do not let
            # three inference passes increment the real ledger three times.
            local_signal = copy.deepcopy(original)
            local_quotes = copy.deepcopy(quotes)
            local_state = copy.deepcopy(state)
            result = _active_model_signals(
                [local_signal], local_quotes, local_state, policy
            )
            decisions[strategy] = dict(
                (result[0].get("active_model") if result else {}) or {}
            )

        trend = decisions.get("trend_swing") or {}
        confirmations = [
            name for name in ("breakout_pullback", "momentum_regime")
            if bool((decisions.get(name) or {}).get("entry_allowed"))
        ]
        available_weights = sum(weights[name] for name in decisions)
        vote_score = (
            sum(
                weights[name]
                for name, decision in decisions.items()
                if bool(decision.get("entry_allowed"))
            )
            / available_weights
            if available_weights
            else 0.0
        )
        trend_ok = bool(trend.get("entry_allowed"))
        entry_allowed = trend_ok and len(confirmations) >= 1 and vote_score >= threshold
        exit_models = [
            name for name, decision in decisions.items()
            if bool(decision.get("exit_required"))
        ]
        score_values = [
            weights[name] * _float(decision.get("ranking_score"))
            for name, decision in decisions.items()
            if decision.get("ranking_score") is not None
        ]
        fused_score = sum(score_values) / available_weights if available_weights else 0.0
        decision = {
            "mode": "weighted_vote",
            "ensemble_version": ensemble.get("ensemble_version", MODEL_ENSEMBLE_VERSION),
            "weights": weights,
            "entry_threshold": threshold,
            "trend_required": True,
            "confirmation_required": 1,
            "entry_allowed": entry_allowed,
            "exit_required": bool(exit_models),
            "trend_vote": trend_ok,
            "confirmation_votes": confirmations,
            "vote_score": round(vote_score, 6),
            "ranking_score": round(fused_score, 6),
            "exit_models": exit_models,
            "models": decisions,
            "reason": (
                "趋势模型通过且确认模型达到加权门槛"
                if entry_allowed
                else ("；".join(f"{name}触发退出" for name in exit_models)
                      if exit_models else "趋势或确认模型未形成共振")
            ),
        }
        merged = original
        merged["active_model"] = decision
        execution = merged.get("execution_signal") or {}
        if execution.get("action") == "buy" and not entry_allowed:
            merged["execution_signal"] = None
            if merged.get("action") in {"买入", "加仓"}:
                merged["action"] = "等待买入"
                merged["trigger"] = "多模型投票未达到短线趋势波段入场门槛"
        position = next(
            (item for item in state.get("positions") or []
             if str(item.get("symbol") or "") == str(merged.get("symbol") or "")),
            None,
        )
        if position is not None and exit_models:
            quantity = math.floor(_float(position.get("quantity")) / 100) * 100
            market_date = str(merged.get("updated_at") or "latest")[:10]
            same_day_entry = str(position.get("model_entry_date") or "") == market_date
            if quantity > 0 and not same_day_entry:
                price = _float(merged.get("current_price"))
                merged["action"] = "模型卖出"
                merged["trigger"] = decision["reason"]
                merged["execution_signal"] = {
                    "signal_id": f"ensemble-{ensemble.get('ensemble_version', MODEL_ENSEMBLE_VERSION)}-{merged.get('symbol')}-{market_date}-sell",
                    "symbol": merged.get("symbol"),
                    "action": "sell",
                    "quantity": float(quantity),
                    "price": round(price, 3),
                    "strategy_id": MODEL_ENSEMBLE_VERSION,
                }
        output.append(merged)
    return output


def _signal_priority(signal: dict[str, Any]) -> tuple[float, float, float, float]:
    """Return the portfolio execution priority used by both sides.

    Buy signals consume scarce slots from strongest to weakest. Sell signals
    release the weakest holdings first, using the exact inverse order.
    """
    probabilities = signal.get("probabilities") or {}
    five_day_probability = (
        probabilities.get("next_5_trading_days") or {}
    ).get("value")
    return (
        _float(signal.get("swing_score"), _float(signal.get("selection_score"))),
        _float(five_day_probability, -1.0),
        _float(signal.get("up_probability"), -1.0),
        _sector_strength_value(signal),
    )


def _execute_local_signals(
    state: dict[str, Any],
    *,
    refresh: bool = True,
    automated: bool = False,
    rendered: list[dict[str, Any]] | None = None,
    active_model: dict[str, Any] | None = None,
    required_market_date: str | None = None,
) -> dict[str, Any]:
    """Apply actionable signals to the local simulated ledger only.

    Scheduled automation never asks for interactive confirmation.  It may
    execute only signals without an explicit model/rule conflict and without
    a human-review flag.  The manual endpoint keeps its existing explicit
    confirmation flow and can therefore remain available for operator review.
    """
    rendered = rendered or _research_pool_items(state, refresh=refresh)
    signals = _pool_signals(state, rendered)
    if automated and active_model:
        if active_model.get("mode") == "weighted_vote":
            signals = _active_ensemble_signals(signals, rendered, state, active_model)
        else:
            signals = _active_model_signals(signals, rendered, state, active_model)
    # Exit first; when cash is limited, buy/add orders are ranked by the
    # Short-swing priority: composite score, 5-day probability, next-day
    # probability, then sector strength.
    exits = [signal for signal in signals if (signal.get("execution_signal") or {}).get("action") == "sell"]
    entries = [signal for signal in signals if (signal.get("execution_signal") or {}).get("action") == "buy"]
    others = [signal for signal in signals if signal not in exits and signal not in entries]
    exits.sort(key=_signal_priority)
    entries.sort(key=_signal_priority, reverse=True)
    signals = exits + entries + others
    existing_ids = {
        str(item.get("source_signal_id") or "")
        for item in state.get("manual_trades") or []
    }
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    positions = state.setdefault("positions", [])
    trades = state.setdefault("manual_trades", [])
    cash = _local_available_cash(state)
    cash_before = cash
    for signal in signals:
        execution = signal.get("execution_signal") or {}
        signal_id = str(execution.get("signal_id") or "")
        if not signal_id or signal_id in existing_ids:
            skipped.append({"symbol": signal.get("symbol"), "reason_code": "not_actionable", "reason": "信号已执行或无可执行信号"})
            continue
        symbol = str(execution.get("symbol") or signal.get("symbol") or "")
        if automated:
            signal_market_date = str(signal.get("updated_at") or "")[:10]
            if required_market_date and signal_market_date != required_market_date:
                skipped.append(
                    {
                        "symbol": symbol,
                        "name": signal.get("name", symbol),
                        "reason_code": "stale_market_data",
                        "reason": (
                            f"自动交易已跳过：行情日期 {signal_market_date or '未知'} "
                            f"不是 {required_market_date}"
                        ),
                    }
                )
                continue
            fusion = signal.get("fusion") or {}
            conflict_level = str(fusion.get("conflict_level") or "none").lower()
            human_review_required = bool(
                fusion.get("human_review_required")
                or (fusion.get("risk") or {}).get("human_review_required")
            )
            if conflict_level != "none":
                skipped.append(
                    {
                        "symbol": symbol,
                        "name": signal.get("name", symbol),
                        "reason_code": "signal_conflict",
                        "reason": f"自动交易已跳过：模型与规则存在 {conflict_level} 冲突",
                        "conflict_level": conflict_level,
                    }
                )
                continue
            if human_review_required:
                skipped.append(
                    {
                        "symbol": symbol,
                        "name": signal.get("name", symbol),
                        "reason_code": "human_review_required",
                        "reason": "自动交易已跳过：该信号需要人工复核",
                    }
                )
                continue
        action = str(execution.get("action") or "").lower()
        price = _float(execution.get("price"))
        quantity = _float(execution.get("quantity"))
        if action not in {"buy", "sell"} or price <= 0 or quantity <= 0:
            skipped.append({"symbol": symbol, "reason_code": "invalid_signal", "reason": "信号价格或数量无效"})
            continue
        position = next((item for item in positions if str(item.get("symbol")) == symbol), None)
        if action == "sell" and (position is None or _float(position.get("quantity")) < quantity):
            skipped.append({"symbol": symbol, "reason_code": "position_insufficient", "reason": "卖出数量超过本地持仓"})
            continue
        if action == "buy":
            if position is None and len(positions) >= MAX_LOCAL_POSITIONS:
                skipped.append(
                    {
                        "symbol": symbol,
                        "name": signal.get("name", symbol),
                        "reason_code": "position_limit",
                        "reason": f"组合持仓已达 {MAX_LOCAL_POSITIONS} 只上限，按排序跳过后续买入",
                        "position_count": len(positions),
                        "max_positions": MAX_LOCAL_POSITIONS,
                    }
                )
                continue
            requested_quantity = quantity
            affordable_quantity = math.floor(
                max(0.0, cash) / max(price * 1.0003, 1e-9) / 100
            ) * 100
            quantity = min(quantity, float(affordable_quantity))
            if quantity <= 0:
                skipped.append(
                    {
                        "symbol": symbol,
                        "reason_code": "insufficient_cash",
                        "reason": f"本地可用现金 {cash:.2f} 元，不足买入一手（约 {price * 100:.2f} 元）",
                        "requested_quantity": requested_quantity,
                        "available_cash": round(cash, 2),
                        "required_cash": round(price * 100 * 1.0003, 2),
                    }
                )
                continue
        realized_pnl = 0.0
        if action == "buy":
            if position is None:
                position = {"symbol": symbol, "name": signal.get("name", symbol), "quantity": quantity, "entry_price": price}
                positions.append(position)
            else:
                old_quantity = _float(position.get("quantity"))
                old_cost = _float(position.get("entry_price"))
                position["quantity"] = old_quantity + quantity
                position["entry_price"] = ((old_quantity * old_cost) + quantity * price) / position["quantity"]
        else:
            realized_pnl = (price - _float(position.get("entry_price"))) * quantity
            position["quantity"] = _float(position.get("quantity")) - quantity
            if position["quantity"] <= 1e-12:
                positions.remove(position)
        trade = {
            "id": f"auto-{len(trades) + 1}",
            "source_signal_id": signal_id,
            "symbol": symbol,
            "name": signal.get("name", symbol),
            "action": action,
            "price": price,
            "quantity": quantity,
            "net_pnl": realized_pnl,
            "time": _now_iso(),
            "source": "scheduled_local_model_signal"
            if automated
            else "local_signal",
            "model_id": (active_model or {}).get("model_id") if automated else None,
            "model_version": (active_model or {}).get("version") if automated else None,
        }
        if action == "buy" and automated and position is not None:
            position["model_entry_date"] = str(signal.get("updated_at") or "")[:10]
            position["model_last_evaluated_date"] = position["model_entry_date"]
            position["model_holding_bars"] = 0
            position["model_peak_price"] = price
        trades.append(trade)
        cash += -price * quantity if action == "buy" else price * quantity
        existing_ids.add(signal_id)
        applied.append(trade)
    if applied:
        state["available_cash"] = round(cash, 2)
    return {
        "status": "completed",
        "applied": applied,
        "skipped": skipped,
        "signal_count": len(signals),
        "available_cash_before": round(cash_before, 2),
        "available_cash_after": round(cash, 2),
        "automated": automated,
        "position_count": len(positions),
        "max_positions": MAX_LOCAL_POSITIONS,
        "active_model": active_model,
        "research_performed": False,
        "model_changed": False,
        "llm_agent_called": False,
    }


def _run_daily_trade_action(
    state: dict[str, Any], *, refresh: bool = True
) -> dict[str, Any]:
    """Force-refresh today's pool, then execute paper trades without research."""
    # ``refresh`` is retained for compatibility with older callers, but daily
    # automation must never be allowed to opt into a cached pool snapshot.
    refresh_requested = bool(refresh)
    refresh_started_at = _now_iso()
    rendered = _research_pool_items(state, refresh=True)
    refresh_completed_at = _now_iso()
    local_today = datetime.now().date().isoformat()

    expected_symbols = list(
        dict.fromkeys(
            symbol
            for item in list(state.get("positions") or [])
            + list(state.get("watchlist") or [])
            if (symbol := _code(str(item.get("symbol") or "")))
        )
    )
    rendered_by_symbol = {
        _code(str(item.get("symbol") or "")): item
        for item in rendered
        if _code(str(item.get("symbol") or ""))
    }
    refresh_errors: dict[str, str] = {}
    refreshed_dates: dict[str, str] = {}
    for symbol in expected_symbols:
        item = rendered_by_symbol.get(symbol)
        if item is None:
            refresh_errors[symbol] = "刷新结果缺少该标的"
            continue
        if item.get("quote_error"):
            refresh_errors[symbol] = str(item.get("quote_error"))
            continue
        market_date = str(
            (item.get("analysis") or {}).get("as_of")
            or item.get("updated_at")
            or ""
        )[:10]
        if market_date:
            refreshed_dates[symbol] = market_date
        else:
            refresh_errors[symbol] = "刷新结果缺少行情日期"

    market_dates = sorted(
        {
            str((item.get("analysis") or {}).get("as_of") or item.get("updated_at") or "")[:10]
            for item in rendered
            if str((item.get("analysis") or {}).get("as_of") or item.get("updated_at") or "")[:10]
        }
    )
    snapshot_end = market_dates[-1] if market_dates else ""
    refresh_audit = {
        "pool_refresh_performed": True,
        "pool_refresh_forced": True,
        "pool_refresh_requested": refresh_requested,
        "pool_refresh_status": "completed",
        "pool_refresh_symbol_count": len(expected_symbols),
        "pool_refresh_success_count": len(expected_symbols) - len(refresh_errors),
        "pool_refresh_errors": refresh_errors,
        "pool_refresh_dates": refreshed_dates,
        "pool_refresh_started_at": refresh_started_at,
        "pool_refresh_completed_at": refresh_completed_at,
        "pool_as_of": snapshot_end,
    }
    common_result = {
        **refresh_audit,
        "snapshot_end": snapshot_end,
        "local_today": local_today,
        "mode": "local_paper_trade_only",
        "research_performed": False,
        "model_changed": False,
        "llm_agent_called": False,
        "applied": [],
        "skipped": [],
    }
    if refresh_errors:
        return {
            **common_result,
            "status": "failed",
            "reason": "票池行情刷新未完整，为避免使用旧数据已取消本次自动交易",
            "pool_refresh_status": "failed",
        }
    stale_symbols = {
        symbol: market_date
        for symbol, market_date in refreshed_dates.items()
        if market_date != local_today
    }
    if snapshot_end != local_today or stale_symbols:
        return {
            **common_result,
            "status": "skipped",
            "reason": "最新行情日期不是今天，疑似周末/节假日或行情尚未更新",
            "pool_refresh_status": "stale",
            "pool_refresh_stale_symbols": stale_symbols,
        }
    # Keep the Champion lookup as an auditable compatibility hook for callers
    # and older integrations; the actual runtime decision uses the published
    # three-model ensemble below.
    _active_champion_runtime_policy()
    model = _active_ensemble_runtime_policy()
    if model is None:
        return {
            **common_result,
            "status": "skipped",
            "reason": "没有已发布且处于 active_paper 状态的 Champion，未执行自动交易",
        }
    execution = _execute_local_signals(
        state,
        refresh=False,
        automated=True,
        rendered=rendered,
        active_model=model,
        required_market_date=local_today,
    )
    return {
        **common_result,
        **execution,
    }


def _scheduled_trade_already_recorded(storage: Any, schedule_date: str) -> bool:
    return any(
        item.get("action") == "daily_trade"
        and str((item.get("params") or {}).get("schedule_date") or "")
        == schedule_date
        for item in storage.list_automation_runs(limit=100)
    )


def _execute_scheduled_daily_trade(root: Path, schedule_date: str) -> dict[str, Any]:
    """Execute one server-native 19:00 run without Codex/Agent scheduling."""
    storage = _get_ai_service().storage
    with _DAILY_TRADE_SCHEDULER_LOCK:
        if _scheduled_trade_already_recorded(storage, schedule_date):
            return {"status": "deduplicated", "schedule_date": schedule_date}
        run_id = storage.create_automation_run(
            "daily_trade",
            {
                "refresh": True,
                "refresh_forced": True,
                "schedule_date": schedule_date,
                "trigger": "review_center_server_native_scheduler",
                "mode": "local_paper_trade_only",
                "account_source": "local_review_center_state",
                "research_performed": False,
                "llm_agent_called": False,
            },
        )
    state_path = root / STATE_FILE
    try:
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {
                "positions": [],
                "watchlist": [],
                "manual_trades": [],
                "initialized": True,
            }
        )
        result = _run_daily_trade_action(state, refresh=True)
        if result.get("status") == "failed":
            storage.finish_automation_run(
                run_id,
                status="failed",
                result=result,
                error=str(result.get("reason") or "票池刷新失败"),
            )
            return {"status": "failed", "run_id": run_id, "result": result}
        if result.get("status") == "completed":
            state["initialized"] = True
            state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (root / POOL_CACHE_FILE).unlink(missing_ok=True)
            (root / BACKTEST_CACHE_FILE).unlink(missing_ok=True)
        storage.finish_automation_run(run_id, status="completed", result=result)
        return {"status": "completed", "run_id": run_id, "result": result}
    except Exception as exc:  # noqa: BLE001 - scheduler must leave an audit row
        storage.finish_automation_run(
            run_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"status": "failed", "run_id": run_id, "error": str(exc)}


def _daily_trade_scheduler_loop(root: Path) -> None:
    """Run once each weekday during the local 19:00 hour."""
    while True:
        now = datetime.now()
        if now.weekday() < 5 and now.hour == 19:
            _execute_scheduled_daily_trade(root, now.date().isoformat())
        time.sleep(30)


class ReviewCenterHandler(SimpleHTTPRequestHandler):
    """Static file handler with local pool management endpoints."""

    server_version = "AKQuantReviewCenter/0.1"

    def end_headers(self) -> None:  # noqa: N802
        # The review center and its report are stateful local views.  Prevent a
        # browser from reopening an older static snapshot after a trade or pool
        # edit.
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    @property
    def root(self) -> Path:
        return Path(self.directory or ".").resolve()

    @property
    def state_path(self) -> Path:
        return self.root / STATE_FILE

    @property
    def pool_cache_path(self) -> Path:
        return self.root / POOL_CACHE_FILE

    @property
    def backtest_cache_path(self) -> Path:
        return self.root / BACKTEST_CACHE_FILE

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "watchlist": [],
                "positions": [],
                "manual_trades": [],
                "initialized": False,
            }
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        return {
            "watchlist": list(state.get("watchlist") or []),
            "positions": list(state.get("positions") or []),
            "manual_trades": list(state.get("manual_trades") or []),
            "available_cash": state.get("available_cash"),
            "initialized": bool(state.get("initialized", False)),
        }

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.pool_cache_path.unlink(missing_ok=True)
        self.backtest_cache_path.unlink(missing_ok=True)

    def _read_json_cache(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def _write_json_cache(self, path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _serve_signal_center(self) -> None:
        path = self.root / "signal_center.html"
        html = path.read_text(encoding="utf-8")
        html = html.replace(
            "</body>",
            '<script src="/signal_center_enhancements.js"></script></body>',
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _pool_item(self, item: dict[str, Any], refresh: bool = False) -> dict[str, Any]:
        try:
            quote = stock_kline(str(item.get("symbol", "")), refresh=refresh)
            merged = dict(item)
            series = dict(quote.get("series") or {})
            for key, value in quote.items():
                if key == "series":
                    continue
                if key == "name" and value == quote.get("symbol") and item.get("name"):
                    continue
                # Quote providers may omit industry metadata.  Never erase a
                # previously cached classification with an empty response.
                if key in {"sector_name", "region_name", "concept_names"} and not value:
                    continue
                merged[key] = value
            if not merged.get("sector_name"):
                local_sector = _local_sector_name(
                    str(merged.get("symbol") or ""), merged.get("name")
                )
                if local_sector:
                    merged["sector_name"] = local_sector
                    merged["sector_context"] = {
                        "status": "fallback",
                        "name": local_sector,
                        "strength": "未知",
                        "source": "local_symbol_sector_map",
                    }
            if not merged.get("sector_name"):
                sector_context = _fetch_miaoxiang_sector(
                    str(merged.get("symbol") or ""), merged.get("sector_name")
                )
                if sector_context.get("name"):
                    merged["sector_name"] = sector_context["name"]
                    merged["sector_context"] = sector_context
            if not item.get("self_price"):
                merged["self_price"] = quote.get("current_price", 0.0)
            merged["_ai_series"] = series
            merged["analysis"] = _analyze_series(series)
            return merged
        except Exception as exc:  # noqa: BLE001 - one stale symbol must not break all pools
            return {**item, "quote_error": str(exc)}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in {"/signal_center.html", "/signal_center"}:
                self._serve_signal_center()
                return
            if parsed.path == "/api/pools":
                state = self._read_state()
                refresh = query.get("refresh", [""])[0] in {"1", "true"}
                if not refresh:
                    cached_payload = self._read_json_cache(self.pool_cache_path)
                    if cached_payload is not None and cached_payload.get("_cache_version") == POOL_CACHE_VERSION:
                        cached_payload["cache"] = {"status": "hit", "source": POOL_CACHE_FILE}
                        self._json(cached_payload)
                        return
                # Quote/K-line requests are independent.  Refreshing them in
                # parallel keeps the review page responsive as the pools grow.
                sources = state["watchlist"] + state["positions"]
                with ThreadPoolExecutor(
                    # 妙想 MCP is the first-priority source and has a
                    # per-request handshake cost; bounded parallelism keeps
                    # first-load latency reasonable without unbounded bursts.
                    max_workers=max(1, min(16, len(sources)))
                ) as executor:
                    rendered = list(
                        executor.map(
                            lambda item: self._pool_item(item, refresh=refresh),
                            sources,
                        )
                    )
                watchlist = rendered[: len(state["watchlist"])]
                positions = rendered[len(state["watchlist"]) :]
                for item in rendered:
                    if not item.get("quote_error"):
                        _update_outcome_labels(item)
                migrated = False
                for source, rendered in zip(state["watchlist"], watchlist):
                    if (
                        rendered.get("name")
                        and rendered["name"] != rendered.get("symbol")
                        and source.get("name") != rendered["name"]
                    ):
                        source["name"] = rendered["name"]
                        migrated = True
                    if rendered.get("sector_name") and source.get("sector_name") != rendered["sector_name"]:
                        source["sector_name"] = rendered["sector_name"]
                        migrated = True
                    if not source.get("self_price") and not rendered.get("quote_error"):
                        source["self_price"] = rendered.get("self_price")
                        migrated = True
                for source, rendered in zip(state["positions"], positions):
                    if rendered.get("sector_name") and source.get("sector_name") != rendered["sector_name"]:
                        source["sector_name"] = rendered["sector_name"]
                        migrated = True
                if migrated:
                    self._write_state(state)
                public_watchlist = [
                    {key: value for key, value in item.items() if key != "_ai_series"}
                    for item in watchlist
                ]
                public_positions = [
                    {key: value for key, value in item.items() if key != "_ai_series"}
                    for item in positions
                ]
                signals = _pool_signals(state, watchlist + positions)
                active_model = _active_ensemble_runtime_policy()
                if active_model:
                    signals = _active_ensemble_signals(
                        signals, watchlist + positions, state, active_model
                    )
                miaoxiang_quota_messages = list(
                    dict.fromkeys(
                        message
                        for item in (watchlist + positions)
                        for diagnostic in (
                            item.get("quote_error"),
                            item.get("provider_diagnostics"),
                        )
                        if (message := _miaoxiang_quota_message(diagnostic))
                    )
                )
                response_payload = {
                        "watchlist": public_watchlist,
                        "positions": public_positions,
                        "indices": market_indices(refresh=refresh),
                        "manual_trades": sorted(
                            state["manual_trades"],
                            key=lambda trade: str(trade.get("time") or ""),
                            reverse=True,
                        ),
                        "signals": signals,
                        "active_model": active_model,
                        "initialized": state["initialized"],
                        "initial_equity": REVIEW_INITIAL_EQUITY,
                        "available_cash": round(_local_available_cash(state), 2),
                        "as_of": _now_iso(),
                        "_cache_version": POOL_CACHE_VERSION,
                        "cache": {"status": "refreshed" if refresh else "miss"},
                        "alerts": [
                            {
                                "code": "miaoxiang_quota_exhausted",
                                "level": "warning",
                                "title": "妙想 MCP 积分已用完",
                                "message": "刷新行情时检测到妙想 MCP 积分/额度已用完。当前标的未使用妙想数据，请补充额度后再刷新。",
                                "details": miaoxiang_quota_messages[:3],
                            }
                        ] if miaoxiang_quota_messages else [],
                        "miaoxiang_quota_exhausted": bool(miaoxiang_quota_messages),
                    }
                if refresh:
                    # Quotes and derived signals changed; force the next
                    # dashboard read to rebuild its stateful backtest.
                    self.backtest_cache_path.unlink(missing_ok=True)
                self._write_json_cache(self.pool_cache_path, response_payload)
                self._json(response_payload)
                return
            if parsed.path == "/api/reports/backtest-dashboard":
                pool_payload = self._read_json_cache(self.pool_cache_path) or {}
                self._json(
                    _backtest_dashboard_payload(
                        self._read_state(),
                        pool_payload=pool_payload,
                        force_current_backtest=(
                            not pool_payload
                            or
                            pool_payload.get("cache", {}).get("status") == "refreshed"
                        ),
                    )
                )
                return
            if parsed.path == "/api/backtest/review-baseline":
                days = int(query.get("days", ["60"])[0])
                refresh = query.get("refresh", [""])[0] in {"1", "true"}
                if not refresh:
                    cached_backtest = self._read_json_cache(self.backtest_cache_path)
                    if cached_backtest is not None:
                        cached_backtest["cache"] = {"status": "hit", "source": BACKTEST_CACHE_FILE}
                        self._json(cached_backtest)
                        return
                backtest_payload = _review_baseline_backtest(self._read_state(), calendar_days=max(30, min(days, 180)))
                self._write_json_cache(self.backtest_cache_path, backtest_payload)
                self._json(backtest_payload)
                return
            if parsed.path == "/api/ai/status":
                self._json(_get_ai_service().status())
                return
            if parsed.path == "/api/ai/latest":
                symbol = _code(query.get("symbol", [""])[0])
                self._json({"analysis": _get_ai_service().storage.latest(symbol)})
                return
            if parsed.path == "/api/ai/performance":
                self._json(_get_ai_service().storage.performance_report())
                return
            if parsed.path == "/api/ai/training":
                self._json(_get_ai_service().storage.training_dataset_summary())
                return
            if parsed.path == "/api/research/status":
                storage = _get_ai_service().storage
                state_snapshot = self._read_state()
                response = {
                    "pool_source": "local_review_center_state",
                    "positions": len(state_snapshot.get("positions") or []),
                    "watchlist": len(state_snapshot.get("watchlist") or []),
                    "latest_runs": storage.list_research_runs(limit=10, include_results=False),
                }
                # The signal-center status strip only needs the latest run.
                # Computing the full performance/training reports requires
                # scanning and decoding every historical observation and made
                # each page load take several seconds.  Keep that detail
                # available for explicit diagnostics without penalising the
                # normal UI path.
                if query.get("detail", [""])[0].lower() in {"1", "true", "yes"}:
                    response["performance"] = storage.performance_report()
                    response["training"] = storage.training_dataset_summary()
                self._json(response)
                return
            if parsed.path == "/api/research/runs":
                limit = int(query.get("limit", ["20"])[0])
                # Keep the default response bounded.  A completed optimize
                # run can contain a very large artifact (the historical
                # payload previously made this endpoint ~150 MB), while the
                # status poller only needs run metadata.  Full results remain
                # available through an explicit ``detail=1``/``summary=0``.
                summary_value = query.get("summary", [""])[0].lower()
                summary = not (
                    query.get("detail", [""])[0].lower() in {"1", "true", "yes"}
                    or summary_value in {"0", "false", "no"}
                )
                self._json(
                    {
                        "items": _get_ai_service().storage.list_research_runs(
                            limit, include_results=not summary
                        )
                    }
                )
                return
            if parsed.path == "/api/automation/status":
                storage = _get_ai_service().storage
                self._json(
                    {
                        "mode": "local_paper_trade_only",
                        "schedule": "weekdays_19:00_Asia/Shanghai",
                        "active_model": _active_ensemble_runtime_policy(storage),
                        "active_champion": _active_champion_runtime_policy(storage),
                        "latest_runs": storage.list_automation_runs(limit=10),
                        "research_performed": False,
                        "llm_agent_called": False,
                    }
                )
                return
            if parsed.path == "/api/automation/runs":
                limit = int(query.get("limit", ["20"])[0])
                self._json(
                    {"items": _get_ai_service().storage.list_automation_runs(limit)}
                )
                return
            if parsed.path == "/api/models":
                storage = _get_ai_service().storage
                self._json(
                    {
                        "items": storage.list_models(include_snapshots=False),
                        "active_champion": storage.active_champion(include_snapshot=False),
                        "ensemble": _active_ensemble_runtime_policy(storage),
                        "release_requests": storage.list_release_requests(),
                        "releases": storage.list_model_releases(include_snapshots=False),
                    }
                )
                return
            if parsed.path == "/api/models/release-gate":
                model_id = str(query.get("model_id", [""])[0])
                if not model_id:
                    self._json({"error": "缺少 model_id"}, status=400)
                    return
                self._json(_get_ai_service().storage.evaluate_release_gate(model_id))
                return
            if parsed.path == "/api/ai/history":
                symbol = _code(query.get("symbol", [""])[0]) if query.get("symbol") else None
                limit = int(query.get("limit", ["100"])[0])
                self._json({"items": _get_ai_service().storage.list_analyses(limit=limit, symbol=symbol)})
                return
            if parsed.path == "/api/miaoxiang/status":
                status = _get_ai_service().status()
                mx_config = _get_ai_service().config.miaoxiang
                self._json(
                    {
                        "enabled": mx_config.enabled,
                        "ready": status["miaoxiang_ready"],
                        "url": mx_config.url,
                        "trade_writes_enabled": False,
                        "self_select_requires_confirmation": True,
                        "read_tool_allowlist": list(mx_config.read_tool_allowlist),
                        "stock_limits": {
                            "max_tables": mx_config.stock_max_tables,
                            "max_rows": mx_config.stock_max_rows,
                            "max_value_chars": mx_config.stock_max_value_chars,
                            "max_total_chars": mx_config.stock_max_total_chars,
                        },
                        "events": {
                            "enabled": mx_config.news_enabled,
                            "days": mx_config.news_days,
                            "max_items": mx_config.news_max_items,
                            "max_chars": mx_config.news_max_chars,
                        },
                    }
                )
                return
            if parsed.path == "/api/miaoxiang/tools":
                import asyncio

                client = _llm_submodule("miaoxiang").MiaoxiangClient(
                    _get_ai_service().config.miaoxiang
                )
                self._json({"tools": asyncio.run(client.list_tools())})
                return
            if parsed.path == "/api/stocks/search":
                self._json({"items": stock_search(query.get("q", [""])[0])})
                return
            if parsed.path == "/api/stocks/kline":
                symbol = query.get("symbol", [""])[0]
                refresh = query.get("refresh", [""])[0] in {"1", "true"}
                self._json(stock_kline(symbol, refresh=refresh))
                return
        except Exception as exc:  # noqa: BLE001 - convert upstream errors to API JSON
            # A transient upstream/provider or concurrent refresh failure must
            # not blank the signal center.  Serve the last valid pool snapshot
            # with an explicit stale/error marker so the UI can remain usable
            # while making the failed refresh auditable.
            if parsed.path == "/api/pools":
                try:
                    cached_payload = self._read_json_cache(self.pool_cache_path)
                except Exception:  # noqa: BLE001 - fall through to JSON error
                    cached_payload = None
                if isinstance(cached_payload, dict) and cached_payload.get("_cache_version") == POOL_CACHE_VERSION:
                    cached_payload["cache"] = {
                        "status": "stale_error",
                        "source": POOL_CACHE_FILE,
                        "error": str(exc),
                    }
                    alerts = list(cached_payload.get("alerts") or [])
                    alerts.append(
                        {
                            "code": "pools_refresh_failed",
                            "level": "warning",
                            "title": "行情刷新暂时失败",
                            "message": "本次刷新未完成，已暂时显示最近一次有效票池数据；请稍后重试。",
                            "details": [str(exc)],
                        }
                    )
                    cached_payload["alerts"] = alerts
                    cached_payload["refresh_error"] = str(exc)
                    self._json(cached_payload)
                    return
            self._json({"error": str(exc)}, status=502)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = self._body()
            state = self._read_state()
            symbol = _code(str(payload.get("symbol", "")))
            if self.path == "/api/models/release/request":
                model_id = str(payload.get("model_id") or "")
                if not model_id:
                    self._json({"error": "缺少 model_id"}, status=400)
                    return
                try:
                    result = _get_ai_service().storage.request_model_release(
                        model_id,
                        requested_by=str(payload.get("requested_by") or "local_user"),
                        note=str(payload.get("note") or ""),
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, status=400)
                    return
                self._json(result)
                return
            if self.path == "/api/models/release/approve":
                try:
                    result = _get_ai_service().storage.approve_model_release(
                        str(payload.get("request_id") or ""),
                        approved_by=str(payload.get("approved_by") or "local_user"),
                        note=str(payload.get("note") or ""),
                        force=bool(payload.get("force", False)),
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, status=400)
                    return
                # A publish changes the immutable model parameters consumed by
                # signal-center inference and by the stateful backtest.  Drop
                # both caches immediately so the next read cannot expose a
                # pre-publish ensemble snapshot.
                self.pool_cache_path.unlink(missing_ok=True)
                self.backtest_cache_path.unlink(missing_ok=True)
                self._json(result)
                return
            if self.path == "/api/models/release/reject":
                try:
                    result = _get_ai_service().storage.reject_model_release(
                        str(payload.get("request_id") or ""),
                        decided_by=str(payload.get("decided_by") or "local_user"),
                        note=str(payload.get("note") or ""),
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, status=400)
                    return
                self._json(result)
                return
            if self.path == "/api/models/rollback":
                try:
                    result = _get_ai_service().storage.rollback_model_release(
                        target_model_id=str(payload.get("target_model_id") or "") or None,
                        actor=str(payload.get("actor") or "local_user"),
                        note=str(payload.get("note") or ""),
                    )
                except ValueError as exc:
                    self._json({"error": str(exc)}, status=400)
                    return
                # Rollback also changes the live ensemble lineage; invalidate
                # inference and report caches for the same reason as publish.
                self.pool_cache_path.unlink(missing_ok=True)
                self.backtest_cache_path.unlink(missing_ok=True)
                self._json(result)
                return
            if self.path == "/api/automation/trade":
                storage = _get_ai_service().storage
                running = next(
                    (
                        item
                        for item in storage.list_automation_runs(limit=10)
                        if item.get("action") == "daily_trade"
                        and item.get("status") == "running"
                    ),
                    None,
                )
                if running:
                    self._json(
                        {
                            "run_id": running.get("run_id"),
                            "action": "daily_trade",
                            "status": "running",
                            "background": True,
                            "deduplicated": True,
                        },
                        status=202,
                    )
                    return
                run_id = storage.create_automation_run(
                    "daily_trade",
                    {
                        "refresh": True,
                        "refresh_forced": True,
                        "mode": "local_paper_trade_only",
                        "account_source": "local_review_center_state",
                        "symbols": [
                            str(item.get("symbol"))
                            for item in (state.get("positions") or [])
                            + (state.get("watchlist") or [])
                            if item.get("symbol")
                        ],
                        "research_performed": False,
                        "llm_agent_called": False,
                    },
                )

                def execute_trade_job() -> dict[str, Any]:
                    try:
                        result = _run_daily_trade_action(state, refresh=True)
                        if result.get("status") == "failed":
                            storage.finish_automation_run(
                                run_id,
                                status="failed",
                                result=result,
                                error=str(result.get("reason") or "票池刷新失败"),
                            )
                            return {
                                "status": "failed",
                                "error": str(result.get("reason") or "票池刷新失败"),
                                "result": result,
                            }
                        if result.get("status") == "completed":
                            state["initialized"] = True
                            self._write_state(state)
                        storage.finish_automation_run(
                            run_id, status="completed", result=result
                        )
                        return {"status": "completed", "result": result}
                    except Exception as exc:  # noqa: BLE001 - auditable failure
                        storage.finish_automation_run(
                            run_id,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return {"status": "failed", "error": str(exc)}

                if bool(payload.get("background", False)):
                    Thread(
                        target=execute_trade_job,
                        name=f"automation-{run_id}",
                        daemon=True,
                    ).start()
                    self._json(
                        {
                            "run_id": run_id,
                            "action": "daily_trade",
                            "status": "running",
                            "started_at": _now_iso(),
                            "background": True,
                        },
                        status=202,
                    )
                    return

                outcome = execute_trade_job()
                if outcome["status"] == "failed":
                    self._json(
                        {
                            "run_id": run_id,
                            "action": "daily_trade",
                            "error": outcome["error"],
                        },
                        status=502,
                    )
                else:
                    self._json(
                        {
                            "run_id": run_id,
                            "action": "daily_trade",
                            "result": outcome["result"],
                        }
                    )
                return
            if self.path == "/api/research/execute":
                if not bool(payload.get("confirm")):
                    preview_state = json.loads(json.dumps(state, ensure_ascii=False))
                    preview = _execute_local_signals(
                        preview_state, refresh=bool(payload.get("refresh", False))
                    )
                    self._json(
                        {
                            "status": "requires_confirmation",
                            "message": "该操作只会写入本地模拟交易和持仓池；请显式传 confirm=true。",
                            "preview": preview,
                        },
                        status=409,
                    )
                    return
                result = _execute_local_signals(
                    state, refresh=bool(payload.get("refresh", True))
                )
                if result.get("applied"):
                    state["initialized"] = True
                    self._write_state(state)
                self._json(result)
                return
            if self.path == "/api/research/run":
                action = str(payload.get("action") or "full").strip().lower()
                aliases = {
                    "backtest": "backtest",
                    "回测": "backtest",
                    "labels": "labels",
                    "label": "labels",
                    "更新标签": "labels",
                    "metrics": "metrics",
                    "指标": "metrics",
                    "train": "train",
                    "training": "train",
                    "训练": "train",
                    "historical_train": "historical_train",
                    "historical": "historical_train",
                    "历史回放训练": "historical_train",
                    "optimize": "optimize",
                    "optimization": "optimize",
                    "优化": "optimize",
                    "full": "full",
                    "全部": "full",
                }
                action = aliases.get(action, action)
                if action not in {"backtest", "labels", "metrics", "train", "historical_train", "optimize", "full"}:
                    self._json({"error": f"不支持的研究动作: {action}"}, status=400)
                    return
                days = max(30, min(int(payload.get("calendar_days") or 60), 180))
                start_date = str(payload.get("start_date") or "").strip() or None
                end_date = str(payload.get("end_date") or "").strip() or None
                universe = str(payload.get("universe") or "pool").strip().lower()
                if universe in {"hs300", "沪深300"}:
                    universe = "csi300"
                if universe in {
                    "csi300+watchlist",
                    "hs300_plus_watchlist",
                    "沪深300+观察池",
                    "沪深300加观察池",
                    "combined",
                }:
                    universe = "csi300_plus_watchlist"
                if universe not in {"pool", "csi300", "csi300_plus_watchlist"}:
                    self._json({"error": f"不支持的训练股票池: {universe}"}, status=400)
                    return
                try:
                    parsed_start = datetime.fromisoformat(start_date).date() if start_date else None
                    parsed_end = datetime.fromisoformat(end_date).date() if end_date else None
                except ValueError:
                    self._json({"error": "start_date/end_date 必须使用 YYYY-MM-DD 格式"}, status=400)
                    return
                if parsed_start and parsed_end and parsed_start > parsed_end:
                    self._json({"error": "start_date 不能晚于 end_date"}, status=400)
                    return
                storage = _get_ai_service().storage
                # Research actions are CPU/network intensive and share the
                # same local artifacts. Do not start a second job while one is
                # already running; callers can poll the existing run instead.
                # Listing also reconciles abandoned rows after a crash.
                running = next(
                    (
                        item
                        for item in storage.list_research_runs(
                            limit=20, include_results=False
                        )
                        if item.get("status") == "running"
                    ),
                    None,
                )
                if running:
                    self._json(
                        {
                            "run_id": running.get("run_id"),
                            "action": running.get("action"),
                            "status": "running",
                            "background": True,
                            "deduplicated": True,
                        },
                        status=202,
                    )
                    return
                run_id = storage.create_research_run(
                    action,
                    {
                        "calendar_days": days,
                        "start_date": start_date,
                        "end_date": end_date,
                        "refresh": bool(payload.get("refresh", True)),
                        "pool_source": (
                            "csi300_plus_local_watchlist"
                            if universe == "csi300_plus_watchlist"
                            else (
                                "csi300_current_constituents"
                                if universe == "csi300"
                                else "local_review_center_state"
                            )
                        ),
                        "universe": universe,
                        "symbols": [
                            str(item.get("symbol"))
                            for item in (state.get("positions") or [])
                            + (state.get("watchlist") or [])
                            if item.get("symbol")
                        ],
                    },
                )
                refresh_research = bool(payload.get("refresh", True))

                def execute_research_job() -> dict[str, Any]:
                    try:
                        result = _run_research_action(
                            action,
                            state,
                            calendar_days=days,
                            refresh=refresh_research,
                            start_date=start_date,
                            end_date=end_date,
                            universe=universe,
                        )
                        storage.finish_research_run(
                            run_id, status="completed", result=result
                        )
                        return {"status": "completed", "result": result}
                    except Exception as exc:  # noqa: BLE001 - auditable failure
                        storage.finish_research_run(
                            run_id,
                            status="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return {"status": "failed", "error": str(exc)}

                if bool(payload.get("background", False)):
                    Thread(
                        target=execute_research_job,
                        name=f"research-{run_id}",
                        daemon=True,
                    ).start()
                    self._json(
                        {
                            "run_id": run_id,
                            "action": action,
                            "status": "running",
                            "started_at": _now_iso(),
                            "background": True,
                        },
                        status=202,
                    )
                    return

                outcome = execute_research_job()
                if outcome["status"] == "failed":
                    self._json(
                        {
                            "run_id": run_id,
                            "action": action,
                            "error": outcome["error"],
                        },
                        status=502,
                    )
                else:
                    self._json(
                        {
                            "run_id": run_id,
                            "action": action,
                            "result": outcome["result"],
                        }
                    )
                return
            if self.path in {"/api/ai/analyze", "/api/ai/daily-run"}:
                raw_symbols = list(payload.get("symbols") or [])
                symbols = list(
                    dict.fromkeys(_code(value) for value in raw_symbols if _code(value))
                )
                if self.path == "/api/ai/daily-run" or payload.get("daily_run"):
                    symbols = list(dict.fromkeys(
                        [str(item.get("symbol")) for item in (state.get("positions") or []) + (state.get("watchlist") or []) if item.get("symbol")]
                    ))
                if not symbols:
                    self._json({"error": "请至少选择一只票"}, status=400)
                    return
                if len(symbols) > 20:
                    self._json({"error": "单次最多分析 20 只票"}, status=400)
                    return
                allowed = {
                    str(item.get("symbol"))
                    for item in list(state.get("watchlist") or [])
                    + list(state.get("positions") or [])
                }
                unknown = [value for value in symbols if value not in allowed]
                if unknown:
                    self._json(
                        {"error": f"标的不在当前票池: {', '.join(unknown)}"}, status=400
                    )
                    return
                source_by_symbol = {
                    str(item.get("symbol")): item
                    for item in list(state.get("watchlist") or [])
                    + list(state.get("positions") or [])
                }
                with ThreadPoolExecutor(
                    max_workers=max(1, min(8, len(symbols)))
                ) as executor:
                    rendered = list(
                        executor.map(
                            lambda value: self._pool_item(
                                source_by_symbol[value],
                                refresh=bool(payload.get("refresh")),
                            ),
                            symbols,
                        )
                    )
                indices = market_indices(refresh=bool(payload.get("refresh")))
                results = []
                for item in rendered:
                    if item.get("quote_error"):
                        results.append(
                            {
                                "symbol": item.get("symbol"),
                                "error": item.get("quote_error"),
                            }
                        )
                        continue
                    context = _build_ai_context(
                        {**item, "series": item.get("_ai_series") or {}}, state, indices
                    )
                    results.append(
                        _get_ai_service().analyze(
                            context,
                            account_equity=_float(
                                payload.get("account_equity"),
                                _float(
                                    (context.get("portfolio_context") or {}).get(
                                        "account_equity"
                                    ),
                                    REVIEW_INITIAL_EQUITY,
                                ),
                            ),
                        )
                    )
                # Signal cards are cached independently from analysis rows.
                # Invalidate that cache after a targeted LLM run so the next
                # signal-center load reads the newly persisted result instead
                # of showing an older same-day analysis for the symbol.
                try:
                    self.pool_cache_path.unlink(missing_ok=True)
                except OSError:
                    pass
                self._json({"results": results, "status": _get_ai_service().status()})
                return
            if self.path == "/api/ai/replay":
                analysis_id = str(payload.get("analysis_id") or "")
                if not analysis_id:
                    self._json({"error": "缺少 analysis_id"}, status=400)
                    return
                self._json(
                    _get_ai_service().replay(
                        analysis_id, persist=bool(payload.get("persist"))
                    )
                )
                return
            if self.path == "/api/ai/replay-batch":
                limit = max(1, min(int(payload.get("limit") or 20), 100))
                items = _get_ai_service().storage.list_analyses(limit=limit)
                if not payload.get("confirm"):
                    self._json({
                        "status": "requires_confirmation",
                        "count": len(items),
                        "message": "批量重放会重新消耗 LLM 配额，请显式传 confirm=true。",
                    })
                    return
                results = []
                for item in items:
                    analysis_id = str(item.get("analysis_id") or "")
                    if analysis_id:
                        results.append(_get_ai_service().replay(analysis_id, persist=bool(payload.get("persist"))))
                self._json({"status": "completed", "count": len(results), "results": results})
                return
            if self.path == "/api/ai/feedback":
                analysis_id = str(payload.get("analysis_id") or "")
                feedback_type = str(payload.get("feedback_type") or "")
                if not analysis_id or not feedback_type:
                    self._json(
                        {"error": "缺少 analysis_id 或 feedback_type"}, status=400
                    )
                    return
                _get_ai_service().storage.record_feedback(
                    analysis_id,
                    feedback_type,
                    payload.get("value"),
                    str(payload.get("note") or ""),
                )
                self._json({"ok": True})
                return
            if self.path == "/api/watchlist":
                if any(item.get("symbol") == symbol for item in state["watchlist"]):
                    self._json({"error": "标的已在观察票池"}, status=409)
                    return
                quote = stock_kline(symbol)
                state["watchlist"].append(
                    {
                        "symbol": symbol,
                        "name": quote["name"],
                        "self_price": quote["current_price"],
                        "note": "",
                    }
                )
                state["initialized"] = True
                self._write_state(state)
                self._json({"ok": True})
                return
            if self.path == "/api/positions":
                if any(item.get("symbol") == symbol for item in state["positions"]):
                    self._json({"error": "标的已在持仓票池"}, status=409)
                    return
                quote = stock_kline(symbol)
                state["positions"].append(
                    {
                        "symbol": symbol,
                        "name": quote["name"],
                        "quantity": _float(payload.get("quantity")),
                        "entry_price": _float(
                            payload.get("cost"), quote["current_price"]
                        ),
                    }
                )
                state.pop("available_cash", None)
                state["initialized"] = True
                self._write_state(state)
                self._json({"ok": True})
                return
            if self.path == "/api/simulated-trades":
                action = str(payload.get("action", "")).strip().lower()
                price = _float(payload.get("price"))
                quantity = _float(payload.get("quantity"))
                if action not in ("buy", "sell") or price <= 0 or quantity <= 0:
                    self._json(
                        {"error": "模拟交易需要 buy/sell、正数价格和正数数量"},
                        status=400,
                    )
                    return
                quote = stock_kline(symbol)
                position = next(
                    (
                        item
                        for item in state["positions"]
                        if item.get("symbol") == symbol
                    ),
                    None,
                )
                realized_pnl = 0.0
                cash = _local_available_cash(state)
                if action == "buy":
                    if price * quantity > cash + 1e-9:
                        self._json(
                            {"error": f"本地可用现金不足：可用 {cash:.2f} 元"},
                            status=409,
                        )
                        return
                    if position is None:
                        position = {
                            "symbol": symbol,
                            "name": quote["name"],
                            "quantity": quantity,
                            "entry_price": price,
                        }
                        state["positions"].append(position)
                    else:
                        old_quantity = _float(position.get("quantity"))
                        old_cost = _float(position.get("entry_price"))
                        position["quantity"] = old_quantity + quantity
                        position["entry_price"] = (
                            (old_quantity * old_cost) + (quantity * price)
                        ) / position["quantity"]
                else:
                    if position is None or _float(position.get("quantity")) < quantity:
                        self._json({"error": "模拟卖出数量超过当前持仓"}, status=400)
                        return
                    realized_pnl = (
                        price - _float(position.get("entry_price"))
                    ) * quantity
                    position["quantity"] = _float(position.get("quantity")) - quantity
                    if position["quantity"] <= 1e-12:
                        state["positions"].remove(position)
                trade = {
                    "id": f"manual-{len(state['manual_trades']) + 1}",
                    "symbol": symbol,
                    "name": quote.get("name", symbol),
                    "action": action,
                    "price": price,
                    "quantity": quantity,
                    "net_pnl": realized_pnl,
                    "time": _now_iso(),
                }
                state["manual_trades"].append(trade)
                state["available_cash"] = round(
                    cash - price * quantity
                    if action == "buy"
                    else cash + price * quantity,
                    2,
                )
                state["initialized"] = True
                self._write_state(state)
                self._json({"ok": True, "trade": trade})
                return
            self._json({"error": "未知 API"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, status=502)

    def do_PUT(self) -> None:  # noqa: N802
        try:
            payload = self._body()
            symbol = _code(str(payload.get("symbol", "")))
            state = self._read_state()
            for item in state["positions"]:
                if item.get("symbol") == symbol:
                    item["quantity"] = _float(payload.get("quantity"))
                    item["entry_price"] = _float(payload.get("cost"))
                    state.pop("available_cash", None)
                    state["initialized"] = True
                    self._write_state(state)
                    self._json({"ok": True})
                    return
            self._json({"error": "持仓标的不在票池"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, status=400)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        symbol = _code(unquote(query.get("symbol", [""])[0]))
        state = self._read_state()
        if parsed.path == "/api/watchlist":
            state["watchlist"] = [
                item for item in state["watchlist"] if item.get("symbol") != symbol
            ]
        elif parsed.path == "/api/positions":
            state["positions"] = [
                item for item in state["positions"] if item.get("symbol") != symbol
            ]
            state.pop("available_cash", None)
        else:
            self._json({"error": "未知 API"}, status=404)
            return
        state["initialized"] = True
        self._write_state(state)
        self._json({"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="AKQuant local review center server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", default=".")
    parser.add_argument(
        "--llm-config",
        default=None,
        help="LLM 本地配置路径，默认读取仓库 llm_trade.local.yaml",
    )
    args = parser.parse_args()
    if args.llm_config:
        os.environ["AKQUANT_LLM_CONFIG"] = str(Path(args.llm_config).resolve())
    root = Path(args.root).resolve()
    handler = lambda *handler_args, **handler_kwargs: ReviewCenterHandler(  # noqa: E731
        *handler_args, directory=str(root), **handler_kwargs
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    Thread(
        target=_daily_trade_scheduler_loop,
        args=(root,),
        name="daily-local-paper-trade-scheduler",
        daemon=True,
    ).start()
    print(
        f"AKQuant review center: http://{args.host}:{args.port}/akquant_review_center.html"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
