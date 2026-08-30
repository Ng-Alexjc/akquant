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
                CREATE TABLE IF NOT EXISTS research_runs (
                    run_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    params_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_research_runs_time
                    ON research_runs(started_at DESC);
                CREATE TABLE IF NOT EXISTS model_registry (
                    model_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version TEXT NOT NULL,
                    metrics_json TEXT,
                    artifact_path TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    approved_by TEXT,
                    published_at TEXT,
                    parent_model_id TEXT,
                    version_snapshot_json TEXT,
                    release_notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_model_registry_role_time
                    ON model_registry(role, created_at DESC);
                CREATE TABLE IF NOT EXISTS model_release_requests (
                    request_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    requested_by TEXT,
                    request_note TEXT,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_note TEXT,
                    gate_json TEXT NOT NULL,
                    force_override INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_model_release_requests_time
                    ON model_release_requests(requested_at DESC);
                CREATE TABLE IF NOT EXISTS model_releases (
                    release_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    previous_model_id TEXT,
                    request_id TEXT,
                    actor TEXT,
                    note TEXT,
                    gate_json TEXT,
                    snapshot_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_releases_time
                    ON model_releases(created_at DESC);
                """
            )
            # Existing local databases predate the release workflow.  SQLite's
            # additive migration keeps them usable without destructive rebuilds.
            existing = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(model_registry)").fetchall()
            }
            for name, definition in {
                "approved_at": "TEXT",
                "approved_by": "TEXT",
                "published_at": "TEXT",
                "parent_model_id": "TEXT",
                "version_snapshot_json": "TEXT",
                "release_notes": "TEXT",
            }.items():
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE model_registry ADD COLUMN {name} {definition}"
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

    def create_research_run(self, action: str, params: dict[str, Any]) -> str:
        """Create an auditable local research run record."""
        digest = hashlib.sha256(
            f"{action}|{datetime.now(timezone.utc).isoformat()}".encode("utf-8")
        ).hexdigest()[:16]
        run_id = f"research-{digest}"
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO research_runs
                (run_id,action,status,started_at,params_json)
                VALUES(?,?,?,?,?)""",
                (
                    run_id,
                    str(action),
                    "running",
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(params, ensure_ascii=False, default=str),
                ),
            )
        return run_id

    def finish_research_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Finish a research run without changing model weights."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE research_runs SET status=?,finished_at=?,result_json=?,error=?
                WHERE run_id=?""",
                (
                    str(status),
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(result, ensure_ascii=False, default=str)
                    if result is not None
                    else None,
                    error,
                    run_id,
                ),
            )

    def list_research_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent research runs for the UI."""
        limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT run_id,action,status,started_at,finished_at,params_json,
                result_json,error FROM research_runs ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for key in ("params_json", "result_json"):
                raw = item.pop(key)
                target = "params" if key == "params_json" else "result"
                try:
                    item[target] = json.loads(raw) if raw else None
                except (TypeError, json.JSONDecodeError):
                    item[target] = None
            items.append(item)
        return items

    def register_model(
        self,
        model_id: str,
        *,
        model_name: str,
        role: str,
        status: str,
        version: str,
        metrics: dict[str, Any] | None = None,
        artifact_path: str | None = None,
    ) -> None:
        """Register a champion/challenger without changing live weights."""
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO model_registry
                (model_id,model_name,role,status,version,metrics_json,artifact_path,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(model_id) DO UPDATE SET
                    model_name=excluded.model_name,
                    role=CASE WHEN model_registry.status='active_paper' THEN model_registry.role ELSE excluded.role END,
                    status=CASE WHEN model_registry.status IN ('active_paper','release_requested','approved') THEN model_registry.status ELSE excluded.status END,
                    version=excluded.version,
                    metrics_json=excluded.metrics_json,
                    artifact_path=excluded.artifact_path""",
                (
                    model_id,
                    model_name,
                    role,
                    status,
                    version,
                    json.dumps(metrics or {}, ensure_ascii=False, default=str),
                    artifact_path,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def list_models(self, role: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_registry WHERE (? IS NULL OR role=?) ORDER BY created_at DESC",
                (role, role),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["metrics"] = {}
                item.pop("metrics_json", None)
            try:
                item["version_snapshot"] = json.loads(
                    item.pop("version_snapshot_json") or "null"
                )
            except (TypeError, json.JSONDecodeError):
                item["version_snapshot"] = None
                item.pop("version_snapshot_json", None)
            result.append(item)
        return result

    def get_model(self, model_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.list_models() if item["model_id"] == model_id),
            None,
        )

    def active_champion(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT model_id FROM model_registry
                WHERE role='champion' AND status='active_paper'
                ORDER BY published_at DESC, created_at DESC LIMIT 1"""
            ).fetchone()
        return self.get_model(str(row["model_id"])) if row else None

    @staticmethod
    def _metric_value(metrics: dict[str, Any], *paths: tuple[str, ...]) -> float | None:
        for path in paths:
            value: Any = metrics
            for key in path:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return None

    def evaluate_release_gate(self, model_id: str) -> dict[str, Any]:
        """Evaluate deterministic publication thresholds for a challenger."""
        candidate = self.get_model(model_id)
        if candidate is None:
            raise ValueError(f"模型不存在: {model_id}")
        champion = self.active_champion()
        metrics = dict(candidate.get("metrics") or {})
        champion_metrics = dict((champion or {}).get("metrics") or {})
        candidate_sharpe = self._metric_value(metrics, ("sharpe_ratio",), ("best", "sharpe_ratio"))
        champion_sharpe = self._metric_value(champion_metrics, ("sharpe_ratio",), ("best", "sharpe_ratio"))
        candidate_drawdown = self._metric_value(metrics, ("max_drawdown_pct",), ("best", "max_drawdown_pct"))
        champion_drawdown = self._metric_value(champion_metrics, ("max_drawdown_pct",), ("best", "max_drawdown_pct"))
        completed_trials = self._metric_value(metrics, ("completed_trial_count",), ("optimization", "completed_trial_count")) or 0.0
        trade_count = self._metric_value(metrics, ("trade_count",), ("best", "trade_count")) or 0.0
        cpcv_status = str(metrics.get("cpcv_status") or (metrics.get("cpcv") or {}).get("status") or "unavailable")
        mean_test_sharpe = self._metric_value(metrics, ("mean_test_sharpe",), ("cpcv", "summary", "mean_test_sharpe"))
        positive_fold_ratio = self._metric_value(metrics, ("positive_test_fold_ratio",), ("cpcv", "summary", "positive_test_fold_ratio"))
        pbo = self._metric_value(metrics, ("pbo",), ("robustness", "pbo"))
        checks = [
            {"name": "challenger_role", "passed": candidate.get("role") == "challenger", "actual": candidate.get("role"), "required": "challenger"},
            {"name": "evaluated_status", "passed": candidate.get("status") in {"evaluated", "release_requested", "approved"}, "actual": candidate.get("status"), "required": "evaluated/release_requested/approved"},
            {"name": "optuna_trials", "passed": completed_trials >= 10, "actual": completed_trials, "required": ">=10"},
            {"name": "minimum_trades", "passed": trade_count >= 3, "actual": trade_count, "required": ">=3"},
            {"name": "cpcv_valid", "passed": cpcv_status == "valid", "actual": cpcv_status, "required": "valid"},
            {"name": "cpcv_mean_sharpe", "passed": mean_test_sharpe is not None and mean_test_sharpe >= 0.0, "actual": mean_test_sharpe, "required": ">=0"},
            {"name": "positive_fold_ratio", "passed": positive_fold_ratio is not None and positive_fold_ratio >= 0.5, "actual": positive_fold_ratio, "required": ">=0.5"},
            {"name": "pbo", "passed": pbo is not None and pbo <= 0.5, "actual": pbo, "required": "<=0.5"},
            {"name": "champion_sharpe", "passed": champion_sharpe is None or (candidate_sharpe is not None and candidate_sharpe >= champion_sharpe - 0.10), "actual": candidate_sharpe, "required": f">={champion_sharpe - 0.10:.4f}" if champion_sharpe is not None else "baseline unavailable"},
            {"name": "champion_drawdown", "passed": champion_drawdown is None or (candidate_drawdown is not None and candidate_drawdown >= champion_drawdown - 3.0), "actual": candidate_drawdown, "required": f">={champion_drawdown - 3.0:.4f}" if champion_drawdown is not None else "baseline unavailable"},
        ]
        return {"model_id": model_id, "champion_model_id": (champion or {}).get("model_id"), "passed": all(bool(item["passed"]) for item in checks), "checks": checks, "evaluated_at": datetime.now(timezone.utc).isoformat()}

    def request_model_release(self, model_id: str, *, requested_by: str = "local_user", note: str = "") -> dict[str, Any]:
        candidate = self.get_model(model_id)
        if candidate is None:
            raise ValueError(f"模型不存在: {model_id}")
        if candidate.get("role") != "challenger":
            raise ValueError("只有 Challenger 可以发起发布申请")
        if candidate.get("status") not in {"evaluated", "release_requested"}:
            raise ValueError(f"模型当前状态不能申请发布: {candidate.get('status')}")
        with self._connect() as connection:
            pending = connection.execute(
                "SELECT request_id FROM model_release_requests WHERE model_id=? AND status='pending' LIMIT 1",
                (model_id,),
            ).fetchone()
        if pending is not None:
            raise ValueError(f"该模型已有待审批申请: {pending['request_id']}")
        gate = self.evaluate_release_gate(model_id)
        request_id = "release-request-" + hashlib.sha256(f"{model_id}|{datetime.now(timezone.utc).isoformat()}".encode("utf-8")).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("UPDATE model_registry SET status='release_requested' WHERE model_id=?", (model_id,))
            connection.execute(
                """INSERT INTO model_release_requests
                (request_id,model_id,status,requested_at,requested_by,request_note,gate_json)
                VALUES (?,?,?,?,?,?,?)""",
                (request_id, model_id, "pending", now, requested_by, note, json.dumps(gate, ensure_ascii=False)),
            )
        return {"request_id": request_id, "status": "pending", "gate": gate}

    def list_release_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM model_release_requests ORDER BY requested_at DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["gate"] = json.loads(item.pop("gate_json") or "{}")
            item["force_override"] = bool(item.get("force_override"))
            items.append(item)
        return items

    def approve_model_release(
        self,
        request_id: str,
        *,
        approved_by: str = "local_user",
        note: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            request = connection.execute("SELECT * FROM model_release_requests WHERE request_id=?", (request_id,)).fetchone()
        if request is None:
            raise ValueError(f"发布申请不存在: {request_id}")
        if request["status"] != "pending":
            raise ValueError(f"发布申请不是待审批状态: {request['status']}")
        model_id = str(request["model_id"])
        candidate = self.get_model(model_id)
        if candidate is None or candidate.get("role") != "challenger":
            raise ValueError("发布对象已不再是可发布的 Challenger")
        gate = self.evaluate_release_gate(model_id)
        if not gate["passed"] and not force:
            raise ValueError("发布门槛未通过，不能批准")
        if force and not note.strip():
            raise ValueError("强制发布必须填写审批说明")
        now = datetime.now(timezone.utc).isoformat()
        release_id = "release-" + hashlib.sha256(f"publish|{model_id}|{now}".encode("utf-8")).hexdigest()[:16]
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM model_registry ORDER BY created_at").fetchall()
            snapshot = [dict(row) for row in rows]
            previous = connection.execute(
                """SELECT model_id FROM model_registry
                WHERE role='champion' AND status='active_paper'
                ORDER BY published_at DESC,created_at DESC LIMIT 1"""
            ).fetchone()
            previous_id = str(previous["model_id"]) if previous and previous["model_id"] != model_id else None
            if previous_id:
                connection.execute("UPDATE model_registry SET role='archived',status='superseded' WHERE model_id=?", (previous_id,))
            connection.execute(
                """UPDATE model_registry SET role='champion',status='active_paper',
                approved_at=?,approved_by=?,published_at=?,parent_model_id=?,
                version_snapshot_json=?,release_notes=? WHERE model_id=?""",
                (now, approved_by, now, previous_id, json.dumps(snapshot, ensure_ascii=False, default=str), note, model_id),
            )
            connection.execute(
                """UPDATE model_release_requests SET status='approved',decided_at=?,
                decided_by=?,decision_note=?,gate_json=?,force_override=? WHERE request_id=?""",
                (now, approved_by, note, json.dumps(gate, ensure_ascii=False), int(force), request_id),
            )
            connection.execute(
                """INSERT INTO model_releases
                (release_id,action,model_id,previous_model_id,request_id,actor,note,gate_json,snapshot_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (release_id, "publish", model_id, previous_id, request_id, approved_by, note, json.dumps(gate, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False, default=str), now),
            )
        return {"release_id": release_id, "model_id": model_id, "previous_model_id": previous_id, "status": "published", "gate": gate, "force_override": force}

    def reject_model_release(self, request_id: str, *, decided_by: str = "local_user", note: str = "") -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            request = connection.execute("SELECT model_id,status FROM model_release_requests WHERE request_id=?", (request_id,)).fetchone()
            if request is None or request["status"] != "pending":
                raise ValueError("发布申请不存在或已处理")
            connection.execute(
                """UPDATE model_release_requests SET status='rejected',decided_at=?,
                decided_by=?,decision_note=? WHERE request_id=?""",
                (now, decided_by, note, request_id),
            )
            connection.execute("UPDATE model_registry SET status='evaluated' WHERE model_id=?", (request["model_id"],))
        return {"request_id": request_id, "status": "rejected"}

    def rollback_model_release(
        self,
        *,
        target_model_id: str | None = None,
        actor: str = "local_user",
        note: str = "",
    ) -> dict[str, Any]:
        current = self.active_champion()
        if current is None:
            raise ValueError("当前没有已发布 Champion")
        if target_model_id is None:
            target_model_id = str(current.get("parent_model_id") or "")
        target = self.get_model(target_model_id) if target_model_id else None
        if target is None:
            raise ValueError("没有可回滚的目标版本")
        if target_model_id == current["model_id"]:
            raise ValueError("目标版本已经是当前 Champion")
        if target.get("role") != "archived" or target.get("status") not in {
            "superseded",
            "rolled_back",
        }:
            raise ValueError("回滚目标必须是曾经发布过的历史 Champion")
        now = datetime.now(timezone.utc).isoformat()
        release_id = "release-" + hashlib.sha256(f"rollback|{target_model_id}|{now}".encode("utf-8")).hexdigest()[:16]
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM model_registry ORDER BY created_at").fetchall()
            snapshot = [dict(row) for row in rows]
            connection.execute("UPDATE model_registry SET role='archived',status='rolled_back' WHERE model_id=?", (current["model_id"],))
            connection.execute(
                """UPDATE model_registry SET role='champion',status='active_paper',
                published_at=?,parent_model_id=?,release_notes=? WHERE model_id=?""",
                (
                    now,
                    current["model_id"],
                    note or f"rollback from {current['model_id']}",
                    target_model_id,
                ),
            )
            connection.execute(
                """INSERT INTO model_releases
                (release_id,action,model_id,previous_model_id,actor,note,snapshot_json,created_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (release_id, "rollback", target_model_id, current["model_id"], actor, note, json.dumps(snapshot, ensure_ascii=False, default=str), now),
            )
        return {"release_id": release_id, "status": "rolled_back", "model_id": target_model_id, "previous_model_id": current["model_id"]}

    def list_model_releases(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM model_releases ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            for source, target in (("gate_json", "gate"), ("snapshot_json", "snapshot")):
                raw = item.pop(source)
                item[target] = json.loads(raw) if raw else None
            items.append(item)
        return items

    def train_candidate_models(self, minimum_samples: int = 30) -> dict[str, Any]:
        """Evaluate lightweight challenger models on labeled AI observations.

        This method deliberately produces candidate metrics and an artifact only;
        it never changes the active model or fusion weights.
        """
        try:
            from sklearn.linear_model import LogisticRegression
            from sklearn.metrics import (
                accuracy_score,
                brier_score_loss,
                log_loss,
                precision_score,
                roc_auc_score,
            )
            from sklearn.ensemble import HistGradientBoostingClassifier
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler
        except Exception as exc:  # pragma: no cover - optional dependency
            return {"status": "unavailable", "reason": str(exc)}

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT a.as_of,a.compact_json,o.next_day_label,o.day5_label
                FROM analyses a JOIN outcome_labels o ON o.analysis_id=a.analysis_id
                WHERE o.next_day_label IS NOT NULL OR o.day5_label IS NOT NULL
                ORDER BY a.as_of"""
            ).fetchall()

        feature_names = [
            "traditional_score",
            "traditional_next_day_probability",
            "traditional_five_day_probability",
            "next_day_probability_missing",
            "five_day_probability_missing",
            "fusion_score",
            "data_quality",
            "meta_signal",
        ]

        def build_rows(label_key: str) -> tuple[list[list[float]], list[int]]:
            features: list[list[float]] = []
            labels: list[int] = []
            for row in rows:
                label = row[label_key]
                if label is None:
                    continue
                try:
                    result = json.loads(row["compact_json"])
                    traditional = result.get("traditional") or {}
                    fusion = result.get("fusion") or {}
                    probs = traditional.get("probabilities") or {}
                    next_prob = (probs.get("next_trading_day") or {}).get("value")
                    five_prob = (probs.get("next_5_trading_days") or {}).get("value")
                    quality = (result.get("data_quality") or {}).get("score", 0)
                    next_missing = 1.0 if next_prob is None else 0.0
                    five_missing = 1.0 if five_prob is None else 0.0
                    features.append(
                        [
                            float(traditional.get("selection_score") or 0),
                            float(next_prob) if next_prob is not None else 0.5,
                            float(five_prob) if five_prob is not None else 0.5,
                            next_missing,
                            five_missing,
                            float(fusion.get("final_score") or 0),
                            float(quality or 0) / 100.0,
                            1.0 if float(traditional.get("selection_score") or 0) >= 60.0 else 0.0,
                        ]
                    )
                    labels.append(int(label))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            return features, labels

        output: dict[str, Any] = {"status": "insufficient_data", "models": {}}
        for horizon, label_key in (
            ("next_trading_day", "next_day_label"),
            ("next_5_trading_days", "day5_label"),
        ):
            features, labels = build_rows(label_key)
            if len(labels) < minimum_samples or len(set(labels)) < 2:
                output["models"][horizon] = {
                    "status": "insufficient_data",
                    "sample_count": len(labels),
                }
                continue
            split = max(1, int(len(labels) * 0.8))
            if split >= len(labels):
                split = len(labels) - 1
            if len(set(labels[:split])) < 2:
                output["models"][horizon] = {
                    "status": "insufficient_data",
                    "sample_count": len(labels),
                    "reason": "训练窗口只包含单一类别",
                }
                continue
            baseline = Pipeline(
                [
                    ("scale", StandardScaler()),
                    ("model", LogisticRegression(max_iter=500, random_state=0)),
                ]
            )
            expected = labels[split:]
            train_x, test_x = features[:split], features[split:]

            def evaluate(model: Any, model_name: str, calibration_method: str = "none") -> dict[str, Any]:
                model.fit(train_x, labels[:split])
                probabilities = model.predict_proba(test_x)[:, 1]
                predicted = (probabilities >= 0.5).astype(int)
                k = max(1, int(len(probabilities) * 0.2))
                top_k = sorted(range(len(probabilities)), key=lambda idx: probabilities[idx], reverse=True)[:k]
                precision_at_k = float(sum(expected[idx] for idx in top_k) / k) if top_k else 0.0
                bins = []
                for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
                    members = [idx for idx, prob in enumerate(probabilities) if lower <= prob < lower + 0.2 or (lower == 0.8 and prob <= 1.0)]
                    if members:
                        bins.append(abs(float(sum(probabilities[idx] for idx in members) / len(members)) - float(sum(expected[idx] for idx in members) / len(members))))
                importance: dict[str, float] = {}
                raw_model = model.named_steps.get("model") if hasattr(model, "named_steps") else model
                if hasattr(raw_model, "coef_"):
                    importance = {name: float(value) for name, value in zip(feature_names, raw_model.coef_[0])}
                elif hasattr(raw_model, "feature_importances_"):
                    importance = {name: float(value) for name, value in zip(feature_names, raw_model.feature_importances_)}
                return {
                    "status": "candidate",
                    "model_name": model_name,
                    "calibration_method": calibration_method,
                    "sample_count": len(labels),
                    "train_count": split,
                    "test_count": len(expected),
                    "accuracy": float(accuracy_score(expected, predicted)),
                    "brier_score": float(brier_score_loss(expected, probabilities)),
                    "log_loss": float(log_loss(expected, probabilities, labels=[0, 1])),
                    "auc": float(roc_auc_score(expected, probabilities)) if len(set(expected)) > 1 else None,
                    "precision_at_k": precision_at_k,
                    "precision": float(precision_score(expected, predicted, zero_division=0)),
                    "calibration_error": float(sum(bins) / len(bins)) if bins else None,
                    "feature_names": feature_names,
                    "feature_importance": importance,
                }

            models: dict[str, Any] = {}
            models["logistic"] = evaluate(baseline, "logistic")
            try:
                gb = HistGradientBoostingClassifier(max_iter=120, learning_rate=0.05, max_leaf_nodes=15, random_state=0)
                models["gradient_boosting"] = evaluate(gb, "hist_gradient_boosting")
                try:
                    calibrated = CalibratedClassifierCV(baseline, method="sigmoid", cv=3)
                    models["logistic_calibrated"] = evaluate(calibrated, "logistic", "platt_sigmoid")
                except Exception as calibration_exc:
                    models["logistic_calibrated"] = {"status": "unavailable", "reason": str(calibration_exc)}
            except Exception as model_exc:
                models["gradient_boosting"] = {"status": "unavailable", "reason": str(model_exc)}
            output["models"][horizon] = {
                "status": "candidate",
                "sample_count": len(labels),
                "train_count": split,
                "test_count": len(expected),
                "models": models,
                "meta_labeling": {"status": "enabled", "rule": "selection_score >= 60 as base signal"},
                "baseline_vs_challenger": {
                    "baseline": models.get("logistic", {}).get("brier_score"),
                    "challenger": models.get("gradient_boosting", {}).get("brier_score"),
                },
            }
            output["status"] = "candidate_ready"
        output["release_policy"] = "仅评估 challenger，不自动替换线上模型。"
        return output

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
