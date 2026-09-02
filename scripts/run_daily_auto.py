"""Run the scheduled local-paper trading cycle.

This entrypoint is safe to invoke from Windows Task Scheduler/Codex
automation. It never connects to a broker; it updates only the local review
center state and automation audit database. It deliberately performs no
research, training, optimization, model publication, LLM, or Agent calls.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = "http://127.0.0.1:8765"
POLL_SECONDS = 15
MAX_WAIT_SECONDS = 2 * 60 * 60


def _request_json(request: Request, *, timeout: float = 30.0) -> dict:
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_run(run_id: str) -> dict:
    """Wait for one already-created trade run without ever duplicating it."""
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    while time.monotonic() < deadline:
        request = Request(f"{SERVER_ROOT}/api/automation/runs?limit=20", method="GET")
        payload = _request_json(request)
        run = next(
            (item for item in payload.get("items", []) if item.get("run_id") == run_id),
            None,
        )
        if run and run.get("status") in {"completed", "failed"}:
            return run
        time.sleep(POLL_SECONDS)
    raise TimeoutError(f"automated trade run timed out: {run_id}")


def _run_in_process() -> int:
    """Trade-only fallback used when the local HTTP service is unavailable."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import review_center_server as server

    state_path = ROOT / ".review_center_state.json"
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
    result = server._run_daily_trade_action(state, refresh=True)
    if result.get("status") == "completed":
        state["initialized"] = True
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 1 if result.get("status") == "failed" else 0


def main() -> int:
    payload = json.dumps(
        {
            "refresh": True,
            "background": True,
        }
    ).encode("utf-8")
    request = Request(
        f"{SERVER_ROOT}/api/automation/trade",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        created = _request_json(request)
    except (URLError, TimeoutError, OSError):
        # Fall back to an in-process execution when the static review server
        # is not running. This keeps the scheduled job self-contained.
        return _run_in_process()

    run_id = str(created.get("run_id") or "")
    if not run_id:
        print(json.dumps({"status": "failed", "error": "server omitted run_id"}))
        return 1
    try:
        completed = _wait_for_run(run_id)
    except (URLError, TimeoutError, OSError) as exc:
        # A run already exists, so never fall back and create a duplicate.
        print(
            json.dumps(
                {"status": "failed", "run_id": run_id, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(completed, ensure_ascii=False, default=str))
    return 0 if completed.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
