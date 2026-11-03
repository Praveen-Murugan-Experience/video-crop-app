"""API request and response schemas (camelCase on the wire)."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pydantic.alias_generators import to_camel

MAX_CROP_PX = 100_000


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})


class Crop(ApiModel):
    """Pixels to remove from each edge, measured on the video as displayed."""

    # StrictInt rejects "20", 20.5 and true rather than silently coercing them.
    top: StrictInt = Field(ge=0, le=MAX_CROP_PX)
    bottom: StrictInt = Field(ge=0, le=MAX_CROP_PX)
    left: StrictInt = Field(ge=0, le=MAX_CROP_PX)
    right: StrictInt = Field(ge=0, le=MAX_CROP_PX)

    @model_validator(mode="after")
    def _not_a_no_op(self) -> "Crop":
        if not (self.top or self.bottom or self.left or self.right):
            raise ValueError("at least one crop value must be greater than 0")
        return self


class CreateCropJobRequest(ApiModel):
    source_video: str = Field(
        min_length=1,
        max_length=255,
        description="File name (or relative path) of a video inside the service's input directory.",
        examples=["video1.mp4"],
    )
    crop: Crop


class ErrorBody(ApiModel):
    code: str
    message: str
    details: list[dict] | None = None


class ErrorResponse(ApiModel):
    error: ErrorBody


class SourceVideo(ApiModel):
    name: str = Field(description="Value to pass as sourceVideo when creating a crop job.")
    size_bytes: int
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None


class SourceVideoList(ApiModel):
    items: list[SourceVideo]


class SourceInfo(ApiModel):
    width: int
    height: int
    duration_seconds: float | None


class OutputInfo(ApiModel):
    width: int
    height: int
    size_bytes: int | None = None
    url: str | None = None
    available: bool
    expires_at: datetime | None = None


class JobErrorInfo(ApiModel):
    code: str
    message: str


class CropJobResponse(ApiModel):
    id: str
    status: JobStatus
    source_video: str
    crop: Crop
    source: SourceInfo
    output: OutputInfo
    progress: float | None = Field(description="0-100. Null while processing a source of unknown duration.")
    attempts: int
    max_attempts: int
    cancel_requested: bool
    error: JobErrorInfo | None = Field(
        description="Most recent failure. On a pending job with attempts > 0 this is why it is being retried."
    )
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CropJobList(ApiModel):
    items: list[CropJobResponse]
