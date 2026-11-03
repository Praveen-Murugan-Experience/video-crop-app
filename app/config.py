"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass, replace
from pathlib import Path

# Containers we accept as input. ffprobe is the real gatekeeper; this list just
# gives callers a fast, explicit error for obviously wrong files.
SUPPORTED_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})

# Smallest width/height we are willing to produce.
MIN_OUTPUT_DIMENSION = 16


@dataclass(frozen=True)
class Settings:
    input_dir: Path
    output_dir: Path
    db_path: Path
    worker_concurrency: int = 2
    max_attempts: int = 3
    retry_base_delay_s: float = 2.0
    job_timeout_s: float = 600.0
    probe_timeout_s: float = 30.0
    output_retention_hours: float = 24.0
    poll_interval_s: float = 1.0
    janitor_interval_s: float = 60.0
    max_upload_mb: int = 2048
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    @classmethod
    def for_data_dir(cls, data_dir: Path, **overrides) -> "Settings":
        data_dir = Path(data_dir).resolve()
        base = cls(
            input_dir=data_dir / "input",
            output_dir=data_dir / "output",
            db_path=data_dir / "jobs.db",
        )
        return replace(base, **overrides)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        overrides = {}
        if "CROP_INPUT_DIR" in env:
            overrides["input_dir"] = Path(env["CROP_INPUT_DIR"]).resolve()
        if "CROP_OUTPUT_DIR" in env:
            overrides["output_dir"] = Path(env["CROP_OUTPUT_DIR"]).resolve()
        for field, name, cast in (
            ("worker_concurrency", "CROP_WORKER_CONCURRENCY", int),
            ("max_attempts", "CROP_MAX_ATTEMPTS", int),
            ("retry_base_delay_s", "CROP_RETRY_BASE_DELAY_S", float),
            ("job_timeout_s", "CROP_JOB_TIMEOUT_S", float),
            ("output_retention_hours", "CROP_OUTPUT_RETENTION_HOURS", float),
            ("max_upload_mb", "CROP_MAX_UPLOAD_MB", int),
            ("ffmpeg_bin", "CROP_FFMPEG_BIN", str),
            ("ffprobe_bin", "CROP_FFPROBE_BIN", str),
        ):
            if name in env:
                overrides[field] = cast(env[name])
        return cls.for_data_dir(Path(env.get("CROP_DATA_DIR", "data")), **overrides)

    def ensure_dirs(self) -> None:
        for path in (self.input_dir, self.output_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)
