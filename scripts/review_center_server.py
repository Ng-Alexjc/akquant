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
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener

EASTMONEY_QUOTE = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"
EASTMONEY_TRENDS = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_CLIST = "https://push2.eastmoney.com/api/qt/clist/get"
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
STATE_FILE = ".review_center_state.json"
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
    cash = initial
    for trade in state.get("manual_trades") or []:
        amount = _float(trade.get("price")) * _float(trade.get("quantity"))
        cash += amount if str(trade.get("action")).lower() == "sell" else -amount
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


def _extract_capital_flow(payload: dict[str, Any], config: Any) -> dict[str, Any]:
    """Return only a few money-flow rows from the individual-stock response."""
    rows: list[dict[str, Any]] = []
    max_chars = min(int(getattr(config, "stock_max_total_chars", 5000)), 900)
    used = 0
    for label, values in _mcp_table_pairs(payload):
        if any(token in label for token in ("市现率", "PCF", "现金净流量")):
            continue
        if not any(token in label for token in ("资金流入", "资金净流", "主力", "超大单", "大单")):
            continue
        compact_values = [_repair_text(value)[: int(getattr(config, "stock_max_value_chars", 80))] for value in values[:4]]
        entry = {"metric": label[:80], "values": compact_values}
        size = len(json.dumps(entry, ensure_ascii=False))
        if used + size > max_chars:
            break
        rows.append(entry)
        used += size
        if len(rows) >= 6:
            break
    return {"status": "valid" if rows else "unavailable", "rows": rows, "truncated": bool(rows and used >= max_chars)}


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
            {"query": f"查询{symbol}当前行情、换手率、成交额、主力资金净流入、超大单/大单净流入、估值、所属行业和风险指标"},
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
            data = (
                _get_json(
                    EASTMONEY_QUOTE,
                    {"secid": secid, "fields": "f57,f58,f43,f60,f170"},
                ).get("data")
                or {}
            )
            return {
                "name": name,
                "symbol": str(data.get("f57") or secid.split(".")[-1]),
                "current_price": _float(data.get("f43")) / 100.0,
                "previous_price": _float(data.get("f60")) / 100.0,
                "change_pct": _float(data.get("f170")) / 100.0,
            }
        except Exception as exc:  # noqa: BLE001 - index failure must not block the review page
            return {
                "name": name,
                "symbol": secid.split(".")[-1],
                "quote_error": str(exc),
            }

    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        result = list(executor.map(lambda target: fetch(*target), targets))
    with _MARKET_INDEX_CACHE_LOCK:
        _MARKET_INDEX_CACHE = (now, result)
    return result


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
            "initialized": bool(state.get("initialized", False)),
        }

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

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
            if parsed.path == "/api/pools":
                state = self._read_state()
                refresh = query.get("refresh", [""])[0] in {"1", "true"}
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
                self._json(
                    {
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
                        "as_of": _now_iso(),
                    }
                )
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
                if action == "buy":
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
