"""Source videos in the input directory: upload, list, inspect and serve for preview.

Uploading is a convenience for the demo UI. The crop API itself still takes a
plain file reference, so it would work unchanged with object storage.
"""

import logging
import os
import re
import uuid
from pathlib import Path
from typing import BinaryIO

from app import media
from app.config import SUPPORTED_EXTENSIONS, Settings
from app.errors import ApiError
from app.models import SourceVideo
from app.validation import InvalidRequest, check_source_file, resolve_source

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
# Browsers will play H.264 in a QuickTime container if it is labelled as MP4.
_PREVIEW_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}


class SourceLibrary:
    def __init__(self, settings: Settings):
        self._settings = settings

    def list_sources(self) -> list[SourceVideo]:
        files = [
            p for p in self._settings.input_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]  # fmt: skip
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [SourceVideo(name=p.name, size_bytes=p.stat().st_size) for p in files]

    def describe(self, name: str) -> SourceVideo:
        path = self.path_for(name)
        try:
            info = media.probe(path, self._settings.ffprobe_bin, self._settings.probe_timeout_s)
        except media.ProbeError as exc:
            raise ApiError(422, "INVALID_SOURCE", str(exc)) from exc
        return self._to_model(path, info)

    def path_for(self, name: str) -> Path:
        try:
            path = resolve_source(self._settings.input_dir, name)
            check_source_file(path, name)
        except InvalidRequest as exc:
            raise ApiError(404 if exc.code == "SOURCE_NOT_FOUND" else 422, exc.code, exc.message) from exc
        return path

    def preview_type(self, path: Path) -> str:
        return _PREVIEW_TYPES.get(path.suffix.lower(), "application/octet-stream")

    def save_upload(self, filename: str | None, stream: BinaryIO) -> SourceVideo:
        """Stream an upload to disk, confirm it is a readable video, then publish it under a unique name."""
        stem, suffix = _safe_name(filename or "")
        if suffix not in SUPPORTED_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
            raise ApiError(422, "UNSUPPORTED_FORMAT", f"'{suffix or filename}' is not supported; use one of {supported}")

        limit = self._settings.max_upload_mb * 1024 * 1024
        tmp = self._settings.input_dir / f".upload-{uuid.uuid4().hex}{suffix}"
        try:
            written = 0
            with tmp.open("wb") as out:
                while chunk := stream.read(_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise ApiError(413, "UPLOAD_TOO_LARGE", f"Uploads are limited to {self._settings.max_upload_mb} MB")
                    out.write(chunk)
            if written == 0:
                raise ApiError(422, "INVALID_SOURCE", "Uploaded file is empty")
            try:
                info = media.probe(tmp, self._settings.ffprobe_bin, self._settings.probe_timeout_s)
            except media.ProbeError as exc:
                raise ApiError(422, "INVALID_SOURCE", str(exc)) from exc
            final = self._publish(tmp, stem, suffix)
        finally:
            tmp.unlink(missing_ok=True)

        logger.info("source.uploaded", extra={"source_video": final.name, "size_bytes": written})
        return self._to_model(final, info)

    def _publish(self, tmp: Path, stem: str, suffix: str) -> Path:
        # os.link fails if the name is taken, so two uploads of the same name can't overwrite each other.
        for i in range(1000):
            final = self._settings.input_dir / (f"{stem}{suffix}" if i == 0 else f"{stem}-{i}{suffix}")
            try:
                os.link(tmp, final)
                return final
            except FileExistsError:
                continue
        raise ApiError(409, "NAME_UNAVAILABLE", "Could not find a free file name for the upload")

    @staticmethod
    def _to_model(path: Path, info: media.VideoInfo) -> SourceVideo:
        return SourceVideo(
            name=path.name, size_bytes=path.stat().st_size,
            width=info.width, height=info.height, duration_seconds=info.duration_s,
        )  # fmt: skip


def _safe_name(filename: str) -> tuple[str, str]:
    """Reduce a client-supplied file name to [A-Za-z0-9._-], with no path components."""
    base = Path(filename.replace("\\", "/")).name
    suffix = Path(base).suffix.lower()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(base).stem).strip("-.")[:100] or "video"
    return stem, suffix
