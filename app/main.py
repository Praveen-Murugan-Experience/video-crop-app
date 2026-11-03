"""HTTP API. Run with: uvicorn app.main:create_app --factory"""

import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from app.config import Settings
from app.errors import register_error_handlers
from app.logging_setup import configure_logging, request_id_var
from app.models import (
    CreateCropJobRequest,
    CropJobList,
    CropJobResponse,
    ErrorResponse,
    JobStatus,
    SourceVideo,
    SourceVideoList,
)
from app.service import CropJobService
from app.sources import SourceLibrary
from app.store import JobStore
from app.worker import WorkerPool

logger = logging.getLogger("app.http")

_ERRORS = {code: {"model": ErrorResponse} for code in (400, 404, 409, 410, 413, 422)}
_UI_PAGE = Path(__file__).parent / "static" / "index.html"


def create_app(settings: Settings | None = None, start_workers: bool = True) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    store = JobStore(settings.db_path)
    workers = WorkerPool(settings, store)
    service = CropJobService(settings, store, workers)
    sources = SourceLibrary(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_workers:
            workers.start()
        yield
        if start_workers:
            workers.stop()

    app = FastAPI(title="Video Crop Service", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.workers = settings, store, workers
    register_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    @app.post("/v1/crop-jobs", status_code=202, response_model=CropJobResponse, responses=_ERRORS)
    def create_crop_job(
        body: CreateCropJobRequest,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=255),
    ):
        """Queue a crop job. Returns 202 with the new job, or 200 with the original job for an idempotent replay."""
        job, created = service.submit(body, idempotency_key)
        response.headers["Location"] = f"/v1/crop-jobs/{job.id}"
        if not created:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return service.to_response(job)

    @app.get("/v1/crop-jobs", response_model=CropJobList)
    def list_crop_jobs(status: JobStatus | None = None, limit: int = Query(default=50, ge=1, le=200)):
        return CropJobList(items=[service.to_response(j) for j in service.list_jobs(status, limit)])

    @app.get("/v1/crop-jobs/{job_id}", response_model=CropJobResponse, responses=_ERRORS)
    def get_crop_job(job_id: str):
        return service.to_response(service.get(job_id))

    @app.post("/v1/crop-jobs/{job_id}/cancel", response_model=CropJobResponse, responses=_ERRORS)
    def cancel_crop_job(job_id: str, response: Response):
        """Cancel a job. Pending jobs are cancelled at once (200); processing jobs are stopped asynchronously (202)."""
        job = service.cancel(job_id)
        if job.status == JobStatus.PROCESSING:
            response.status_code = 202
        return service.to_response(job)

    @app.get("/v1/crop-jobs/{job_id}/output", response_class=FileResponse, responses=_ERRORS)
    def download_output(job_id: str):
        path, filename = service.output_file(job_id)
        return FileResponse(path, media_type="video/mp4", filename=filename, content_disposition_type="inline")

    @app.post("/v1/sources", status_code=201, response_model=SourceVideo, responses=_ERRORS)
    def upload_source(file: UploadFile = File(...)):
        """Upload a video into the input directory. Use the returned name as sourceVideo."""
        return sources.save_upload(file.filename, file.file)

    @app.get("/v1/sources", response_model=SourceVideoList)
    def list_sources():
        return SourceVideoList(items=sources.list_sources())

    @app.get("/v1/sources/{name}", response_model=SourceVideo, responses=_ERRORS)
    def describe_source(name: str):
        return sources.describe(name)

    @app.get("/v1/sources/{name}/content", response_class=FileResponse, responses=_ERRORS)
    def source_content(name: str):
        path = sources.path_for(name)
        return FileResponse(path, media_type=sources.preview_type(path), content_disposition_type="inline")

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(_UI_PAGE, media_type="text/html")

    @app.get("/healthz")
    def health():
        tools = {"ffmpeg": shutil.which(settings.ffmpeg_bin) is not None, "ffprobe": shutil.which(settings.ffprobe_bin) is not None}
        return {"status": "ok" if all(tools.values()) else "degraded", **tools}

    return app
