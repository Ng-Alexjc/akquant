from __future__ import annotations

from urllib.error import URLError

from scripts import run_daily_auto


def test_wait_for_run_returns_matching_completed_run(monkeypatch) -> None:
    responses = iter(
        [
            {"items": [{"run_id": "other", "status": "completed"}]},
            {"items": [{"run_id": "research-1", "status": "completed"}]},
        ]
    )
    monkeypatch.setattr(run_daily_auto, "_request_json", lambda request: next(responses))
    monkeypatch.setattr(run_daily_auto.time, "sleep", lambda seconds: None)

    result = run_daily_auto._wait_for_run("research-1")

    assert result["status"] == "completed"


def test_main_never_falls_back_after_server_created_run(monkeypatch) -> None:
    monkeypatch.setattr(
        run_daily_auto,
        "_request_json",
        lambda request: {"run_id": "research-created", "status": "running"},
    )
    monkeypatch.setattr(
        run_daily_auto,
        "_wait_for_run",
        lambda run_id: (_ for _ in ()).throw(URLError("poll failed")),
    )
    fallback_called = False

    def fallback() -> int:
        nonlocal fallback_called
        fallback_called = True
        return 0

    monkeypatch.setattr(run_daily_auto, "_run_in_process", fallback)

    assert run_daily_auto.main() == 1
    assert fallback_called is False


def test_main_uses_trade_only_automation_endpoint(monkeypatch) -> None:
    requested_urls: list[str] = []

    def request_json(request):
        requested_urls.append(request.full_url)
        return {"run_id": "automation-created", "status": "running"}

    monkeypatch.setattr(run_daily_auto, "_request_json", request_json)
    monkeypatch.setattr(
        run_daily_auto,
        "_wait_for_run",
        lambda run_id: {"run_id": run_id, "status": "completed"},
    )

    assert run_daily_auto.main() == 0
    assert requested_urls == [
        "http://127.0.0.1:8765/api/automation/trade"
    ]
