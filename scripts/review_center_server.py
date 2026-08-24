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
import json
import os
import re
import shutil
import subprocess
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
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
STATE_FILE = ".review_center_state.json"
REVIEW_INITIAL_EQUITY = 100000.0
KLINE_HISTORY_DAYS = 900
QUOTE_CACHE_TTL_SECONDS = 90.0
_DIRECT_OPENER = build_opener(ProxyHandler({}))
_STOCK_KLINE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_STOCK_KLINE_CACHE_LOCK = RLock()
_MARKET_INDEX_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_MARKET_INDEX_CACHE_LOCK = RLock()


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


def _pool_signals(state: dict[str, Any], quotes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a strategy-neutral signal contract for the first UI iteration."""
    signals: list[dict[str, Any]] = []
    for item in quotes:
        symbol = str(item.get("symbol", ""))
        if not symbol or item.get("quote_error"):
            continue
        holding = any(str(pos.get("symbol")) == symbol for pos in state["positions"])
        signal = {
            "symbol": symbol,
            "name": item.get("name", symbol),
            "pool": "持仓" if holding else "观察",
            "action": "观望",
            "suggested_price": _float(item.get("current_price")),
            "reason": "待接入策略分析",
            "updated_at": item.get("updated_at") or _now_iso(),
        }
        # 观察票只有在策略给出明确的买卖判断时才展示；持仓则始终保留，
        # 方便后续直接对持仓输出观望、减仓或卖出建议。
        if holding or signal["action"] in {"买入", "卖出"}:
            signals.append(signal)
    return signals


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
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
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
                "fields": "f57,f58,f43,f47,f48,f60,f170",
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
        (last["value"] / previous_volume - 1.0) * 100.0
        if previous_volume
        else 0.0
    )
    code = _code(symbol)
    name = _repair_text(quote.get("name") or data.get("name") or code, code)
    if name == code or name.isdigit():
        try:
            matches = stock_search(code)
            name = next((item["name"] for item in matches if item["symbol"] == code), name)
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
                {"time": row["time"], "value": row["value"], "up": row["close"] >= row["open"]}
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
        quote = _get_json(
            EASTMONEY_QUOTE,
            {
                "secid": _secid(code),
                "fields": "f57,f58,f43,f47,f48,f60,f170",
            },
        ).get("data") or {}
    except Exception:  # noqa: BLE001 - daily K line remains available without quote
        quote = {}
    name = _repair_text(quote.get("f58") or code, code)
    if name == code or name.isdigit():
        try:
            matches = stock_search(code)
            name = next((item["name"] for item in matches if item["symbol"] == code), name)
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
            else ((last["close"] / previous_price - 1.0) * 100.0 if previous_price else 0.0)
        ),
        "volume": last["value"],
        "volume_change_pct": ((last["value"] / previous_volume - 1.0) * 100.0 if previous_volume else 0.0),
        "updated_at": last["time"],
        "series": {
            "symbol": code,
            "candles": [
                {"time": row["time"], "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]}
                for row in rows
            ],
            "volume": [
                {"time": row["time"], "value": row["value"], "up": row["close"] >= row["open"]}
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
            return {
                "name": name,
                "symbol": str(data.get("f57") or secid.split(".")[-1]),
                "current_price": _float(data.get("f43")) / 100.0,
                "previous_price": _float(data.get("f60")) / 100.0,
                "change_pct": _float(data.get("f170")) / 100.0,
            }
        except Exception as exc:  # noqa: BLE001 - index failure must not block the review page
            return {"name": name, "symbol": secid.split(".")[-1], "quote_error": str(exc)}

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
            return {"watchlist": [], "positions": [], "manual_trades": [], "initialized": False}
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
            for key, value in quote.items():
                if key == "series":
                    continue
                if key == "name" and value == quote.get("symbol") and item.get("name"):
                    continue
                merged[key] = value
            if not item.get("self_price"):
                merged["self_price"] = quote.get("current_price", 0.0)
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
                with ThreadPoolExecutor(max_workers=max(1, min(8, len(sources)))) as executor:
                    rendered = list(
                        executor.map(
                            lambda item: self._pool_item(item, refresh=refresh),
                            sources,
                        )
                    )
                watchlist = rendered[: len(state["watchlist"])]
                positions = rendered[len(state["watchlist"]) :]
                migrated = False
                for source, rendered in zip(state["watchlist"], watchlist):
                    if rendered.get("name") and rendered["name"] != rendered.get("symbol") and source.get("name") != rendered["name"]:
                        source["name"] = rendered["name"]
                        migrated = True
                    if not source.get("self_price") and not rendered.get("quote_error"):
                        source["self_price"] = rendered.get("self_price")
                        migrated = True
                if migrated:
                    self._write_state(state)
                self._json(
                    {
                        "watchlist": watchlist,
                        "positions": positions,
                        "indices": market_indices(refresh=refresh),
                        "manual_trades": state["manual_trades"],
                        "signals": _pool_signals(state, watchlist + positions),
                        "initialized": state["initialized"],
                        "initial_equity": REVIEW_INITIAL_EQUITY,
                        "as_of": _now_iso(),
                    }
                )
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
                        "entry_price": _float(payload.get("cost"), quote["current_price"]),
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
                    self._json({"error": "模拟交易需要 buy/sell、正数价格和正数数量"}, status=400)
                    return
                quote = stock_kline(symbol)
                position = next((item for item in state["positions"] if item.get("symbol") == symbol), None)
                realized_pnl = 0.0
                if action == "buy":
                    if position is None:
                        position = {"symbol": symbol, "name": quote["name"], "quantity": quantity, "entry_price": price}
                        state["positions"].append(position)
                    else:
                        old_quantity = _float(position.get("quantity"))
                        old_cost = _float(position.get("entry_price"))
                        position["quantity"] = old_quantity + quantity
                        position["entry_price"] = ((old_quantity * old_cost) + (quantity * price)) / position["quantity"]
                else:
                    if position is None or _float(position.get("quantity")) < quantity:
                        self._json({"error": "模拟卖出数量超过当前持仓"}, status=400)
                        return
                    realized_pnl = (price - _float(position.get("entry_price"))) * quantity
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
            state["watchlist"] = [item for item in state["watchlist"] if item.get("symbol") != symbol]
        elif parsed.path == "/api/positions":
            state["positions"] = [item for item in state["positions"] if item.get("symbol") != symbol]
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
    args = parser.parse_args()
    handler = lambda *handler_args, **handler_kwargs: ReviewCenterHandler(  # noqa: E731
        *handler_args, directory=str(Path(args.root).resolve()), **handler_kwargs
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"AKQuant review center: http://{args.host}:{args.port}/akquant_review_center.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
