"""Adopt finished downloads into Liner's local library.

Liner (``liner-bin``) keeps a local store under ``<userData>/local-tracks``::

    audio/<sha1-20>.<ext>     the audio itself, named by content hash
    covers/<sha1-20>.<ext>    embedded artwork, same id
    library.json              the index the UI reads

Downloading *into* that folder is not enough - the UI only lists what
``library.json`` names. This module renames the finished file to its content id
and appends the index record, which is what Liner's own
``local-tracks:import`` handler does (see ``Rx`` in its main bundle).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("mdownload.liner")

SUPPORTED = ("mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "webm")
MIME = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4",
    "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg",
    "wav": "audio/wav", "webm": "audio/webm",
}
COVER_EXT = {"mjpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif"}
_TRACK_NO = re.compile(r"^\d{1,3}[\s._-]+")
LOCK_NAME = "SingletonLock"  # Electron's instance lock, lives beside local-tracks

_lock = threading.Lock()


def store(dest: str | Path) -> Path | None:
    """The Liner library this destination feeds, or None."""
    root = Path(dest).expanduser()
    if (root / "library.json").is_file() and (root / "audio").is_dir():
        return root
    if root.name == "local-tracks" and (root / "audio").is_dir():
        return root
    return None


def adopt(path: str | Path, dest: str | Path, move: bool = True) -> Path | None:
    """File the finished download into Liner's store; return its new path.

    Returns None when the destination is not a Liner library or the file is not
    something Liner can play, so the caller keeps the path it already had.
    """
    p = Path(path)
    root = store(dest)
    if root is None or not p.is_file():
        return None
    if p.suffix.lower().lstrip(".") not in SUPPORTED:
        return None
    try:
        return _adopt(p, root, move)
    except Exception as exc:  # noqa: BLE001 - a failed import must not fail the job
        log.warning("liner: import of %s failed: %s", p.name, exc)
        return None


def adopt_tree(src: str | Path, dest: str | Path) -> tuple[int, int, int]:
    """Adopt every supported file in ``src``. Returns (added, duplicates, failed)."""
    root = store(dest)
    if root is None:
        raise RuntimeError(f"not a Liner library: {dest}")
    added = dupes = failed = 0
    for p in sorted(Path(src).expanduser().iterdir()):
        if not p.is_file() or p.suffix.lower().lstrip(".") not in SUPPORTED:
            continue
        before = (root / "library.json").stat().st_mtime_ns
        out = adopt(p, root)
        if out is None:
            failed += 1
        elif not p.exists():
            # moved, or dropped because the bytes were already in the library
            if (root / "library.json").stat().st_mtime_ns != before:
                added += 1
            else:
                dupes += 1
    return added, dupes, failed


# ------------------------------------------------------------- running app
def app_pid(root: str | Path) -> int | None:
    """PID of the running Liner main process, read from its instance lock."""
    try:
        target = os.readlink(Path(root).expanduser().parent / LOCK_NAME)
        host, _, pid = target.rpartition("-")
    except OSError:
        return None
    if host != socket.gethostname() or not pid.isdigit():
        return None
    return int(pid) if _alive(int(pid)) else None


def refresh_app(root: str | Path, since: float, timeout: float = 20.0) -> bool:
    """Restart a running Liner so it re-reads ``library.json``; True if it did.

    Liner loads the index once per process, so rows added underneath it stay
    invisible until the main process restarts. Nothing happens when Liner is
    not running (the next start reads the file anyway) or when it started after
    ``since`` - that instance already has the rows. Callers repeat this during a
    batch; the caller owns the cadence.
    """
    root = Path(root).expanduser()
    pid = app_pid(root)
    if pid is None or _started(pid) >= since:
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False  # a headless relaunch would take the instance lock
    try:
        before = _index(root)
        exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return False

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.5)
    after = _index(root)
    kept = {r.get("id") for r in after}
    lost = [r for r in before if r.get("id") not in kept]
    if lost:  # a quit that saved a stale cache must not eat the new rows
        _save(root, after + lost)
        log.warning("liner: quit dropped %d records, merged back", len(lost))

    subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     cwd=str(Path.home()))
    log.info("liner: restarted so %d tracks show up", len(before))
    return True


# ------------------------------------------------------------------ internals
def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _started(pid: int) -> float:
    """Epoch seconds when the process started; 0.0 when it cannot be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])  # field 22
        btime = next(int(line.split()[1]) for line in
                     Path("/proc/stat").read_text().splitlines()
                     if line.startswith("btime "))
    except (OSError, ValueError, IndexError, StopIteration):
        return 0.0
    return btime + ticks / os.sysconf("SC_CLK_TCK")


# ------------------------------------------------------------------ internals
def _adopt(src: Path, root: Path, move: bool) -> Path:
    ext = src.suffix.lower().lstrip(".")
    ident = _content_id(src)
    with _lock:
        rows = _index(root)
        audio = root / "audio"
        audio.mkdir(parents=True, exist_ok=True)
        target = audio / f"{ident}.{ext}"

        same = next((r for r in rows if r.get("id") == ident), None)
        if same is not None:
            old = audio / str(same.get("audioFile") or "")
            if old.is_file():
                if move:
                    src.unlink(missing_ok=True)
                log.info("liner: %s already in library ", src.name)
                return old
            rows = [r for r in rows if r is not same]  # index pointed at a gone file

        if move:
            os.replace(src, target)
        else:
            shutil.copyfile(src, target)

        meta = _probe(target)
        cover_codec = meta.pop("cover", "")
        cover_file = ""
        if cover_codec:
            cdir = root / "covers"
            cdir.mkdir(parents=True, exist_ok=True)
            cext = COVER_EXT.get(cover_codec, "jpg")
            name = f"{ident}.{cext}"
            try:
                _extract_cover(target, cdir / name, cover_codec if cover_codec in COVER_EXT else "")
                if (cdir / name).stat().st_size:
                    cover_file = name
                else:
                    (cdir / name).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("liner: cover for %s failed: %s", target.name, exc)

        row = {
            "id": ident,
            "title": meta["title"],
            "artist": meta["artist"],
        }
        if meta.get("album"):
            row["album"] = meta["album"]
        row.update({
            "durationMs": meta["durationMs"],
            "fileName": src.name,
            "audioFile": target.name,
            "mimeType": meta["mimeType"],
        })
        if cover_file:
            row["coverFile"] = cover_file
        row.update({"byteSize": target.stat().st_size,
                    "addedAt": int(time.time() * 1000)})
        rows.append(row)
        _save(root, rows)
        return target


def _index(root: Path) -> list[dict]:
    path = root / "library.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(root: Path, rows: list[dict]) -> None:
    path = root / "library.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(rows), encoding="utf-8")
    tmp.replace(path)


def _content_id(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:20]


def _run(argv: list[str]) -> str:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "").strip()[:300] or "ffmpeg failed")
    return out.stdout


def _from_name(path: Path) -> tuple[str, str]:
    """Liner's filename fallback: strip a leading track number, split on ' - '."""
    stem = path.stem.strip()
    clean = _TRACK_NO.sub("", stem).strip() or stem
    cut = clean.find(" - ")
    if cut > 0:
        return clean[cut + 3:].strip(), clean[:cut].strip()
    return clean, ""


def _probe(path: Path) -> dict:
    ext = path.suffix.lower().lstrip(".")
    title, artist = _from_name(path)
    info = {"title": title, "artist": artist, "durationMs": 0,
            "mimeType": MIME.get(ext, "application/octet-stream")}
    try:
        data = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json",
                                "-show_format", "-show_streams", str(path)]))
    except Exception as exc:  # noqa: BLE001 - unreadable tags just fall back
        log.info("liner: no tags in %s (%s)", path.name, exc)
        return info
    fmt = data.get("format") or {}
    tags = {str(k).lower(): str(v).strip()
            for k, v in (fmt.get("tags") or {}).items()}
    if tags.get("title"):
        info["title"] = tags["title"]
    if tags.get("artist") or tags.get("album_artist"):
        info["artist"] = tags.get("artist") or tags.get("album_artist")
    if tags.get("album"):
        info["album"] = tags["album"]
    try:
        info["durationMs"] = round(float(fmt.get("duration") or 0) * 1000)
    except (TypeError, ValueError):
        pass
    for st in data.get("streams") or []:
        if st.get("codec_type") == "video" and \
                ((st.get("disposition") or {}).get("attached_pic") or 0) == 1:
            info["cover"] = str(st.get("codec_name") or "")
            break
    return info


def _extract_cover(src: Path, dest: Path, codec: str) -> None:
    if codec:
        try:
            _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
                  "-frames:v", "1", "-c", "copy", str(dest)])
            if dest.is_file() and dest.stat().st_size:
                return
        except Exception:  # noqa: BLE001 - fall through to a transcode
            pass
        dest = dest.with_suffix(".jpg")
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
          "-frames:v", "1", "-q:v", "3", str(dest)])