"""Upload, listing and preview of source videos, plus the UI page."""

from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import crop_request, requires_ffmpeg, wait_for_terminal

pytestmark = requires_ffmpeg


def _upload(client, sample_dir, name="small.mp4", as_name=None):
    with open(sample_dir / name, "rb") as f:
        return client.post("/v1/sources", files={"file": (as_name or name, f, "application/octet-stream")})


def test_uploaded_video_can_be_cropped(client, sample_dir):
    resp = _upload(client, sample_dir, as_name="My Holiday Clip.MP4")
    assert resp.status_code == 201
    src = resp.json()
    assert src["name"] == "My-Holiday-Clip.mp4"  # sanitised, extension normalised
    assert (src["width"], src["height"]) == (320, 240)

    job = client.post("/v1/crop-jobs", json=crop_request(src["name"], top=20)).json()
    assert wait_for_terminal(client, job["id"])["status"] == "completed"


def test_upload_never_overwrites_existing_file(client, sample_dir, settings):
    original = (settings.input_dir / "small.mp4").read_bytes()
    resp = _upload(client, sample_dir, "silent.mkv", as_name="small.mp4")
    assert resp.json()["name"] == "small-1.mp4"
    assert (settings.input_dir / "small.mp4").read_bytes() == original


def test_upload_strips_path_components(client, sample_dir, settings):
    resp = _upload(client, sample_dir, as_name="../../evil.mp4")
    assert resp.json()["name"] == "evil.mp4"
    assert (settings.input_dir / "evil.mp4").is_file()


def test_upload_rejects_unsupported_and_corrupt_files(client, sample_dir, settings):
    assert _upload(client, sample_dir, "notes.txt").json()["error"]["code"] == "UNSUPPORTED_FORMAT"
    resp = _upload(client, sample_dir, "corrupt.mp4", as_name="broken.mp4")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_SOURCE"
    assert not (settings.input_dir / "broken.mp4").exists()
    assert not list(settings.input_dir.glob(".upload-*"))  # temp file cleaned up


def test_upload_size_limit(settings, sample_dir):
    with TestClient(create_app(replace(settings, max_upload_mb=0), start_workers=False)) as client:
        resp = _upload(client, sample_dir)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    assert not list(settings.input_dir.glob(".upload-*"))


def test_list_describe_and_preview_sources(idle_client):
    names = [s["name"] for s in idle_client.get("/v1/sources").json()["items"]]
    assert {"small.mp4", "silent.mkv", "rotated.mp4", "corrupt.mp4"} <= set(names)
    assert "notes.txt" not in names

    info = idle_client.get("/v1/sources/rotated.mp4").json()
    assert (info["width"], info["height"]) == (240, 320)

    content = idle_client.get("/v1/sources/small.mp4/content")
    assert content.status_code == 200
    assert content.headers["content-type"] == "video/mp4"

    assert idle_client.get("/v1/sources/missing.mp4").status_code == 404
    assert idle_client.get("/v1/sources/corrupt.mp4").json()["error"]["code"] == "INVALID_SOURCE"


def test_ui_page_is_served(idle_client):
    resp = idle_client.get("/")
    assert resp.status_code == 200
    assert "Video Crop Service" in resp.text
