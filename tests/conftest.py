import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import TERMINAL_STATUSES

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    """Small synthetic videos generated once per test session."""
    d = tmp_path_factory.mktemp("samples")
    (d / "corrupt.mp4").write_bytes(b"this is not a video" * 100)
    (d / "notes.txt").write_text("not a video")
    if HAS_FFMPEG:
        # 320x240, 2s, H.264 + AAC
        _ffmpeg(
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "small.mp4"),
        )  # fmt: skip
        # Different container and codec, no audio track
        _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=1", "-c:v", "mpeg4", str(d / "silent.mkv"))
        # Same 320x240 frames, but tagged to display rotated 90 degrees (as phones record portrait video)
        _ffmpeg("-display_rotation:v:0", "90", "-i", str(d / "small.mp4"), "-c", "copy", str(d / "rotated.mp4"))
    return d


@pytest.fixture
def settings(tmp_path, sample_dir) -> Settings:
    s = Settings.for_data_dir(
        tmp_path,
        worker_concurrency=1,
        retry_base_delay_s=0.05,
        poll_interval_s=0.05,
        janitor_interval_s=3600,
    )
    s.ensure_dirs()
    for f in sample_dir.iterdir():
        shutil.copy(f, s.input_dir / f.name)
    return s


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def idle_client(settings):
    """App with workers stopped, so jobs stay pending."""
    with TestClient(create_app(settings, start_workers=False)) as c:
        yield c


def wait_for_terminal(client: TestClient, job_id: str, timeout_s: float = 30) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job = client.get(f"/v1/crop-jobs/{job_id}").json()
        if job["status"] in {s.value for s in TERMINAL_STATUSES}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s; last state: {job}")


def crop_request(source: str = "small.mp4", **crop) -> dict:
    return {"sourceVideo": source, "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0, **crop}}
