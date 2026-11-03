"""Application logic behind the HTTP routes."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app import media
from app.config import Settings
from app.errors import ApiError
from app.models import (
    CreateCropJobRequest,
    CropJobResponse,
    JobErrorInfo,
    JobStatus,
    OutputInfo,
    SourceInfo,
)
from app.store import DuplicateIdempotencyKey, Job, JobStore
from app.validation import InvalidRequest, check_source_file, output_dimensions, resolve_source
from app.worker import WorkerPool

logger = logging.getLogger(__name__)


class CropJobService:
    def __init__(self, settings: Settings, store: JobStore, workers: WorkerPool):
        self._settings = settings
        self._store = store
        self._workers = workers

    def submit(self, request: CreateCropJobRequest, idempotency_key: str | None) -> tuple[Job, bool]:
        """Validate and queue a crop job. Returns (job, created); created is False for an idempotent replay."""
        request_hash = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True).encode()).hexdigest()
        # Check for a replay before validating, so a retried request still gets its original
        # job back even if the source file has since been removed.
        if idempotency_key and (existing := self._store.find_by_idempotency_key(idempotency_key)):
            return self._replay(existing, request_hash), False

        try:
            src = resolve_source(self._settings.input_dir, request.source_video)
            check_source_file(src, request.source_video)
            info = media.probe(src, self._settings.ffprobe_bin, self._settings.probe_timeout_s)
            out_w, out_h = output_dimensions(info.width, info.height, request.crop)
        except InvalidRequest as exc:
            raise ApiError(422, exc.code, exc.message) from exc
        except media.ProbeError as exc:
            raise ApiError(422, "INVALID_SOURCE", str(exc)) from exc

        job = Job.new(
            source_video=request.source_video,
            crop=request.crop,
            source_width=info.width,
            source_height=info.height,
            source_duration=info.duration_s,
            output_width=out_w,
            output_height=out_h,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        try:
            self._store.insert(job)
        except DuplicateIdempotencyKey:
            # Lost a race with a concurrent request carrying the same key.
            return self._replay(self._store.find_by_idempotency_key(idempotency_key), request_hash), False

        logger.info(
            "job.created",
            extra={
                "job_id": job.id,
                "source_video": job.source_video,
                "crop": request.crop.model_dump(),
                "source_size": f"{info.width}x{info.height}",
                "output_size": f"{out_w}x{out_h}",
            },
        )
        self._workers.notify()
        return job, True

    def get(self, job_id: str) -> Job:
        job = self._store.get(job_id)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", f"No crop job with id '{job_id}'")
        return job

    def list_jobs(self, status: JobStatus | None, limit: int) -> list[Job]:
        return self._store.list_jobs(status, limit)

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            raise ApiError(409, "JOB_NOT_CANCELLABLE", f"Job is already {job.status.value}")
        job = self._store.request_cancel(job_id)
        logger.info("job.cancel_requested", extra={"job_id": job_id, "status": job.status.value})
        return job

    def output_file(self, job_id: str) -> tuple[Path, str]:
        job = self.get(job_id)
        if job.status != JobStatus.COMPLETED:
            raise ApiError(409, "OUTPUT_NOT_READY", f"Output is not available while the job is {job.status.value}")
        path = Path(job.output_path)
        if job.output_expired or not path.is_file():
            raise ApiError(410, "OUTPUT_EXPIRED", "Output has been removed under the retention policy")
        return path, f"{Path(job.source_video).stem}-cropped.mp4"

    def to_response(self, job: Job) -> CropJobResponse:
        completed = job.status == JobStatus.COMPLETED
        available = completed and not job.output_expired
        expires_at = None
        if completed and job.completed_at:
            expires_at = _dt(job.completed_at + self._settings.output_retention_hours * 3600)
        if job.status == JobStatus.PROCESSING and not job.source_duration:
            progress = None
        else:
            progress = job.progress
        return CropJobResponse(
            id=job.id,
            status=job.status,
            source_video=job.source_video,
            crop=job.crop,
            source=SourceInfo(width=job.source_width, height=job.source_height, duration_seconds=job.source_duration),
            output=OutputInfo(
                width=job.output_width,
                height=job.output_height,
                size_bytes=job.output_size,
                url=f"/v1/crop-jobs/{job.id}/output" if available else None,
                available=available,
                expires_at=expires_at,
            ),
            progress=progress,
            attempts=job.attempts,
            max_attempts=self._settings.max_attempts,
            cancel_requested=job.cancel_requested,
            error=JobErrorInfo(code=job.error_code, message=job.error_message) if job.error_code else None,
            created_at=_dt(job.created_at),
            updated_at=_dt(job.updated_at),
            started_at=_dt(job.started_at),
            completed_at=_dt(job.completed_at),
        )

    @staticmethod
    def _replay(existing: Job, request_hash: str) -> Job:
        if existing.request_hash != request_hash:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request body")
        logger.info("job.idempotent_replay", extra={"job_id": existing.id})
        return existing


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc) if ts is not None else None
