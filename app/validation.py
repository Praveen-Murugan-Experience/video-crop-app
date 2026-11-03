"""Checks on the source file and crop geometry, shared by the API and the worker.

The API runs them at submit time so callers get immediate feedback; the worker
runs them again because the file may have changed while the job was queued.
"""

from pathlib import Path

from app.config import MIN_OUTPUT_DIMENSION, SUPPORTED_EXTENSIONS
from app.models import Crop


class InvalidRequest(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_source(input_dir: Path, source_video: str) -> Path:
    """Map a caller-supplied reference to a path, refusing anything outside input_dir."""
    if Path(source_video).is_absolute() or "\\" in source_video or "\x00" in source_video:
        raise InvalidRequest("INVALID_SOURCE_REFERENCE", "sourceVideo must be a relative path inside the input directory")
    path = (input_dir / source_video).resolve()
    if not path.is_relative_to(input_dir.resolve()):
        raise InvalidRequest("INVALID_SOURCE_REFERENCE", "sourceVideo must be a relative path inside the input directory")
    return path


def check_source_file(path: Path, source_video: str) -> None:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise InvalidRequest("UNSUPPORTED_FORMAT", f"'{path.suffix or source_video}' is not supported; use one of {supported}")
    if not path.is_file():
        raise InvalidRequest("SOURCE_NOT_FOUND", f"Source video '{source_video}' does not exist")


def output_dimensions(width: int, height: int, crop: Crop) -> tuple[int, int]:
    out_w = width - crop.left - crop.right
    out_h = height - crop.top - crop.bottom
    if out_w < MIN_OUTPUT_DIMENSION or out_h < MIN_OUTPUT_DIMENSION:
        raise InvalidRequest(
            "CROP_EXCEEDS_DIMENSIONS",
            f"Cropping {width}x{height} by left={crop.left}, right={crop.right}, top={crop.top}, "
            f"bottom={crop.bottom} leaves {out_w}x{out_h}; the result must be at least "
            f"{MIN_OUTPUT_DIMENSION}x{MIN_OUTPUT_DIMENSION}",
        )
    # H.264 with 4:2:0 chroma needs even dimensions. We reject rather than silently
    # shaving a pixel, so the output is always exactly what was asked for.
    if out_w % 2 or out_h % 2:
        raise InvalidRequest(
            "ODD_OUTPUT_DIMENSIONS",
            f"Cropping {width}x{height} leaves {out_w}x{out_h}; output width and height must both be even",
        )
    return out_w, out_h
