"""Run the scheduled local-paper research and execution cycle.

This entrypoint is safe to invoke from Windows Task Scheduler/Codex
automation. It never connects to a broker; it updates only the local review
center state and audit database.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    payload = json.dumps({"action": "daily_auto", "calendar_days": 60, "refresh": True}).encode("utf-8")
    request = Request(
        "http://127.0.0.1:8765/api/research/run",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=300) as response:
            print(response.read().decode("utf-8"))
            return 0
    except (URLError, TimeoutError, OSError):
        # Fall back to an in-process execution when the static review server
        # is not running. This keeps the scheduled job self-contained.
        sys.path.insert(0, str(ROOT / "scripts"))
        import review_center_server as server

        state_path = ROOT / ".review_center_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"positions": [], "watchlist": [], "manual_trades": [], "initialized": True}
        result = server._run_research_action("full", state, calendar_days=60, refresh=True)
        snapshot_end = str((result.get("dataset") or {}).get("snapshot", {}).get("end") or "")[:10]
        today = datetime.now().date().isoformat()
        result["execution"] = (
            server._execute_local_signals(state, refresh=True, automated=True)
            if snapshot_end == today
            else {"status": "skipped", "reason": "最新行情日期不是今天，疑似周末/节假日或行情未更新", "snapshot_end": snapshot_end, "local_today": today, "applied": [], "skipped": []}
        )
        state["initialized"] = True
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
