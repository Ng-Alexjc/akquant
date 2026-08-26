"""SQLite audit, feedback and retention storage for daily AI observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class AnalysisStorage:
    def __init__(self, database_path: Path, raw_directory: Path) -> None:
        self.database_path = database_path
        self.raw_directory = raw_directory
        database_path.parent.mkdir(parents=True, exist_ok=True)
        raw_directory.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analyses (
                    analysis_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    final_action TEXT,
                    next_day_probability REAL,
                    next_five_probability REAL,
                    score REAL,
                    compact_json TEXT NOT NULL,
                    prompt_hash TEXT,
                    knowledge_hash TEXT,
                    provider_model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cached_tokens INTEGER,
                    raw_path TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_analyses_symbol_time ON analyses(symbol, as_of DESC);
                CREATE TABLE IF NOT EXISTS outcome_labels (
                    analysis_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    base_close REAL NOT NULL,
                    next_day_close REAL,
                    day5_close REAL,
                    next_day_label INTEGER,
                    day5_label INTEGER,
                    mae5 REAL,
                    mfe5 REAL,
                    max_drawdown5 REAL,
                    stop_triggered INTEGER,
                    labeled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_id TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    value TEXT,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    def save(
        self,
        result: dict[str, Any],
        *,
        raw_request: str = "",
        raw_response: str = "",
        usage: dict[str, Any] | None = None,
        oversized_token_threshold: int | None = None,
    ) -> None:
        usage = usage or {}
        analysis_id = str(result["analysis_id"])
        total_tokens = int(usage.get("input_tokens") or 0) + int(
            usage.get("output_tokens") or 0
        )
        oversized = (
            oversized_token_threshold is not None
            and total_tokens >= oversized_token_threshold
        )
        raw_path: str | None = None
        if raw_request or raw_response:
            raw_path = str(self.raw_directory / f"{analysis_id}.json")
            Path(raw_path).write_text(
                json.dumps(
                    {
                        "request": raw_request,
                        "response": raw_response,
                        "oversized": oversized,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        fusion = result.get("fusion") or {}
        probabilities = fusion.get("final_up_probabilities") or {}
        instrument = result.get("instrument") or {}
        audit = result.get("audit") or {}
        compact = json.dumps(
            result, ensure_ascii=False, separators=(",", ":"), default=str
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO analyses
                (analysis_id,symbol,as_of,created_at,status,final_action,next_day_probability,
                 next_five_probability,score,compact_json,prompt_hash,knowledge_hash,provider_model,
                 input_tokens,output_tokens,cached_tokens,raw_path)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    analysis_id,
                    str(instrument.get("symbol") or ""),
                    str(result.get("as_of") or ""),
                    datetime.now(timezone.utc).isoformat(),
                    str(fusion.get("assessment_status") or "unavailable"),
                    fusion.get("final_action"),
                    (probabilities.get("next_trading_day") or {}).get("value"),
                    (probabilities.get("next_5_trading_days") or {}).get("value"),
                    fusion.get("final_score"),
                    compact,
                    audit.get("prompt_sha256"),
                    audit.get("knowledge_sha256"),
                    audit.get("provider_model"),
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    usage.get("cached_tokens"),
                    raw_path,
                ),
            )
            traditional = result.get("traditional") or {}
            base_close = float(
                traditional.get("close") or instrument.get("current_price") or 0
            )
            if base_close > 0:
                connection.execute(
                    "INSERT OR IGNORE INTO outcome_labels(analysis_id,symbol,base_close) VALUES(?,?,?)",
                    (analysis_id, str(instrument.get("symbol") or ""), base_close),
                )

    def latest(self, symbol: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT compact_json FROM analyses WHERE symbol=? ORDER BY as_of DESC, created_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return json.loads(row["compact_json"]) if row else None

    def get(self, analysis_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT compact_json,raw_path FROM analyses WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
        if not row:
            return None
        result = json.loads(row["compact_json"])
        result["_raw_path"] = row["raw_path"]
        return result

    def list_analyses(self, *, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self._connect() as connection:
            if symbol:
                rows = connection.execute(
                    "SELECT compact_json FROM analyses WHERE symbol=? ORDER BY as_of DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT compact_json FROM analyses ORDER BY as_of DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [json.loads(row["compact_json"]) for row in rows]

    def training_dataset_summary(self) -> dict[str, Any]:
        """Return data-driven training readiness without mutating live weights."""
        report = self.performance_report(minimum_samples=1)
        samples = {
            key: value.get("sample_count", 0)
            for key, value in report.items()
            if isinstance(value, dict) and "sample_count" in value
        }
        return {
            "status": "ready_for_candidate_evaluation" if max(samples.values() or [0]) >= 30 else "insufficient_data",
            "samples": samples,
            "release_gate": report.get("release_gate"),
            "policy": "只生成候选评估，不自动修改线上权重；需样本外窗口和人工确认。",
        }

    def delete(self, analysis_id: str | None) -> None:
        if not analysis_id:
            return
        with self._connect() as connection:
            row = connection.execute("SELECT raw_path FROM analyses WHERE analysis_id=?", (analysis_id,)).fetchone()
            connection.execute("DELETE FROM analyses WHERE analysis_id=?", (analysis_id,))
            connection.execute("DELETE FROM outcome_labels WHERE analysis_id=?", (analysis_id,))
            connection.execute("DELETE FROM feedback WHERE analysis_id=?", (analysis_id,))
        if row and row["raw_path"]:
            Path(row["raw_path"]).unlink(missing_ok=True)

    def record_feedback(
        self, analysis_id: str, feedback_type: str, value: Any = None, note: str = ""
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO feedback(analysis_id,feedback_type,value,note,created_at) VALUES(?,?,?,?,?)",
                (
                    analysis_id,
                    feedback_type,
                    json.dumps(value, ensure_ascii=False),
                    note,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def label_outcome(
        self,
        analysis_id: str,
        bars: list[dict[str, Any] | float],
        *,
        stop_price: float | None = None,
    ) -> None:
        if not bars:
            return
        with self._connect() as connection:
            row = connection.execute(
                "SELECT base_close FROM outcome_labels WHERE analysis_id=?",
                (analysis_id,),
            ).fetchone()
            if not row:
                return
            base = float(row["base_close"])
            normalized = []
            for value in bars[:5]:
                if isinstance(value, dict):
                    close = float(value.get("close") or 0)
                    normalized.append(
                        {
                            "close": close,
                            "high": float(value.get("high") or close),
                            "low": float(value.get("low") or close),
                        }
                    )
                else:
                    close = float(value)
                    normalized.append({"close": close, "high": close, "low": close})
            normalized = [value for value in normalized if value["close"] > 0]
            if not normalized:
                return
            closes = [value["close"] for value in normalized]
            adverse_returns = [value["low"] / base - 1 for value in normalized]
            favorable_returns = [value["high"] / base - 1 for value in normalized]
            peak = base
            max_drawdown = 0.0
            for value in normalized:
                peak = max(peak, value["high"])
                max_drawdown = min(max_drawdown, value["low"] / peak - 1)
            connection.execute(
                """UPDATE outcome_labels SET next_day_close=?,day5_close=?,next_day_label=?,day5_label=?,
                mae5=?,mfe5=?,max_drawdown5=?,stop_triggered=?,labeled_at=? WHERE analysis_id=?""",
                (
                    closes[0],
                    closes[4] if len(closes) >= 5 else None,
                    int(closes[0] > base),
                    int(closes[4] > base) if len(closes) >= 5 else None,
                    min(adverse_returns),
                    max(favorable_returns),
                    max_drawdown,
                    int(
                        bool(
                            stop_price
                            and min(value["low"] for value in normalized) <= stop_price
                        )
                    ),
                    datetime.now(timezone.utc).isoformat(),
                    analysis_id,
                ),
            )

    def pending_labels(self, symbol: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.analysis_id,a.as_of,a.compact_json FROM analyses a
                JOIN outcome_labels o ON o.analysis_id=a.analysis_id
                WHERE a.symbol=? AND o.day5_label IS NULL ORDER BY a.as_of""",
                (symbol,),
            ).fetchall()
        return [
            {
                "analysis_id": row["analysis_id"],
                "as_of": row["as_of"],
                "result": json.loads(row["compact_json"]),
            }
            for row in rows
        ]

    def performance_report(self, minimum_samples: int = 1) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.next_day_probability,a.next_five_probability,
                o.next_day_label,o.day5_label FROM analyses a
                JOIN outcome_labels o ON o.analysis_id=a.analysis_id"""
            ).fetchall()

        def metrics(probability_key: str, label_key: str) -> dict[str, Any]:
            pairs = [
                (float(row[probability_key]), int(row[label_key]))
                for row in rows
                if row[probability_key] is not None and row[label_key] is not None
            ]
            if len(pairs) < minimum_samples:
                return {"sample_count": len(pairs), "status": "insufficient_data"}
            labels = [value[1] for value in pairs]
            probabilities = [value[0] for value in pairs]
            predicted = [int(value >= 0.5) for value in probabilities]
            from sklearn.metrics import (
                brier_score_loss,
                precision_score,
                recall_score,
                roc_auc_score,
            )

            return {
                "sample_count": len(pairs),
                "status": "valid",
                "brier_score": float(brier_score_loss(labels, probabilities)),
                "auc": float(roc_auc_score(labels, probabilities))
                if len(set(labels)) > 1
                else None,
                "precision": float(precision_score(labels, predicted, zero_division=0)),
                "recall": float(recall_score(labels, predicted, zero_division=0)),
                "calibration_error": abs(
                    sum(probabilities) / len(pairs) - sum(labels) / len(pairs)
                ),
            }

        return {
            "next_trading_day": metrics("next_day_probability", "next_day_label"),
            "next_5_trading_days": metrics("next_five_probability", "day5_label"),
            "release_gate": {
                "status": "pending_real_data",
                "message": "最小样本量、评价窗口和发布门槛将在首批真实数据积累后确定。",
            },
        }

    def retention_cleanup(self, *, normal_days: int, oversized_days: int) -> int:
        normal_cutoff = datetime.now(timezone.utc) - timedelta(days=normal_days)
        oversized_cutoff = datetime.now(timezone.utc) - timedelta(days=oversized_days)
        removed = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT analysis_id,created_at,raw_path FROM analyses WHERE raw_path IS NOT NULL"
            ).fetchall()
            for row in rows:
                path = Path(row["raw_path"])
                oversized = False
                try:
                    oversized = bool(
                        json.loads(path.read_text(encoding="utf-8")).get("oversized")
                    )
                except (OSError, json.JSONDecodeError):
                    pass
                cutoff = oversized_cutoff if oversized else normal_cutoff
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except ValueError:
                    created = normal_cutoff - timedelta(days=1)
                if created < cutoff:
                    path.unlink(missing_ok=True)
                    connection.execute(
                        "UPDATE analyses SET raw_path=NULL WHERE analysis_id=?",
                        (row["analysis_id"],),
                    )
                    removed += 1
        return removed

    @staticmethod
    def content_id(symbol: str, as_of: str, payload: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
        ).hexdigest()[:12]
        return f"ai-{symbol}-{as_of[:10]}-{digest}"
