"""Background workers that pull pending jobs from the store and run ffmpeg.

Workers are threads in the API process. Because all coordination goes through
the job store (claiming, cancel flags, retries), they could move to a separate
process or host without changing the API.
"""

import logging
import os
import threading
import time
from pathlib import Path

from app import media
from app.config import Settings
from app.store import Job, JobStore
from app.validation import InvalidRequest, check_source_file, output_dimensions, resolve_source

logger = logging.getLogger(__name__)

# ffmpeg failures that will fail the same way every time, so retrying is pointless.
PERMANENT_FFMPEG_ERRORS = (
    "Invalid data found when processing input",
    "moov atom not found",
    "does not contain any stream",
    "could not find codec parameters",
    "Error opening input",
)


class ProcessingError(Exception):
    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class WorkerPool:
    def __init__(self, settings: Settings, store: JobStore):
        self._settings = settings
        self._store = store
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        recovered = self._store.recover_interrupted(self._settings.max_attempts)
        for stale in self._settings.output_dir.glob("*.part.mp4"):
            stale.unlink(missing_ok=True)
        if recovered:
            logger.warning("worker.recovered_jobs", extra={"count": recovered})
        self._stop.clear()
        for i in range(self._settings.worker_concurrency):
            self._spawn(self._run, f"crop-worker-{i}")
        self._spawn(self._run_janitor, "crop-janitor")
        logger.info("worker.started", extra={"concurrency": self._settings.worker_concurrency})

    def stop(self, timeout_s: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout=timeout_s)
        self._threads.clear()
        logger.info("worker.stopped")

    def notify(self) -> None:
        """Wake an idle worker so a new job starts without waiting for the next poll."""
        self._wake.set()

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._store.claim_next()
            except Exception:
                logger.exception("worker.claim_failed")
                job = None
            if job is None:
                self._wake.wait(self._settings.poll_interval_s)
                self._wake.clear()
                continue
            self.process(job)

    def _run_janitor(self) -> None:
        while not self._stop.wait(self._settings.janitor_interval_s):
            try:
                self.expire_outputs()
            except Exception:
                logger.exception("janitor.failed")

    def expire_outputs(self) -> None:
        cutoff = time.time() - self._settings.output_retention_hours * 3600
        for job_id, path in self._store.expire_outputs(cutoff):
            if path:
                Path(path).unlink(missing_ok=True)
            logger.info("job.output_expired", extra={"job_id": job_id})

    # --- processing a single job ----------------------------------------------

    def process(self, job: Job) -> None:
        ctx = {"job_id": job.id, "attempt": job.attempts, "source_video": job.source_video}
        logger.info("job.started", extra=ctx)
        started = time.monotonic()
        try:
            output, size = self._crop(job)
        except media.CropInterrupted as exc:
            if exc.reason == "shutdown":
                self._store.requeue(job.id)
                logger.info("job.requeued_on_shutdown", extra=ctx)
            else:
                self._store.mark_cancelled(job.id)
                logger.info("job.cancelled", extra=ctx)
            return
        except ProcessingError as exc:
            self._handle_failure(job, exc, ctx)
            return
        except Exception:
            logger.exception("job.unexpected_error", extra=ctx)
            self._handle_failure(job, ProcessingError("INTERNAL_ERROR", "Unexpected error while processing", True), ctx)
            return

        duration_ms = round((time.monotonic() - started) * 1000)
        if self._store.mark_completed(job.id, str(output), size):
            logger.info("job.completed", extra={**ctx, "duration_ms": duration_ms, "output_bytes": size})
        else:
            # The job left 'processing' under us (shouldn't happen); don't leave an orphaned file.
            output.unlink(missing_ok=True)
            logger.warning("job.completion_discarded", extra=ctx)

    def _crop(self, job: Job) -> tuple[Path, int]:
        settings = self._settings
        try:
            src = resolve_source(settings.input_dir, job.source_video)
            check_source_file(src, job.source_video)
            info = media.probe(src, settings.ffprobe_bin, settings.probe_timeout_s)
            out_w, out_h = output_dimensions(info.width, info.height, job.crop)
        except InvalidRequest as exc:
            raise ProcessingError(exc.code, exc.message, retryable=False) from exc
        except media.ProbeError as exc:
            raise ProcessingError("INVALID_SOURCE", str(exc), retryable=False) from exc

        part = settings.output_dir / f"{job.id}.part.mp4"
        final = settings.output_dir / f"{job.id}.mp4"
        cmd = media.build_crop_command(settings.ffmpeg_bin, src, part, job.crop, out_w, out_h, info.audio_codec)
        try:
            media.run_ffmpeg(
                cmd,
                duration_s=info.duration_s,
                timeout_s=settings.job_timeout_s,
                on_progress=self._progress_reporter(job.id),
                should_stop=lambda: self._stop_reason(job.id),
            )
            self._verify_output(part, out_w, out_h)
        except media.CropTimeout as exc:
            # A job that timed out once will very likely time out again.
            raise ProcessingError("PROCESSING_TIMEOUT", str(exc), retryable=False) from exc
        except media.FfmpegError as exc:
            permanent = any(marker in exc.stderr_tail for marker in PERMANENT_FFMPEG_ERRORS)
            logger.warning(
                "job.ffmpeg_failed",
                extra={"job_id": job.id, "returncode": exc.returncode, "stderr_tail": exc.stderr_tail, "permanent": permanent},
            )
            raise ProcessingError("FFMPEG_FAILED", f"Video processing failed (ffmpeg exit code {exc.returncode})", not permanent) from exc
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise ProcessingError("PROCESSING_ERROR", f"Could not run video processing: {exc}", retryable=True) from exc
        except BaseException:
            part.unlink(missing_ok=True)  # cancelled, shut down or unexpected: never leave partial output
            raise
        os.replace(part, final)  # atomic: readers never see a half-written output
        return final, final.stat().st_size

    def _verify_output(self, part: Path, out_w: int, out_h: int) -> None:
        try:
            info = media.probe(part, self._settings.ffprobe_bin, self._settings.probe_timeout_s)
        except media.ProbeError as exc:
            raise ProcessingError("OUTPUT_VERIFICATION_FAILED", f"Output is not a readable video: {exc}", retryable=True) from exc
        if (info.width, info.height) != (out_w, out_h):
            raise ProcessingError(
                "OUTPUT_VERIFICATION_FAILED",
                f"Output is {info.width}x{info.height}, expected {out_w}x{out_h}",
                retryable=False,
            )

    def _handle_failure(self, job: Job, exc: ProcessingError, ctx: dict) -> None:
        (self._settings.output_dir / f"{job.id}.part.mp4").unlink(missing_ok=True)
        if exc.retryable and job.attempts < self._settings.max_attempts:
            delay = self._settings.retry_base_delay_s * 2 ** (job.attempts - 1)
            self._store.schedule_retry(job.id, delay, exc.code, exc.message)
            logger.warning("job.retry_scheduled", extra={**ctx, "error_code": exc.code, "error": exc.message, "retry_in_s": delay})
        else:
            self._store.mark_failed(job.id, exc.code, exc.message)
            logger.error("job.failed", extra={**ctx, "error_code": exc.code, "error": exc.message, "retryable": exc.retryable})

    def _stop_reason(self, job_id: str) -> str | None:
        if self._stop.is_set():
            return "shutdown"
        return "cancelled" if self._store.is_cancel_requested(job_id) else None

    def _progress_reporter(self, job_id: str):
        last = -1

        def report(pct: float) -> None:
            nonlocal last
            if int(pct) > last:  # throttle DB writes to whole-percent changes
                last = int(pct)
                self._store.update_progress(job_id, round(pct, 1))

        return report
