import pytest

from app import media
from tests.conftest import crop_request, requires_ffmpeg, wait_for_terminal

pytestmark = requires_ffmpeg


# --- the core happy path -------------------------------------------------------


def test_crop_job_produces_video_with_expected_dimensions(client, tmp_path):
    """The most important test: submit -> process -> download, and the pixels are right."""
    resp = client.post("/v1/crop-jobs", json=crop_request(top=20, bottom=20, left=40, right=40))
    assert resp.status_code == 202
    job = resp.json()
    assert resp.headers["Location"] == f"/v1/crop-jobs/{job['id']}"
    assert job["status"] in ("pending", "processing")
    assert job["source"] == {"width": 320, "height": 240, "durationSeconds": pytest.approx(2.0, abs=0.1)}

    done = wait_for_terminal(client, job["id"])
    assert done["status"] == "completed", done
    assert done["progress"] == 100
    assert done["error"] is None
    assert done["output"]["available"] is True
    assert (done["output"]["width"], done["output"]["height"]) == (240, 200)

    download = client.get(done["output"]["url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    out = tmp_path / "out.mp4"
    out.write_bytes(download.content)
    info = media.probe(out)
    assert (info.width, info.height) == (240, 200)
    assert info.audio_codec == "aac"
    assert info.duration_s == pytest.approx(2.0, abs=0.2)


def test_rotated_source_is_cropped_as_displayed(client, tmp_path):
    # rotated.mp4 is stored 320x240 but displays as 240x320 portrait.
    resp = client.post("/v1/crop-jobs", json=crop_request("rotated.mp4", top=100, bottom=20))
    assert resp.status_code == 202
    assert resp.json()["source"]["width"] == 240

    done = wait_for_terminal(client, resp.json()["id"])
    assert done["status"] == "completed", done
    out = tmp_path / "out.mp4"
    out.write_bytes(client.get(done["output"]["url"]).content)
    info = media.probe(out)
    assert (info.width, info.height) == (240, 200)


def test_other_container_without_audio(client):
    resp = client.post("/v1/crop-jobs", json=crop_request("silent.mkv", left=10, right=10))
    done = wait_for_terminal(client, resp.json()["id"])
    assert done["status"] == "completed", done
    assert (done["output"]["width"], done["output"]["height"]) == (300, 240)


# --- request validation --------------------------------------------------------


@pytest.mark.parametrize(
    "body, field",
    [
        (crop_request(top=-1, left=10), "crop.top"),
        ({"sourceVideo": "small.mp4", "crop": {"top": 1, "bottom": 1, "left": 1}}, "crop.right"),
        ({"sourceVideo": "small.mp4"}, "crop"),
        ({"crop": {"top": 2, "bottom": 2, "left": 2, "right": 2}}, "sourceVideo"),
        (crop_request(top="20"), "crop.top"),
        (crop_request(top=20.5), "crop.top"),
        (crop_request(top=True), "crop.top"),
        (crop_request(), "crop"),  # all zeros: nothing to crop
        ({**crop_request(top=2), "unexpected": 1}, "unexpected"),
        (crop_request("", top=2), "sourceVideo"),
    ],
)
def test_invalid_request_bodies_are_rejected(idle_client, body, field):
    resp = idle_client.post("/v1/crop-jobs", json=body)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert field in [d["field"] for d in error["details"]]


def test_malformed_json_is_400(idle_client):
    resp = idle_client.post("/v1/crop-jobs", content=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MALFORMED_REQUEST"


@pytest.mark.parametrize(
    "body, code",
    [
        (crop_request(left=160, right=160), "CROP_EXCEEDS_DIMENSIONS"),  # nothing left
        (crop_request(top=500), "CROP_EXCEEDS_DIMENSIONS"),
        (crop_request(left=150, right=156), "CROP_EXCEEDS_DIMENSIONS"),  # 14px wide: below minimum
        (crop_request(left=1), "ODD_OUTPUT_DIMENSIONS"),
        (crop_request("missing.mp4", top=2), "SOURCE_NOT_FOUND"),
        (crop_request("corrupt.mp4", top=2), "INVALID_SOURCE"),
        (crop_request("notes.txt", top=2), "UNSUPPORTED_FORMAT"),
        (crop_request("../../etc/passwd.mp4", top=2), "INVALID_SOURCE_REFERENCE"),
        (crop_request("/etc/hosts", top=2), "INVALID_SOURCE_REFERENCE"),
    ],
)
def test_source_and_geometry_problems_are_rejected_at_submit(idle_client, body, code):
    resp = idle_client.post("/v1/crop-jobs", json=body)
    assert resp.status_code == 422, resp.json()
    assert resp.json()["error"]["code"] == code
    assert idle_client.get("/v1/crop-jobs").json()["items"] == []  # nothing was queued


# --- job lookup, output and cancellation -----------------------------------------------


def test_unknown_job_is_404(idle_client):
    for path in ("/v1/crop-jobs/nope", "/v1/crop-jobs/nope/output"):
        resp = idle_client.get(path)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_output_before_completion_is_409(idle_client):
    job = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    resp = idle_client.get(f"/v1/crop-jobs/{job['id']}/output")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "OUTPUT_NOT_READY"


def test_cancel_pending_job(idle_client):
    job = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    resp = idle_client.post(f"/v1/crop-jobs/{job['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    # Cancelling again is a harmless no-op.
    assert idle_client.post(f"/v1/crop-jobs/{job['id']}/cancel").json()["status"] == "cancelled"


def test_cannot_cancel_completed_job(client):
    job = client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    assert wait_for_terminal(client, job["id"])["status"] == "completed"
    resp = client.post(f"/v1/crop-jobs/{job['id']}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "JOB_NOT_CANCELLABLE"


def test_list_filters_by_status(idle_client):
    a = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    idle_client.post("/v1/crop-jobs", json=crop_request(top=4))
    idle_client.post(f"/v1/crop-jobs/{a['id']}/cancel")
    assert [j["id"] for j in idle_client.get("/v1/crop-jobs?status=cancelled").json()["items"]] == [a["id"]]
    assert len(idle_client.get("/v1/crop-jobs?status=pending").json()["items"]) == 1


# --- idempotency -------------------------------------------------------------


def test_idempotent_replay_returns_original_job(idle_client):
    headers = {"Idempotency-Key": "order-123"}
    first = idle_client.post("/v1/crop-jobs", json=crop_request(top=2), headers=headers)
    second = idle_client.post("/v1/crop-jobs", json=crop_request(top=2), headers=headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["id"] == first.json()["id"]
    assert len(idle_client.get("/v1/crop-jobs").json()["items"]) == 1


def test_idempotency_key_reused_with_different_body_is_409(idle_client):
    headers = {"Idempotency-Key": "order-123"}
    idle_client.post("/v1/crop-jobs", json=crop_request(top=2), headers=headers)
    resp = idle_client.post("/v1/crop-jobs", json=crop_request(top=4), headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_requests_without_key_are_not_deduplicated(idle_client):
    a = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    b = idle_client.post("/v1/crop-jobs", json=crop_request(top=2)).json()
    assert a["id"] != b["id"]


# --- operational ---------------------------------------------------------------


def test_request_id_is_echoed(idle_client):
    resp = idle_client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert resp.headers["X-Request-ID"] == "abc"
    assert resp.json() == {"status": "ok", "ffmpeg": True, "ffprobe": True}
