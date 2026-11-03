# Video Crop Service

A small HTTP API that crops videos in the background using FFmpeg. A client submits a crop job (a source video plus how many pixels to remove from each edge), polls the job's status and progress, and downloads the cropped MP4 when the job completes.

Built with **Python 3.11, FastAPI, SQLite and FFmpeg**. It covers the core requirements plus these stretch goals: **progress tracking, cancellation, idempotency, retries, more error handling, and a playback UI**.

---

## Quick start

```bash
brew install ffmpeg               # or apt-get install ffmpeg; needs ffmpeg + ffprobe on PATH
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/uvicorn app.main:create_app --factory --port 8000 --no-access-log
```

**Web UI: http://localhost:8000.** Upload a video (or pick one already uploaded) and set the four crop values. A shaded overlay on the preview shows what will be removed. Submit, and the page shows the job's progress, lets you cancel it, and plays the original and cropped videos side by side when it finishes.

Interactive API docs (OpenAPI) are at http://localhost:8000/docs. With the API alone, upload with `POST /v1/sources`, or just copy files into `data/input/`:

```bash
# Submit
curl -s -X POST localhost:8000/v1/crop-jobs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: my-request-1' \
  -d '{"sourceVideo": "video1.mp4", "crop": {"top": 20, "bottom": 20, "left": 100, "right": 100}}'

# Poll (status, progress, error)
curl -s localhost:8000/v1/crop-jobs/<id>

# Download when status == completed
curl -s -o cropped.mp4 localhost:8000/v1/crop-jobs/<id>/output

# Cancel
curl -s -X POST localhost:8000/v1/crop-jobs/<id>/cancel
```

Run the tests (about 10 seconds; they generate their own sample videos with FFmpeg):

```bash
.venv/bin/python -m pytest -q
```

---

## API

| Method | Path | Purpose | Success |
|---|---|---|---|
| `POST` | `/v1/crop-jobs` | Validate and queue a crop job | `202` + `Location` (`200` on idempotent replay) |
| `GET` | `/v1/crop-jobs/{id}` | Job status, progress, error, output info | `200` |
| `GET` | `/v1/crop-jobs?status=&limit=` | List recent jobs, newest first | `200` |
| `POST` | `/v1/crop-jobs/{id}/cancel` | Cancel a job | `200` if cancelled now, `202` if a running job is being stopped |
| `GET` | `/v1/crop-jobs/{id}/output` | Stream the cropped MP4 (`video/mp4`, inline) | `200` |
| `POST` | `/v1/sources` | Upload a video (multipart field `file`); returns the `name` to use as `sourceVideo` | `201` |
| `GET` | `/v1/sources` | List uploaded source videos | `200` |
| `GET` | `/v1/sources/{name}` | Source dimensions and duration (from ffprobe) | `200` |
| `GET` | `/v1/sources/{name}/content` | Stream the original video, for preview | `200` |
| `GET` | `/` | Web UI | `200` |
| `GET` | `/healthz` | Liveness, and whether ffmpeg/ffprobe are installed | `200` |

### Request

```json
{
  "sourceVideo": "video1.mp4",
  "crop": { "top": 20, "bottom": 20, "left": 100, "right": 100 }
}
```

Optional header: `Idempotency-Key: <1–255 chars>`. Optional header: `X-Request-ID`, which is echoed back and added to the logs.

### Job resource

```json
{
  "id": "2e594c516ddf419caa628d5890736420",
  "status": "processing",
  "sourceVideo": "video1.mp4",
  "crop": { "top": 20, "bottom": 20, "left": 100, "right": 100 },
  "source": { "width": 1920, "height": 1080, "durationSeconds": 65.3 },
  "output": { "width": 1720, "height": 1040, "sizeBytes": null, "url": null, "available": false, "expiresAt": null },
  "progress": 41.2,
  "attempts": 1,
  "maxAttempts": 3,
  "cancelRequested": false,
  "error": null,
  "createdAt": "2026-11-03T10:20:06.904Z",
  "updatedAt": "2026-11-03T10:20:21.113Z",
  "startedAt": "2026-11-03T10:20:06.909Z",
  "completedAt": null
}
```

The job reports `output.width`/`output.height` from the moment it is created, so the caller can see the expected result straight away. `output.url` is set once the job completes.

### Job states

```
             ┌──────── retryable failure, attempts left (backoff) ────────┐
             ▼                                                            │
 POST ──► pending ──claim──► processing ──success──► completed ──(retention)──► output expired (410)
             │                   │  │
             │ cancel            │  └──permanent failure / attempts exhausted──► failed
             ▼                   ▼
         cancelled ◄──cancel─────┘ (ffmpeg is terminated; partial output deleted)
```

`completed`, `failed` and `cancelled` are terminal.

### Errors

Every error uses the same shape, with a stable, machine-readable `code`:

```json
{ "error": { "code": "CROP_EXCEEDS_DIMENSIONS", "message": "Cropping 1920x1080 by ... leaves 1920x0; the result must be at least 16x16" } }
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | Body is not valid JSON |
| 422 | `VALIDATION_ERROR` | Schema problems. `details[]` lists each `{field, message}`: a missing field, a negative value, a non-integer (`"20"`, `20.5` and `true` are all rejected), an unknown field, or all four crop values zero |
| 422 | `INVALID_SOURCE_REFERENCE` | Absolute path, or a path that escapes the input directory (`../`) |
| 422 | `UNSUPPORTED_FORMAT` | Extension not in `.mp4 .mov .m4v .mkv .webm .avi` |
| 422 | `SOURCE_NOT_FOUND` | File does not exist |
| 422 | `INVALID_SOURCE` | ffprobe can't read it: corrupt, not a video, or no video stream |
| 422 | `CROP_EXCEEDS_DIMENSIONS` | The result would be smaller than 16×16, including zero or negative |
| 422 | `ODD_OUTPUT_DIMENSIONS` | Resulting width or height is odd. H.264 4:2:0 needs even dimensions |
| 404 | `JOB_NOT_FOUND` | Unknown job id |
| 409 | `OUTPUT_NOT_READY` | Output requested before the job completed |
| 409 | `JOB_NOT_CANCELLABLE` | Cancel requested on a completed or failed job |
| 409 | `IDEMPOTENCY_KEY_REUSED` | Same key sent with a different body |
| 410 | `OUTPUT_EXPIRED` | Output deleted by the retention policy |
| 500 | `INTERNAL_ERROR` | Unexpected error; the details go to the logs, not to the client |

When processing fails, the job ends in `failed` with `error.code` set to one of: `FFMPEG_FAILED`, `PROCESSING_TIMEOUT`, `PROCESSING_ERROR`, `OUTPUT_VERIFICATION_FAILED`, `SOURCE_NOT_FOUND`, `INVALID_SOURCE`, `INTERNAL_ERROR`, or `WORKER_INTERRUPTED`.

---

## Design decisions and trade-offs

**Validate at submit time, and again in the worker.** Before accepting a job, the API runs `ffprobe` on the source, which usually takes tens of milliseconds. A missing file, a corrupt file or an impossible crop is then rejected immediately with a 422, instead of being accepted and failing later. The worker repeats the same checks (`app/validation.py`, shared by both) because the file can change while the job waits in the queue. The cost is a little extra latency on `POST`; the benefit is much better feedback for the caller.

**Crop applies to the video as displayed.** Phone and screen recordings often store a rotation flag. FFmpeg rotates the frame before filters run, so the crop is applied to what the user actually sees. `ffprobe` dimensions are swapped to match. There is a test for this case.

**Reject odd output sizes instead of quietly adjusting them.** H.264 with yuv420p needs even width and height. Rounding the crop silently would return something other than what was asked for, so the API returns a clear 422 instead. `exact=1` on the crop filter stops FFmpeg from shifting odd left/top offsets.

**Output: always H.264/AAC MP4, with `+faststart`.** This plays anywhere and streams well in a browser. Video is re-encoded, because cropping requires it. AAC audio is copied unchanged; other audio is converted to AAC.

**SQLite job store, with workers as threads in the API process.** This keeps the service to one process with no broker, while jobs still survive restarts. All coordination goes through the job store:
- Workers claim jobs with `BEGIN IMMEDIATE`.
- Every state change is a conditional `UPDATE ... WHERE status = ?`.
- Cancellation is a flag in the database.

So the workers could move to a separate process, or to a real queue such as SQS, Celery or Redis, without changing the API. The trade-off is a single node, and SQLite's limits on concurrent writers.

**Crash safety.**
- Output is written to `<id>.part.mp4`, checked with ffprobe (it must be readable and have the expected dimensions), and then atomically renamed to `<id>.mp4`. A partially written file is never served.
- On startup, jobs left in `processing` go back to `pending`, or to `failed` if they have no attempts left. Stray `.part` files are deleted.
- On graceful shutdown, running jobs are requeued without using up an attempt.

**Retries.**
- Failures are classed as retryable or permanent.
- Permanent: a missing or invalid source, an FFmpeg error that is clearly input-related (`Invalid data found...`, `moov atom not found`...), a timeout (it would very likely time out again), or output with the wrong dimensions.
- Everything else (an unknown FFmpeg exit, an OS error, an unreadable output, an unexpected exception) is retried with exponential backoff: 2s, then 4s, up to 3 attempts.
- While a retry is waiting, the job shows `pending` with `attempts > 0`, and `error` holds the reason for the last failure.

**Idempotency.** This is optional and driven by the client through the `Idempotency-Key` header:
- Same key and same body: returns the original job with `200` and `Idempotent-Replayed: true`. This is checked *before* validation, so a retry still gets its job back even if the source file has since been deleted.
- Same key with a different body: `409`.
- A unique index on the key handles two concurrent requests with the same key.
- Requests without a key are deliberately not deduplicated by content, because two identical crops can both be legitimate.

**Progress.** FFmpeg's `-progress pipe:1` output is read on a separate thread, and `out_time_us / duration` is turned into a percentage. The job store is updated only when the whole-number percentage changes. If a source has no known duration (some WebM screen recordings), `progress` is `null` while processing, rather than a made-up number.

**Cancellation.**
- A pending job is cancelled immediately (`200`).
- A running job is flagged (`202`, `cancelRequested: true`). The worker checks the flag every 0.5s, terminates FFmpeg (SIGTERM, then SIGKILL) and deletes the partial output.
- Cancellation is best-effort: if FFmpeg finishes in the same instant, `completed` wins.

**Timeouts.** Each job has a time limit (default 600s). FFmpeg is watched from a loop that never blocks on its output, so even an FFmpeg that hangs without printing anything can be stopped.

**Source references.** `sourceVideo` is a file name relative to the service's input directory. This stands in for an object-storage key. Absolute paths and `..` are rejected, so a caller can't read arbitrary files on the server.

**Logging.**
- Structured JSON on stdout, one object per line, ready for any log aggregator.
- Each HTTP request is logged with method, path, status, duration and `request_id`.
- Each job lifecycle event (`job.created`, `job.started`, `job.retry_scheduled`, `job.ffmpeg_failed` including the tail of FFmpeg's stderr, `job.completed` with `duration_ms` and `output_bytes`, `job.failed`, `job.cancelled`, `job.output_expired`) is logged with `job_id`.
- `job.created` records both `request_id` and `job_id`, so a request can be traced from the API through to the worker.
- FFmpeg's stderr stays in the logs and is not returned to clients.

---

## Assumptions

- **Input**: videos live in `data/input/`, standing in for object storage. They get there by upload (`POST /v1/sources`) or by copying files in. The crop API only takes a reference, so it would work unchanged with S3 keys. Uploads are streamed to disk, limited to 2 GB (`CROP_MAX_UPLOAD_MB`), checked with ffprobe before they are kept, and given safe, unique names (`My Clip.MOV` becomes `My-Clip.mov`, then `My-Clip-1.mov`, …), so an upload never overwrites an existing file. Supported containers are MP4, MOV, M4V, MKV, WebM and AVI. ffprobe makes the final decision.
- **Crop values**: required, integers from 0 to 100000, in pixels of the displayed frame. At least one must be greater than zero. The result must be at least 16×16 with even dimensions.
- **Output**: `data/output/<jobId>.mp4`, downloaded as `<source-stem>-cropped.mp4`. Encoded with x264 `veryfast` at CRF 20, a reasonable balance of speed and quality.
- **Retention**: outputs are deleted 24h after completion (`CROP_OUTPUT_RETENTION_HOURS`). Job records are kept, so a request for an expired output returns `410` rather than `404`.
- **Concurrency**: 2 worker threads by default (`CROP_WORKER_CONCURRENCY`). Each FFmpeg can use several cores.
- **Out of scope, as the brief says**: authentication, IaC, CI/CD, high availability. The UI is a single dependency-free HTML page for demos and playback, not a production front end.

### Configuration (environment variables)

`CROP_DATA_DIR` (default `./data`), `CROP_INPUT_DIR`, `CROP_OUTPUT_DIR`, `CROP_WORKER_CONCURRENCY` (2), `CROP_MAX_ATTEMPTS` (3), `CROP_RETRY_BASE_DELAY_S` (2), `CROP_JOB_TIMEOUT_S` (600), `CROP_OUTPUT_RETENTION_HOURS` (24), `CROP_MAX_UPLOAD_MB` (2048), `CROP_FFMPEG_BIN`, `CROP_FFPROBE_BIN`.

---

## Testing

50 tests in `tests/`, all against real FFmpeg. The fixtures generate small synthetic videos: H.264+AAC MP4, MPEG-4 MKV with no audio, a rotated MP4, a corrupt file, and a `.txt` file.

- **Most important test**: `test_crop_job_produces_video_with_expected_dimensions`. It submits a job, waits for completion, downloads the result, and confirms with ffprobe that the output really is the requested size, with audio and the full duration intact. If only one test could exist, it would be this one.
- **Validation**: every scenario in the brief, plus non-integers, unknown fields, an all-zero crop, odd sizes and path traversal. Each test checks the exact error code, and that nothing was queued.
- **Failure handling**: a transient failure is retried and then succeeds; a permanent failure is not retried; retries stop after the maximum number of attempts; a source deleted after submission fails; cancelling a running job stops it within 5s and leaves no partial file; a timeout fails the job; a job interrupted by a crash is recovered on restart; an expired output returns 410.
- **Idempotency**: replay, a reused key with a different body, and requests without a key.
- **Uploads**: an uploaded file can be cropped; names are made safe and never overwrite existing files; unsupported, corrupt and oversized uploads are rejected with no temporary files left behind; listing, describing and previewing sources.
- **Running FFmpeg**: progress parsing, the stderr tail on failure, and stopping on request, all tested against a fake `ffmpeg` script so they run quickly and predictably.

I also ran a manual check against real 2880×1800 screen recordings: a 65s MOV and a WebM with no duration. That covered progress, an idempotent replay, a crop that was too large, and cancelling a running job (FFmpeg stopped in about 0.4s). I also tried the web UI end to end in a browser: choosing a source, seeing the crop overlay, live progress, the side-by-side result playing at the right size, and a server-side error (an odd width) shown on the page.

---

## How AI was used

This was built with Claude Code as a pair programmer. I set the direction: the stack (FastAPI), which stretch goals to include, and what to put first. I then reviewed and steered the design: the job-state model, validating at submit time, rejecting odd sizes rather than rounding, and classing failures as retryable or permanent. The AI drafted most of the code and the tests. Bugs found during review were fixed, for example a method named `list` that shadowed the built-in type inside its class, and redundant cleanup of partial files. Every behaviour above is covered by a test or was checked by hand against real recordings.

---

## What I'd do with more time

- **Real storage and queue**: S3 or GCS references (or presigned upload URLs) instead of a local directory; a proper queue with workers that scale independently; Postgres instead of SQLite.
- **Push instead of poll**: a webhook or Server-Sent Events when a job finishes; progress as a stream.
- **Resource limits**: maximum input size and duration, per-client rate limits, and FFmpeg run under CPU and memory limits (cgroups or a container per job).
- **Smarter retries**: build up the list of FFmpeg error patterns from real production failures; add a dead-letter view for failed jobs.
- **Metrics**: Prometheus counters and histograms (jobs by status, processing time, queue depth, retry rate) and OpenTelemetry tracing across the API and the worker.
- **Output options**: choice of codec and quality, keeping the source codec where possible, and a thumbnail or preview frame.
- **Retention of job records**, not just output files; pagination cursors on the list endpoint.
- **UI**: set the crop by dragging a box on the preview; play the original and the result in sync; resumable uploads for large files.

## Layout

```
app/
  main.py          FastAPI app factory, routes, request-ID/logging middleware
  sources.py       upload / list / describe / preview of source videos
  static/index.html  web UI (vanilla JS, no build step)
  service.py       submit/get/cancel/output logic, idempotency, response mapping
  worker.py        worker threads: claim → crop → verify → complete; retries, cancel, recovery, retention
  media.py         ffprobe/ffmpeg wrappers (probe, build command, run with progress/cancel/timeout)
  validation.py    path safety, format, crop geometry (shared by API and worker)
  store.py         SQLite job store with atomic, conditional state transitions
  models.py        Pydantic request/response schemas
  errors.py        uniform error envelope and exception handlers
  config.py        settings from environment
  logging_setup.py JSON log formatter
tests/             API, validation, worker, upload and ffmpeg-runner tests
```
