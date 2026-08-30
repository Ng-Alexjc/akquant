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
    normalize_series,
    multi_objective_optimize,
    persist_artifact,
    probability_of_backtest_overfitting,
    purged_walk_forward_optimize,
    simulate_trend_swing,
    simulate_strategy,
    walk_forward_optimize,
)

EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
EASTMONEY_TRENDS = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
STATE_FILE = ".review_center_state.json"
POOL_CACHE_FILE = ".review_center_pool_cache.json"
BACKTEST_CACHE_FILE = ".review_center_backtest_cache.json"
POOL_CACHE_VERSION = 2
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
_DIRECT_OPENER = build_opener(ProxyHandler({}))
_STOCK_KLINE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STOCK_KLINE_CACHE_LOCK = RLock()
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
        "strategy_version": "review_center_momentum_logit_dual_horizon_v2",
        "threshold_version": "review_center_thresholds_2026-08-26",
        "thresholds": {
            "watch": {"score_min": WATCH_SCORE_THRESHOLD, "next_day_probability_min": WATCH_PROBABILITY_THRESHOLD},
            "buy": {"score_min": BUY_SCORE_THRESHOLD, "next_day_probability_min": BUY_PROBABILITY_THRESHOLD, "price_above": "MA20"},
            "strong_buy": {"score_min": STRONG_BUY_SCORE_THRESHOLD, "next_day_probability_min": STRONG_BUY_PROBABILITY_THRESHOLD, "price_relation": "close > MA20 > MA60"},
            "add": {"score_min": ADD_SCORE_THRESHOLD, "next_day_probability_min": ADD_PROBABILITY_THRESHOLD, "price_relation": "close > MA20 > MA60"},
            "hard_stop": {"loss_pct": 0.08, "exit_ratio": 1.0},
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
    score = _float(analysis.get("selection_score"))
    probability = _float(((analysis.get("probabilities") or {}).get("next_trading_day") or {}).get("value"), -1.0)
    close, ma20, ma60 = _float(analysis.get("close")), _float(analysis.get("ma20")), _float(analysis.get("ma60"))
    momentum = _float(analysis.get("momentum20"))
    if holding and close < ma60 and momentum < 0:
        return {"action": "清仓", "trigger": "close < MA60 and momentum20 < 0"}
    if holding and probability >= 0 and probability <= 0.40 and close < ma20:
        return {"action": "减仓", "trigger": "next_day_probability <= 0.40 and close < MA20"}
    if holding and score >= ADD_SCORE_THRESHOLD and probability >= ADD_PROBABILITY_THRESHOLD and close > ma20 > ma60:
        return {"action": "加仓", "trigger": "add thresholds + close > MA20 > MA60"}
    if not holding and score >= STRONG_BUY_SCORE_THRESHOLD and probability >= STRONG_BUY_PROBABILITY_THRESHOLD and close > ma20 > ma60:
        return {"action": "买入", "trigger": "strong-buy thresholds + close > MA20 > MA60"}
    if not holding and score >= BUY_SCORE_THRESHOLD and probability >= BUY_PROBABILITY_THRESHOLD and close > ma20:
        return {"action": "买入", "trigger": "buy thresholds + close > MA20"}
    if not holding and score >= WATCH_SCORE_THRESHOLD and probability >= WATCH_PROBABILITY_THRESHOLD:
        return {"action": "等待买入", "trigger": "watch thresholds met; trend confirmation pending"}
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
    volumes = [volume_by_time.get(str(candle.get("time")), 0.0) for candle in candles]
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
    probability = next_day.get("value")
    validation_accuracy = (next_day.get("validation") or {}).get("accuracy")
    training_samples = next_day.get("training_samples", 0)

    score = 50.0
    score += _clip(momentum20 * 150.0, -20.0, 20.0)
    score += 10.0 if close > ma20 else -10.0
    score += 8.0 if ma20 > ma60 else -8.0
    score += 5.0 if 45.0 <= rsi14 <= 70.0 else (-6.0 if rsi14 >= 80.0 else 0.0)
    score += _clip((volume_ratio - 1.0) * 5.0, -5.0, 5.0)
    score = round(_clip(score, 0.0, 100.0), 1)
    atr14 = _atr(candles)
    recent_high20 = max(_float(candle.get("high")) for candle in candles[-20:])
    recent_low20 = min(_float(candle.get("low")) for candle in candles[-20:])
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
        "trend_strength": ma20 / ma60 - 1.0 if ma60 else 0.0,
        "volatility20": volatility20,
        "breakout20": close / recent_high20 - 1.0 if recent_high20 else 0.0,
        "pullback20": close / recent_low20 - 1.0 if recent_low20 else 0.0,
        "volume_zscore20": volume_zscore20,
        "atr14": atr14,
        "resistance_price": resistance_price,
        "support_price": support_price,
        "selection_score": score,
        "up_probability": probability,
        "probabilities": probability_result["probabilities"],
        "assessment_status": probability_result["assessment_status"],
        "unavailable_reason": next_day.get("unavailable_reason"),
        "validation_accuracy": validation_accuracy,
        "training_samples": training_samples,
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
        key=lambda candidate: _float(candidate["analysis"].get("selection_score")),
        reverse=True,
    )
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
        probability_value = analysis.get("up_probability")
        probability = (
            _float(probability_value) if probability_value is not None else None
        )
        momentum20 = _float(analysis.get("momentum20"))
        ma5 = _float(analysis.get("ma5"))
        ma20 = _float(analysis.get("ma20"))
        ma60 = _float(analysis.get("ma60"))
        atr14 = _float(analysis.get("atr14"))
        entry_price = _float(position.get("entry_price")) if position else 0.0
        position_return = current_price / entry_price - 1.0 if entry_price > 0 else 0.0

        action = "观察"
        trigger = "等待评分、预测概率与趋势形成共振"
        if holding:
            hard_stop = entry_price > 0 and position_return <= -0.08
            trend_exit = current_price < ma60 and momentum20 < 0
            model_exit = (
                probability is not None and probability <= 0.40 and current_price < ma20
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
                score >= ADD_SCORE_THRESHOLD
                and probability is not None
                and probability >= ADD_PROBABILITY_THRESHOLD
                and current_price > ma20 > ma60
            ):
                action = "加仓"
                trigger = "高评分、上涨概率与多头排列共振"
            else:
                action = "持有"
                trigger = "持仓未触发止损或加仓条件"
        elif (
            score >= STRONG_BUY_SCORE_THRESHOLD
            and probability is not None
            and probability >= STRONG_BUY_PROBABILITY_THRESHOLD
            and current_price > ma20 > ma60
        ):
            action = "买入"
            trigger = "达到强势买入阈值，评分、概率与多头排列共振"
        elif (
            score >= BUY_SCORE_THRESHOLD
            and probability is not None
            and probability >= BUY_PROBABILITY_THRESHOLD
            and current_price > ma20
        ):
            action = "买入"
            trigger = "达到普通买入阈值，评分与上涨概率同步转强"
        elif (
            score >= WATCH_SCORE_THRESHOLD
            and probability is not None
            and probability >= WATCH_PROBABILITY_THRESHOLD
        ):
            action = "等待买入"
            trigger = "达到候选关注阈值，等待价格趋势进一步确认"

        validation_accuracy = analysis.get("validation_accuracy")
        validation_text = (
            f" · 验证{_float(validation_accuracy) * 100:.0f}%"
            if validation_accuracy is not None
            else ""
        )
        stop_price = max(0.0, current_price - max(2.0 * atr14, current_price * 0.06))
        take_profit = current_price + max(3.0 * atr14, current_price * 0.10)
        reason = (
            f"{trigger}；20日动量 {momentum20:+.1%} · {analysis.get('trend', '趋势未知')}"
            f"{validation_text} · 风险参考 {stop_price:.2f}/{take_profit:.2f}"
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
                    "tag": f"score={score:.1f};up_probability={probability if probability is not None else 'null'}",
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
            "pool": "持仓" if holding else "观察",
            "action": action,
            "current_price": round(current_price, 3),
            "suggested_price": (
                round(suggested_price, 3) if suggested_price is not None else None
            ),
            "selection_rank": rank,
            "selection_score": score,
            "up_probability": probability,
            "probabilities": analysis.get("probabilities") or {},
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


def _secid(symbol: str) -> str:
    code = _code(symbol)
    # 沪市股票/ETF 以 1 开头,深市/北交所以 0 开头,足够覆盖 A 股票池查询。
    market = "1" if code.startswith(("5", "6", "9")) else "0"
    return f"{market}.{code}"


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urlencode({key: str(value) for key, value in params.items()})
    target = f"{url}?{query}"
    # curl reaches Tencent and most Eastmoney endpoints much faster than a
    # fresh PowerShell process.  Retain PowerShell as a Windows fallback for
    # endpoints which occasionally close curl's TLS connection.
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if curl:
        try:
            completed = subprocess.run(
                [curl, "-sS", "-L", "--max-time", "15", "-A", "Mozilla/5.0", target],
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
                "(Invoke-WebRequest -UseBasicParsing "
                f"-Uri '{target}' -TimeoutSec 15).Content"
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
                "User-Agent": "Mozilla/5.0 AKQuant-Review-Center",
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": "https://quote.eastmoney.com/",
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
    try:
        quote = (
            _get_json(
                EASTMONEY_QUOTE,
                {
                    "secid": _secid(code),
                    "fields": "f57,f58,f43,f47,f48,f60,f170,f127,f128,f129",
                },
            ).get("data")
            or {}
        )
    except Exception:  # noqa: BLE001 - daily K line remains available without quote
        quote = {}
    name = _repair_text(quote.get("f58") or code, code)
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
            else previous_price
        ),
        "change_pct": (
            _float(quote.get("f170")) / 100.0
            if quote.get("f170") is not None
            else (
                (last["close"] / previous_price - 1.0) * 100.0
                if previous_price
                else 0.0
            )
        ),
        "volume": last["value"],
        "volume_change_pct": (
            (last["value"] / previous_volume - 1.0) * 100.0 if previous_volume else 0.0
        ),
        "sector_name": _repair_text(quote.get("f127")) or None,
        "region_name": _repair_text(quote.get("f128")) or None,
        "concept_names": _repair_text(quote.get("f129")) or None,
        "updated_at": last["time"],
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


def stock_kline(symbol: str, refresh: bool = False) -> dict[str, Any]:
    """Return a short-lived cached quote/K-line payload for one A-share."""
    code = _code(symbol)
    now = time.monotonic()
    if not refresh:
        with _STOCK_KLINE_CACHE_LOCK:
            cached = _STOCK_KLINE_CACHE.get(code)
        if cached and now - cached[0] < QUOTE_CACHE_TTL_SECONDS:
            return cached[1]
    try:
        result = _stock_kline_tencent(code)
    except Exception:
        result = _stock_kline_eastmoney(code)
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
            if row["close"] > ma5 > ma20 and momentum20 > 0:
                candidates.append((momentum20, symbol, row))
        candidates.sort(reverse=True)
        for _, symbol, row in candidates:
            if len(holdings) >= 5:
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
    return {"status": "valid", "strategy": "short_term_trend_baseline_v1", "window": {"start": dates[0], "end": dates[-1], "calendar_days": calendar_days}, "symbols": symbols, "errors": errors, "assumptions": {"initial_equity": initial, "entry_weight": 0.10, "max_positions": 5, "commission_rate": commission_rate, "stamp_tax_rate": stamp_tax_rate, "slippage_rate": slippage_rate, "lot_size": 100}, "summary": summary, "equity_curve": curve, "trades": trades}


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


def _run_research_action(
    action: str, state: dict[str, Any], *, calendar_days: int = 60, refresh: bool = True
) -> dict[str, Any]:
    """Run a local, auditable research action against the pool ledger."""
    storage = _get_ai_service().storage
    if action in {"backtest", "labels", "metrics", "optimize", "full"}:
        items = _research_pool_items(state, refresh=refresh)
        series_by_symbol = {
            str(item.get("symbol")): item.get("_ai_series")
            for item in items
            if item.get("symbol") and item.get("_ai_series")
        }
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
            "max_positions": 5,
            "entry_weight": 0.10,
            "commission_rate": 0.0003,
            "stamp_tax_rate": 0.0005,
            "slippage_rate": 0.001,
        }
        param_grid = {
            "fast_window": [3, 5, 8],
            "slow_window": [15, 20, 30],
            "momentum_window": [10, 20],
        }
        for strategy_name in ("trend_swing", "breakout_pullback", "momentum_regime"):
            strategies[strategy_name] = simulate_strategy(series_by_symbol, strategy=strategy_name, **base_params)
            wfo[strategy_name] = walk_forward_optimize(
                series_by_symbol,
                param_grid,
                strategy=strategy_name,
                initial_cash=REVIEW_INITIAL_EQUITY,
                train_bars=120,
                test_bars=30,
            )
        optuna_storage = Path.cwd() / "research_artifacts" / "optuna_trials.sqlite3"
        optimization = {
            name: multi_objective_optimize(
                series_by_symbol,
                param_grid,
                strategy=name,
                initial_cash=REVIEW_INITIAL_EQUITY,
                max_trials=60,
                storage_path=optuna_storage,
                study_name=f"akquant-{SIMULATOR_VERSION}-{dataset['snapshot']['version']}-{name}",
            )
            for name in strategies
        }
        purged = {
            name: purged_walk_forward_optimize(
                series_by_symbol, param_grid, strategy=name,
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
            baseline_model_id = f"champion-baseline-{dataset['snapshot']['version']}"
            active_champion = storage.active_champion()
            if active_champion is None or (
                active_champion.get("model_id") == baseline_model_id
                and not active_champion.get("published_at")
            ):
                storage.register_model(
                    baseline_model_id,
                    model_name=best_name, role="champion", status="active_paper",
                    version=dataset["snapshot"]["version"],
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
                }
                storage.register_model(
                    f"challenger-{strategy_name}-{dataset['snapshot']['version']}",
                    model_name=strategy_name, role="challenger", status="evaluated",
                    version=dataset["snapshot"]["version"], metrics=candidate_metrics,
                )
        if action == "optimize":
            result = {"status": "completed", "dataset": dataset_meta, "optimization": optimization, "purged_walk_forward": purged, "robustness": robustness, "models": storage.list_models()}
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
        training = storage.train_candidate_models()
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
            "models": storage.list_models(),
            "performance": performance,
            "training": training,
        }
        artifact = persist_artifact(Path.cwd() / "research_artifacts", f"full-{dataset['snapshot']['version']}", result)
        result["artifact_path"] = artifact
        return result
    if action == "train":
        result = storage.train_candidate_models()
        artifact = persist_artifact(Path.cwd() / "research_artifacts", f"train-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}", result)
        result["artifact_path"] = artifact
        return result
    raise ValueError(f"不支持的研究动作: {action}")


def _sector_strength_value(signal: dict[str, Any]) -> float:
    context = signal.get("sector_context") or signal.get("market_context") or {}
    raw = context.get("strength") if isinstance(context, dict) else None
    if raw is None:
        raw = signal.get("sector_strength")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw or "").lower()
    return 3.0 if any(token in text for token in ("强", "strong", "hot", "上升")) else (1.0 if any(token in text for token in ("弱", "weak", "冷", "下降")) else 2.0)


def _execute_local_signals(
    state: dict[str, Any], *, refresh: bool = True, automated: bool = False
) -> dict[str, Any]:
    """Apply actionable signals to the local simulated ledger only.

    Scheduled automation never asks for interactive confirmation.  It may
    execute only signals without an explicit model/rule conflict and without
    a human-review flag.  The manual endpoint keeps its existing explicit
    confirmation flow and can therefore remain available for operator review.
    """
    rendered = _research_pool_items(state, refresh=refresh)
    signals = _pool_signals(state, rendered)
    # Exit first; when cash is limited, buy/add orders are ranked by the
    # requested priority: selection score, upward probability, sector strength.
    exits = [signal for signal in signals if (signal.get("execution_signal") or {}).get("action") == "sell"]
    entries = [signal for signal in signals if (signal.get("execution_signal") or {}).get("action") == "buy"]
    others = [signal for signal in signals if signal not in exits and signal not in entries]
    entries.sort(key=lambda signal: (_float(signal.get("selection_score")), _float(signal.get("up_probability"), -1.0), _sector_strength_value(signal)), reverse=True)
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
            "source": "scheduled_local_research_signal"
            if automated
            else "local_research_signal",
        }
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
    }


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
                merged[key] = value
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
                    max_workers=max(1, min(8, len(sources)))
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
                    if not source.get("self_price") and not rendered.get("quote_error"):
                        source["self_price"] = rendered.get("self_price")
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
                response_payload = {
                        "watchlist": public_watchlist,
                        "positions": public_positions,
                        "indices": market_indices(refresh=refresh),
                        "manual_trades": sorted(
                            state["manual_trades"],
                            key=lambda trade: str(trade.get("time") or ""),
                            reverse=True,
                        ),
                        "signals": _pool_signals(state, watchlist + positions),
                        "initialized": state["initialized"],
                        "initial_equity": REVIEW_INITIAL_EQUITY,
                        "available_cash": round(_local_available_cash(state), 2),
                        "as_of": _now_iso(),
                        "_cache_version": POOL_CACHE_VERSION,
                        "cache": {"status": "refreshed" if refresh else "miss"},
                    }
                self._write_json_cache(self.pool_cache_path, response_payload)
                self._json(response_payload)
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
                self._json(
                    {
                        "pool_source": "local_review_center_state",
                        "positions": len(self._read_state().get("positions") or []),
                        "watchlist": len(self._read_state().get("watchlist") or []),
                        "latest_runs": storage.list_research_runs(limit=10),
                        "performance": storage.performance_report(),
                        "training": storage.training_dataset_summary(),
                    }
                )
                return
            if parsed.path == "/api/research/runs":
                limit = int(query.get("limit", ["20"])[0])
                self._json({"items": _get_ai_service().storage.list_research_runs(limit)})
                return
            if parsed.path == "/api/models":
                storage = _get_ai_service().storage
                self._json(
                    {
                        "items": storage.list_models(),
                        "active_champion": storage.active_champion(),
                        "release_requests": storage.list_release_requests(),
                        "releases": storage.list_model_releases(),
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
                self._json(result)
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
                    "optimize": "optimize",
                    "optimization": "optimize",
                    "优化": "optimize",
                    "full": "full",
                    "全部": "full",
                    "daily_auto": "daily_auto",
                    "auto": "daily_auto",
                    "每日自动交易": "daily_auto",
                }
                action = aliases.get(action, action)
                if action not in {"backtest", "labels", "metrics", "train", "optimize", "full", "daily_auto"}:
                    self._json({"error": f"不支持的研究动作: {action}"}, status=400)
                    return
                days = max(30, min(int(payload.get("calendar_days") or 60), 180))
                storage = _get_ai_service().storage
                run_id = storage.create_research_run(
                    action,
                    {
                        "calendar_days": days,
                        "refresh": bool(payload.get("refresh", True)),
                        "pool_source": "local_review_center_state",
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
                        research_action = "full" if action == "daily_auto" else action
                        result = _run_research_action(
                            research_action,
                            state,
                            calendar_days=days,
                            refresh=refresh_research,
                        )
                        if action == "daily_auto":
                            snapshot_end = str(
                                (result.get("dataset") or {})
                                .get("snapshot", {})
                                .get("end")
                                or ""
                            )[:10]
                            local_today = datetime.now().date().isoformat()
                            if snapshot_end != local_today:
                                execution = {
                                    "status": "skipped",
                                    "reason": "最新行情日期不是今天，疑似周末/节假日或行情未更新",
                                    "snapshot_end": snapshot_end,
                                    "local_today": local_today,
                                    "applied": [],
                                    "skipped": [],
                                }
                            else:
                                execution = _execute_local_signals(
                                    state,
                                    refresh=refresh_research,
                                    automated=True,
                                )
                            if execution.get("applied"):
                                state["initialized"] = True
                                self._write_state(state)
                            result = {
                                "status": "completed",
                                "research": result,
                                "execution": execution,
                                "mode": "local_paper_only",
                            }
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
    handler = lambda *handler_args, **handler_kwargs: ReviewCenterHandler(  # noqa: E731
        *handler_args, directory=str(Path(args.root).resolve()), **handler_kwargs
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(
        f"AKQuant review center: http://{args.host}:{args.port}/akquant_review_center.html"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
