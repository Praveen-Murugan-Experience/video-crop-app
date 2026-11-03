"""Failure handling in the background worker: retries, cancellation, timeouts, recovery, retention."""

import sys
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import media
from app.main import create_app
from app.store import JobStore
from tests.conftest import crop_request, requires_ffmpeg, wait_for_terminal

pytestmark = requires_ffmpeg

SLOW_COMMAND = [sys.executable, "-c", "import time; time.sleep(30)"]


def _fail_then_succeed(monkeypatch, failures: list[Exception]):
    """Make run_ffmpeg raise each of `failures` in turn, then behave normally."""
    real = media.run_ffmpeg
    calls = []

    def fake(cmd, **kwargs):
        calls.append(cmd)
        if failures:
            raise failures.pop(0)
        return real(cmd, **kwargs)

    monkeypatch.setattr(media, "run_ffmpeg", fake)
    return calls


def test_transient_failure_is_retried_then_succeeds(client, monkeypatch):
    calls = _fail_then_succeed(monkeypatch, [media.FfmpegError(1, "Conversion failed: resource temporarily unavailable")])
    job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    done = wait_for_terminal(client, job["id"])
    assert done["status"] == "completed", done
    assert done["attempts"] == 2
    assert done["error"] is None
    assert len(calls) == 2


def test_permanent_failure_is_not_retried(client, monkeypatch):
    calls = _fail_then_succeed(monkeypatch, [media.FfmpegError(1, "x.mp4: Invalid data found when processing input")])
    job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    done = wait_for_terminal(client, job["id"])
    assert done["status"] == "failed"
    assert done["attempts"] == 1
    assert done["error"]["code"] == "FFMPEG_FAILED"
    assert len(calls) == 1


def test_retries_stop_after_max_attempts(client, monkeypatch):
    _fail_then_succeed(monkeypatch, [media.FfmpegError(1, "boom")] * 5)
    job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    done = wait_for_terminal(client, job["id"])
    assert done["status"] == "failed"
    assert done["attempts"] == done["maxAttempts"] == 3


def test_source_deleted_after_submit_fails_without_retry(idle_client, settings):
    job = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    (settings.input_dir / "small.mp4").unlink()
    worker = idle_client.app.state.workers
    worker.process(idle_client.app.state.store.claim_next())
    done = idle_client.get(f"/v1/crop-jobs/{job['id']}").json()
    assert done["status"] == "failed"
    assert done["error"]["code"] == "SOURCE_NOT_FOUND"


def test_cancel_in_flight_job_stops_ffmpeg(client, monkeypatch, settings):
    monkeypatch.setattr(media, "build_crop_command", lambda *a, **k: SLOW_COMMAND)
    job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    deadline = time.monotonic() + 10
    while client.get(f"/v1/crop-jobs/{job['id']}").json()["status"] != "processing":
        assert time.monotonic() < deadline
        time.sleep(0.05)

    resp = client.post(f"/v1/crop-jobs/{job['id']}/cancel")
    assert resp.status_code == 202
    assert resp.json()["cancelRequested"] is True

    started = time.monotonic()
    done = wait_for_terminal(client, job["id"], timeout_s=10)
    assert done["status"] == "cancelled"
    assert time.monotonic() - started < 5  # stopped promptly, not after the 30s sleep
    assert list(settings.output_dir.iterdir()) == []  # no partial output left behind


def test_timeout_fails_job(settings, monkeypatch):
    monkeypatch.setattr(media, "build_crop_command", lambda *a, **k: SLOW_COMMAND)
    with TestClient(create_app(replace(settings, job_timeout_s=0.5))) as client:
        job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
        done = wait_for_terminal(client, job["id"], timeout_s=10)
    assert done["status"] == "failed"
    assert done["error"]["code"] == "PROCESSING_TIMEOUT"
    assert done["attempts"] == 1


def test_job_interrupted_by_crash_is_recovered_on_startup(settings):
    with TestClient(create_app(settings, start_workers=False)) as client:
        job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    # Simulate a worker that claimed the job and then the process died.
    assert JobStore(settings.db_path).claim_next().status.value == "processing"

    with TestClient(create_app(settings)) as client:
        done = wait_for_terminal(client, job["id"])
    assert done["status"] == "completed"


def test_expired_output_returns_410(settings):
    with TestClient(create_app(replace(settings, output_retention_hours=0))) as client:
        job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
        assert wait_for_terminal(client, job["id"])["status"] == "completed"
        client.app.state.workers.expire_outputs()

        resp = client.get(f"/v1/crop-jobs/{job['id']}/output")
        assert resp.status_code == 410
        assert resp.json()["error"]["code"] == "OUTPUT_EXPIRED"
        assert client.get(f"/v1/crop-jobs/{job['id']}").json()["output"]["available"] is False
    assert list(settings.output_dir.iterdir()) == []


# --- run_ffmpeg against a fake ffmpeg ---------------------------------------------


def _script(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def test_run_ffmpeg_reports_progress():
    seen = []
    cmd = _script("print('out_time_us=500000', flush=True); print('out_time_us=N/A', flush=True); print('progress=end')")
    media.run_ffmpeg(cmd, duration_s=1.0, timeout_s=5, on_progress=seen.append, should_stop=lambda: None, check_interval_s=0.05)
    assert seen == [50.0]


def test_run_ffmpeg_raises_with_stderr_tail_on_failure():
    cmd = _script("import sys; sys.stderr.write('line1\\nInvalid data found when processing input\\n'); sys.exit(3)")
    with pytest.raises(media.FfmpegError) as exc:
        media.run_ffmpeg(cmd, duration_s=None, timeout_s=5, on_progress=lambda p: None, should_stop=lambda: None)
    assert exc.value.returncode == 3
    assert "Invalid data found" in exc.value.stderr_tail


def test_run_ffmpeg_stops_when_asked():
    with pytest.raises(media.CropInterrupted) as exc:
        media.run_ffmpeg(
            SLOW_COMMAND, duration_s=None, timeout_s=30, on_progress=lambda p: None,
            should_stop=lambda: "shutdown", check_interval_s=0.05,
        )  # fmt: skip
    assert exc.value.reason == "shutdown"
