"""Framed media from handysink; payload boundaries and source timestamps survive pipes."""

import hashlib
import os
import platform
import shlex
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct("!4sBBHQQIIII")
MAX_PAYLOAD = 16 * 1024 * 1024


@dataclass(frozen=True)
class MediaPacket:
    kind: int  # 1: interleaved S16LE audio, 2: JPEG
    pts_ns: int | None  # Unix clock, mapped from pipeline base time + buffer running time
    duration_ns: int
    rate: int
    channels: int
    sequence: int
    discontinuity: bool
    data: bytes


def read_packets(pipe):
    def exact(n, eof=False):
        result = bytearray()
        while len(result) < n:
            chunk = pipe.read(n - len(result))
            if not chunk:
                if eof and not result:
                    return None
                raise ValueError("truncated timestamped media packet")
            result.extend(chunk)
        return bytes(result)

    previous = None
    while header := exact(HEADER.size, eof=True):
        magic, version, kind, flags, pts, duration, size, rate, channels, seq = HEADER.unpack(header)
        if magic != b"HCAP" or version != 1 or kind not in (1, 2) or not 0 < size <= MAX_PAYLOAD:
            raise ValueError("invalid timestamped media header")
        if previous is not None and seq != (previous + 1) % (1 << 32):
            raise ValueError("media packet sequence gap")
        if kind == 1 and (not 1 <= rate <= 384000 or not 1 <= channels <= 8 or size % (2 * channels)):
            raise ValueError("invalid PCM frame layout")
        previous = seq
        yield MediaPacket(kind, pts if flags & 1 else None, duration, rate, channels, seq,
                          bool(flags & 2), exact(size))


def capture_plugin_env() -> dict:
    """Build a tiny sink against the installed GStreamer; never modify UxPlay itself."""
    source = Path(__file__).parent / "native" / "gsthandysink.c"
    try:
        version = subprocess.check_output(["pkg-config", "--modversion", "gstreamer-1.0"], text=True).strip()
        flags = shlex.split(subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "gstreamer-1.0", "gstreamer-base-1.0"], text=True))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("Timestamped capture needs GStreamer development files and pkg-config.") from exc
    key = hashlib.sha256(source.read_bytes() + (version + platform.platform()).encode()).hexdigest()[:16]
    directory = Path(__file__).resolve().parents[1] / "out" / "gstreamer" / key
    extension = ".dylib" if platform.system() == "Darwin" else ".so"
    target = directory / ("libgsthandysink" + extension)
    if not target.exists():
        compiler = shutil.which("cc")
        if not compiler:
            raise RuntimeError("Timestamped capture needs a C compiler (macOS: Xcode Command Line Tools).")
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            output = Path(temporary) / target.name
            command = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-fPIC",
                       "-dynamiclib" if extension == ".dylib" else "-shared", str(source), "-o", str(output), *flags]
            result = subprocess.run(command, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError("Cannot build timestamped capture sink: " + result.stderr[-2000:])
            output.replace(target)
    env = dict(os.environ)
    env["GST_PLUGIN_PATH"] = str(directory) + (
        os.pathsep + env["GST_PLUGIN_PATH"] if env.get("GST_PLUGIN_PATH") else "")
    env["GST_PLUGIN_PATH_1_0"] = str(directory) + (
        os.pathsep + env["GST_PLUGIN_PATH_1_0"] if env.get("GST_PLUGIN_PATH_1_0") else "")
    return env
