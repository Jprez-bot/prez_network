"""Low-level media analysis with FFmpeg / ffprobe / OpenCV.

Everything here reads the original file and never writes to it. Long
recordings (2-6 h) are processed as streams: audio is decoded at 8 kHz mono
and scene detection runs on a downscaled 2 fps proxy, so memory use stays flat.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from . import config

AUDIO_RATE = 8000
ENERGY_HOP = 0.5  # seconds per loudness sample


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-1500:]
        raise MediaError(f"{cmd[0]} failed ({proc.returncode}): {tail}")
    return proc


def probe(path: str | Path) -> dict:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                "-show_streams", str(path)], timeout=120)
    info = json.loads(proc.stdout or b"{}")
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = 0.0
    if v:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
        try:
            fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        except ValueError:
            fps = 0.0
    duration = float(fmt.get("duration") or (v or {}).get("duration") or 0)
    width = int(v.get("width", 0)) if v else 0
    height = int(v.get("height", 0)) if v else 0
    rotation = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])
    if abs(rotation) in (90, 270):
        width, height = height, width
    return {
        "duration": duration, "width": width, "height": height, "fps": round(fps, 3),
        "has_audio": a is not None, "has_video": v is not None,
        "video_codec": (v or {}).get("codec_name"), "audio_codec": (a or {}).get("codec_name"),
        "size": int(fmt.get("size") or 0), "format": fmt.get("format_name"),
    }


def audio_energy(path: str | Path, duration: float | None = None) -> dict:
    """Return a loudness curve (dBFS per 0.5 s), plus silence spans.

    Streams PCM from ffmpeg in chunks so multi-hour files never sit in memory.
    """
    hop = int(AUDIO_RATE * ENERGY_HOP)
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar",
           str(AUDIO_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    db_values: list[float] = []
    peak_values: list[float] = []
    leftover = np.zeros(0, dtype=np.float32)
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(hop * 2 * 240)  # ~2 minutes per read
        if not chunk:
            break
        samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype="<i2").astype(np.float32) / 32768.0
        samples = np.concatenate([leftover, samples])
        n = len(samples) // hop
        if n:
            frames = samples[: n * hop].reshape(n, hop)
            rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
            db_values.extend((20 * np.log10(rms)).tolist())
            peak_values.extend(np.max(np.abs(frames), axis=1).tolist())
        leftover = samples[n * hop:]
    proc.wait()
    if len(leftover) > hop // 4:
        rms = float(np.sqrt(np.mean(leftover ** 2) + 1e-12))
        db_values.append(20 * np.log10(rms))
        peak_values.append(float(np.max(np.abs(leftover))))
    if not db_values:
        return {"hop": ENERGY_HOP, "db": [], "peak": [], "silences": [], "stats": {}}
    db = np.array(db_values)
    floor = float(np.percentile(db, 10))
    median = float(np.median(db))
    silence_thr = max(floor + 6, -50.0)
    silences = _spans(db < silence_thr, ENERGY_HOP, min_len=0.6)
    return {
        "hop": ENERGY_HOP,
        "db": [round(x, 2) for x in db.tolist()],
        "peak": [round(x, 3) for x in peak_values],
        "silences": silences,
        "stats": {
            "median_db": round(median, 2), "floor_db": round(floor, 2),
            "p95_db": round(float(np.percentile(db, 95)), 2),
            "silence_threshold_db": round(silence_thr, 2),
            "silence_ratio": round(float(np.mean(db < silence_thr)), 3),
        },
    }


def _spans(mask: np.ndarray, hop: float, min_len: float) -> list[list[float]]:
    spans = []
    start = None
    for i, m in enumerate(mask.tolist() + [False]):
        if m and start is None:
            start = i
        elif not m and start is not None:
            if (i - start) * hop >= min_len:
                spans.append([round(start * hop, 2), round(i * hop, 2)])
            start = None
    return spans


def scene_changes(path: str | Path, threshold: float = 0.32, fps: float = 2.0,
                  keyframes_only: bool | None = None) -> list[float]:
    """Timestamps of hard cuts / big visual changes, on a low-res 2 fps proxy.

    For long recordings (> 30 min) only keyframes are decoded, which is many
    times faster and still catches scene/game changes for chaptering.
    """
    if keyframes_only is None:
        try:
            keyframes_only = probe(path)["duration"] > 1800
        except MediaError:
            keyframes_only = False
    pre = ["-skip_frame", "nokey"] if keyframes_only else []
    vf = (f"scale=160:-2,select='gt(scene,{threshold})',showinfo" if keyframes_only
          else f"fps={fps},scale=160:-2,select='gt(scene,{threshold})',showinfo")
    cmd = ["ffmpeg", "-v", "info", *pre, "-i", str(path), "-an", "-vf", vf, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    times = []
    for line in proc.stderr.decode(errors="replace").splitlines():
        if "showinfo" in line and "pts_time:" in line:
            try:
                times.append(round(float(line.split("pts_time:")[1].split()[0]), 2))
            except (IndexError, ValueError):
                continue
    return times


def extract_frame(path: str | Path, t: float, out: str | Path, width: int | None = None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = ["-vf", f"scale={width}:-2"] if width else []
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(path),
         "-frames:v", "1", *vf, "-q:v", "2", str(out)], timeout=120)
    return out


def read_frames(path: str | Path, times: list[float], width: int = 640) -> list[tuple[float, np.ndarray]]:
    """Decode individual frames as BGR arrays (for face detection / scoring)."""
    import cv2
    frames = []
    cache = config.CACHE_DIR / "frames_tmp"
    cache.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(times):
        tmp = cache / f"f_{abs(hash((str(path), t)))}.jpg"
        try:
            extract_frame(path, t, tmp, width=width)
            img = cv2.imread(str(tmp))
            if img is not None:
                frames.append((t, img))
        except MediaError:
            continue
        finally:
            tmp.unlink(missing_ok=True)
    return frames


_face_cascade = None


def _cascade():
    global _face_cascade
    if _face_cascade is None:
        import cv2
        _face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _face_cascade


def detect_faces(img: np.ndarray) -> list[list[float]]:
    """Faces as normalized [x, y, w, h] boxes, largest first."""
    import cv2
    h, w = img.shape[:2]
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    min_side = max(24, int(min(w, h) * 0.05))
    found = _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6,
                                        minSize=(min_side, min_side))
    boxes = [[x / w, y / h, bw / w, bh / h] for (x, y, bw, bh) in found]
    boxes.sort(key=lambda b: b[2] * b[3], reverse=True)
    return [[round(v, 4) for v in b] for b in boxes]


def face_track(path: str | Path, start: float, end: float, samples: int = 8) -> dict:
    """Sample frames between start and end and summarise where the face is.

    Returns the median face box and how consistently it was found, which the
    framing engine uses to decide between crop / split-screen / picture-in-picture.
    """
    if end <= start:
        end = start + 1
    times = [start + (end - start) * (i + 0.5) / samples for i in range(samples)]
    boxes = []
    sharp = []
    import cv2
    for t, img in read_frames(path, times, width=960):
        faces = detect_faces(img)
        if faces:
            boxes.append(faces[0])
        sharp.append(float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
    if not boxes:
        return {"found_ratio": 0.0, "box": None, "samples": samples}
    arr = np.array(boxes)
    med = np.median(arr, axis=0).tolist()
    return {
        "found_ratio": round(len(boxes) / max(1, samples), 2),
        "box": [round(v, 4) for v in med],
        "spread": round(float(np.std(arr[:, 0] + arr[:, 2] / 2)), 4),
        "samples": samples,
    }


def frame_quality(img: np.ndarray) -> dict:
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return {
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "brightness": float(gray.mean()),
        "contrast": float(gray.std()),
        "saturation": float(hsv[:, :, 1].mean()),
    }


def energy_at(audio: dict, start: float, end: float) -> list[float]:
    hop = audio.get("hop", ENERGY_HOP) or ENERGY_HOP
    db = audio.get("db", [])
    a, b = int(start / hop), int(np.ceil(end / hop))
    return db[max(a, 0):max(b, a + 1)]
