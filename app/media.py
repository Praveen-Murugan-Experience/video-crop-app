"""Thin wrappers around ffprobe (inspect) and ffmpeg (crop)."""

import json
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from app.models import Crop


@dataclass(frozen=True)
class VideoInfo:
    width: int  # as displayed, i.e. after applying rotation metadata
    height: int
    duration_s: float | None
    audio_codec: str | None
    rotation: int


class ProbeError(Exception):
    pass


class FfmpegError(Exception):
    def __init__(self, returncode: int, stderr_tail: str):
        super().__init__(f"ffmpeg exited with code {returncode}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class CropTimeout(Exception):
    pass


class CropInterrupted(Exception):
    """Raised when should_stop() asks ffmpeg to stop; reason is 'cancelled' or 'shutdown'."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def probe(path: Path, ffprobe_bin: str = "ffprobe", timeout_s: float = 30.0) -> VideoInfo:
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("Timed out reading the source video") from exc
    if result.returncode != 0:
        reason = (result.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise ProbeError(f"File is not a readable video: {reason}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    if video is None:
        raise ProbeError("File contains no video stream")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ProbeError("Video stream has no usable dimensions")

    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return VideoInfo(
        width=width,
        height=height,
        duration_s=_float(data.get("format", {}).get("duration")) or _float(video.get("duration")),
        audio_codec=audio.get("codec_name") if audio else None,
        rotation=rotation,
    )


def build_crop_command(ffmpeg_bin: str, src: Path, dst: Path, crop: Crop, out_w: int, out_h: int, audio_codec: str | None) -> list[str]:
    # ffmpeg auto-rotates before filters run, so the crop applies to the displayed frame.
    # exact=1 stops ffmpeg rounding odd left/top offsets to the chroma grid.
    return [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"crop={out_w}:{out_h}:{crop.left}:{crop.top}:exact=1",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "copy" if audio_codec == "aac" else "aac",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        "-f", "mp4", str(dst),
    ]  # fmt: skip


def run_ffmpeg(
    cmd: list[str],
    *,
    duration_s: float | None,
    timeout_s: float,
    on_progress: Callable[[float], None],
    should_stop: Callable[[], str | None],
    check_interval_s: float = 0.5,
) -> None:
    """Run ffmpeg to completion, reporting progress and honouring stop requests and the timeout."""
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
        # Progress is read on its own thread so a silent ffmpeg can't block the cancel/timeout checks.
        reader = threading.Thread(target=_read_progress, args=(proc.stdout, duration_s, on_progress), daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                try:
                    returncode = proc.wait(timeout=check_interval_s)
                    break
                except subprocess.TimeoutExpired:
                    pass
                reason = should_stop()
                if reason:
                    raise CropInterrupted(reason)
                if time.monotonic() > deadline:
                    raise CropTimeout(f"Processing exceeded {timeout_s:.0f}s")
        finally:
            if proc.poll() is None:
                _kill(proc)
            reader.join(timeout=2)
        if returncode != 0:
            raise FfmpegError(returncode, _tail(stderr_file))


def _read_progress(stream: IO[str], duration_s: float | None, on_progress: Callable[[float], None]) -> None:
    for line in stream:
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and duration_s and value.isdigit():
            on_progress(min(99.0, int(value) / 1_000_000 / duration_s * 100))


def _kill(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _tail(stderr_file: IO[bytes], lines: int = 15) -> str:
    stderr_file.seek(0)
    return "\n".join(stderr_file.read().decode("utf-8", errors="replace").strip().splitlines()[-lines:])


def _rotation(stream: dict) -> int:
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            return int(float(side_data["rotation"])) % 360
    return int(float(stream.get("tags", {}).get("rotate", 0))) % 360


def _float(value) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
