"""Job queue, worker pool and yt-dlp progress plumbing."""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import engine, liner
from .config import Config
from .history import History
from .library import Library, tokens

log = logging.getLogger("mdownload.downloader")

ACTIVE = {"queued", "resolving", "running", "paused", "processing"}
FINISHED = {"done", "error", "cancelled", "skipped"}

_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def youtube_id(url: str) -> str:
    m = _YT_ID.search(url or "")
    return m.group(1) if m else ""


class Cancelled(Exception):
    """Raised from inside a yt-dlp hook to abort a job."""


def _job_key(url: str, track, title: str) -> str:
    """Identity for duplicate suppression: a track id, else the URL itself."""
    if track is not None:
        key = getattr(track, "key", "")
        return f"id:{key}" if key else "t:" + " ".join(sorted(tokens(title)))
    return "u:" + (url or title).strip().lower()


@dataclass
class Job:
    id: str
    url: str = ""
    title: str = ""
    source: str = "url"
    dest: str = ""
    spec: dict = field(default_factory=dict)
    track: object | None = None
    status: str = "queued"
    progress: float = 0.0
    speed: float | None = None
    eta: int | None = None
    downloaded: int = 0
    total: int | None = None
    frag_index: int | None = None
    frag_count: int | None = None
    filename: str = ""
    error: str = ""
    exit_code: int | None = None
    started: float | None = None
    finished: float | None = None


class Control:
    __slots__ = ("pause", "cancel")

    def __init__(self) -> None:
        self.pause = threading.Event()
        self.cancel = threading.Event()


def _rate_bytes(text: str) -> int | None:
    t = (text or "").strip().upper()
    if not t:
        return None
    mult = 1
    for suffix, m in (("K", 1024), ("M", 1024 ** 2), ("G", 1024 ** 3)):
        if t.endswith(suffix):
            t, mult = t[:-1], m
            break
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


def _cancelled(exc: BaseException) -> bool:
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        if isinstance(e, Cancelled):
            return True
        seen.add(id(e))
        e = e.__cause__ or e.__context__
    return False


JS_RUNTIMES = ("deno", "node", "bun", "qjs", "quickjs")

_js_cache: dict | None = None


def js_runtimes() -> dict:
    """The JS runtime yt-dlp needs to solve YouTube's signature challenges.

    yt-dlp only enables deno by default; without one of these it decodes no
    video formats at all ("Requested format is not available").
    """
    global _js_cache
    if _js_cache is None:
        import shutil

        found = next((n for n in JS_RUNTIMES if shutil.which(n)), None)
        _js_cache = {found: {}} if found else {}
    return _js_cache


def human(n: int | float | None) -> str:
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return "-"


def clock(seconds: float | None) -> str:
    if not seconds:
        return "-"
    s = int(seconds)
    return (f"{s // 60}:{s % 60:02d}" if s < 3600
            else f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}")


class DownloadManager:
    def __init__(self, cfg: Config, history: History | None = None) -> None:
        self.cfg = cfg
        self.history = history or History()
        self._jobs: dict[str, Job] = {}
        self._ctl: dict[str, Control] = {}
        self._order: list[str] = []
        self._libs: dict[str, Library] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(cfg.concurrent)))
        self._yt = None
        self._yt_lock = threading.Lock()
        self._closing = False
        self._liner_lock = threading.Lock()

    # ---------------------------------------------------------------- pool
    def _yt_client(self):
        if self._yt is None:
            with self._yt_lock:
                if self._yt is None:
                    from .sources import yt_client
                    self._yt = yt_client(self.cfg)
        return self._yt

    def reconfig(self) -> None:
        n = max(1, int(self.cfg.concurrent))
        with self._lock:
            if n == self._pool._max_workers or self._closing:
                return
            old, self._pool = self._pool, ThreadPoolExecutor(max_workers=n)
            old.shutdown(wait=False, cancel_futures=False)

    def _snapshot(self) -> dict:
        c = self.cfg
        return {
            "format": c.format, "quality": c.quality, "video_codec": c.video_codec,
            "audio_codec": c.audio_codec, "audio_bitrate": c.audio_bitrate,
            "subtitles": c.subtitles, "subtitle_langs": c.subtitle_langs,
            "embed_thumbnail": c.embed_thumbnail, "embed_metadata": c.embed_metadata,
            "embed_chapters": c.embed_chapters, "sponsorblock": c.sponsorblock,
            "cookies_file": c.cookies_file,
            "cookies_from_browser": c.cookies_from_browser,
            "rate_limit": c.rate_limit, "retries": c.retries, "proxy": c.proxy,
            "match_threshold": c.match_threshold,
        }

    # --------------------------------------------------------------- queue
    def library(self, dest: str = "") -> Library:
        root = str(Path(dest or self.cfg.dest_dir).expanduser())
        with self._lock:
            lib = self._libs.get(root)
            if lib is None:
                lib = self._libs[root] = Library(root)
            return lib

    def add(self, url: str = "", title: str = "", track=None,
            source: str = "url", spec: dict | None = None) -> Job | None:
        """Queue one download.

        Returns None when the same track is already queued or already sitting in
        the destination folder - that is the duplicate the user asked not to get.
        """
        label = title or url or str(track or "queued")
        key = _job_key(url, track, label)
        lib = self.library()
        with self._lock:
            for jid in self._order:
                other = self._jobs[jid]
                if other.status in ACTIVE and \
                        _job_key(other.url, other.track, other.title) == key:
                    log.info("skipped (already queued): %s", label[:80])
                    return None
        if not label.startswith("http"):
            hit = lib.match(label)
            if hit is not None:
                log.info("skipped (in %s): %s -> %s", lib.root, label[:80], hit.name)
                return None
        job = Job(
            id=uuid.uuid4().hex[:8],
            url=url,
            title=label,
            source=source,
            dest=str(Path(self.cfg.dest_dir).expanduser()),
            spec=spec or self._snapshot(),
            track=track,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._ctl[job.id] = Control()
        self._submit(job)
        return job

    def _submit(self, job: Job) -> None:
        try:
            self._pool.submit(self._run, job)
        except RuntimeError:
            job.status = "error"
            job.error = "worker pool is shutting down"

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def active(self) -> int:
        return sum(1 for j in self.snapshot() if j.status in ACTIVE)

    # ------------------------------------------------------------- control
    def pause(self, job_id: str) -> None:
        job, ctl = self.get(job_id), self._ctl.get(job_id)
        if job and ctl:
            ctl.pause.set()
            if job.status in ("running", "processing", "resolving"):
                job.status = "paused"

    def resume(self, job_id: str) -> None:
        job, ctl = self.get(job_id), self._ctl.get(job_id)
        if job and ctl:
            ctl.pause.clear()
            if job.status == "paused":
                job.status = "running"

    def cancel(self, job_id: str) -> None:
        job, ctl = self.get(job_id), self._ctl.get(job_id)
        if not ctl:
            return
        ctl.cancel.set()
        ctl.pause.clear()
        if job and job.status in ("queued", "resolving"):
            job.status = "cancelled"
            job.finished = job.finished or time.time()

    def retry(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status not in ("error", "cancelled", "skipped"):
                return False
            if job.status == "skipped":
                # an explicit retry overrides the duplicate guard
                job.spec["force"] = True
            job.status = "queued"
            job.error = ""
            job.exit_code = None
            job.progress = 0.0
            job.speed = None
            job.eta = None
            job.downloaded = 0
            job.total = None
            job.frag_index = job.frag_count = None
            job.started = job.finished = None
            self._ctl[job.id] = Control()
        self._submit(job)
        return True

    def pause_all(self) -> None:
        for j in self.snapshot():
            if j.status in ACTIVE:
                self.pause(j.id)

    def resume_all(self) -> None:
        for j in self.snapshot():
            if j.status == "paused":
                self.resume(j.id)

    def clear_finished(self) -> int:
        with self._lock:
            done = [j for j in self._order if self._jobs[j].status in FINISHED]
            for jid in done:
                self._jobs.pop(jid, None)
                self._ctl.pop(jid, None)
            self._order = [j for j in self._order if j in self._jobs]
        return len(done)

    def retry_failed(self) -> int:
        return sum(1 for j in self.snapshot() if j.status == "error" and self.retry(j.id))

    def remove(self, job_id: str) -> None:
        self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
            self._ctl.pop(job_id, None)
            self._order = [j for j in self._order if j != job_id]

    def shutdown(self) -> None:
        self._closing = True
        for j in self.snapshot():
            if j.status in ACTIVE:
                ctl = self._ctl.get(j.id)
                if ctl:
                    ctl.cancel.set()
                    ctl.pause.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def join(self) -> None:
        """Block until every submitted job has finished and been recorded."""
        self._pool.shutdown(wait=True)

    # ------------------------------------------------------------- workers
    def _run(self, job: Job) -> None:
        ctl = self._ctl.get(job.id)
        if ctl is None or ctl.cancel.is_set():
            job.status = "cancelled"
            return
        job.started = time.time()
        lib = self.library(job.dest)
        force = bool(job.spec.get("force"))
        adopted = 0.0
        try:
            url = job.url
            vid = youtube_id(url)
            if job.track is not None and not force:
                hit = lib.match(str(job.track))
                if hit is not None:
                    job.status = "skipped"
                    job.error = f"already have it: {hit.name}"
                    return
            if job.track is not None:
                job.status = "resolving"
                vid, score = engine.resolve(
                    job.track, self._yt_client(),
                    float(job.spec.get("match_threshold", 0.55)), log)
                if not vid:
                    raise RuntimeError(f"no YouTube match (best score {score:.2f})")
                url = f"https://music.youtube.com/watch?v={vid}"
                job.url = url
            if not url:
                raise RuntimeError("empty URL")
            if vid and not force and lib.has_id(vid):
                job.status = "skipped"
                job.error = "already downloaded"
                return
            job.status = "running"
            import yt_dlp
            with yt_dlp.YoutubeDL(self._opts(job, ctl)) as ydl:
                ydl.download([url])
            job.status = "done"
            job.progress = 1.0
            # a destination that is a Liner library gets the file filed under
            # its content id plus an index record, or Liner never lists it
            final = liner.adopt(job.filename, job.dest or self.cfg.dest_dir)
            if final is not None:
                job.filename = str(final)
                adopted = time.time()
            if vid:
                lib.note_id(vid, job.filename)
            lib.add_file(job.filename)
        except BaseException as exc:  # noqa: BLE001 - surfaced in the UI
            if _cancelled(exc) or ctl.cancel.is_set():
                job.status = "cancelled"
            else:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"[:500]
                job.exit_code = 1
            log.info("job %s %s: %s", job.id, job.status, job.error)
        finally:
            job.finished = time.time()
            self._record(job)
            if adopted:
                self._refresh_liner(job.dest or self.cfg.dest_dir, adopted)

    def _refresh_liner(self, dest: str, since: float) -> None:
        """Restart Liner once the queue is empty so the new rows are listed."""
        with self._liner_lock:
            if self.active():
                return
            root = liner.store(dest)
            if root is None:
                return
            try:
                liner.refresh_app(root, since)
            except Exception as exc:  # noqa: BLE001 - never fail a job over this
                log.warning("liner refresh failed: %s", exc)

    def _record(self, job: Job) -> None:
        if job.status not in FINISHED or job.status == "skipped":
            return
        try:
            self.history.add(title=job.title, url=job.url, dest=job.dest,
                             fmt=job.spec.get("format", ""), status=job.status,
                             size=job.total or job.downloaded or 0,
                             error=job.error, finished_at=job.finished)
        except Exception as exc:  # noqa: BLE001
            log.warning("history write failed: %s", exc)

    def _hook(self, job: Job, ctl: Control):
        def hook(d: dict) -> None:
            if ctl.cancel.is_set():
                raise Cancelled()
            while ctl.pause.is_set():
                if ctl.cancel.is_set():
                    raise Cancelled()
                time.sleep(0.15)
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                got = d.get("downloaded_bytes") or 0
                job.downloaded = got
                job.total = total
                job.progress = (got / total) if total else 0.0
                job.speed = d.get("speed")
                job.eta = d.get("eta")
                job.frag_index = d.get("fragment_index")
                job.frag_count = d.get("fragment_count")
                job.filename = d.get("filename") or job.filename
            elif status == "finished":
                job.progress = 1.0
                job.filename = d.get("filename") or job.filename
        return hook

    def _pp_hook(self, job: Job):
        def hook(d: dict) -> None:
            if d.get("status") == "started":
                job.status = "processing"
            elif d.get("status") == "finished":
                # after FFmpegExtractAudio this is the real output (.mp3), not
                # the intermediate the download hook reported
                info = d.get("info_dict") or {}
                job.filename = info.get("filepath") or job.filename
                if job.status == "processing":
                    job.status = "running"
        return hook

    def _opts(self, job: Job, ctl: Control) -> dict:
        s = job.spec
        dest = Path(job.dest or self.cfg.dest_dir).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        opts: dict = {
            "outtmpl": str(dest / "%(title).180s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            "retries": int(s.get("retries", 3)),
            "fragment_retries": int(s.get("retries", 3)),
            "progress_hooks": [self._hook(job, ctl)],
            "postprocessor_hooks": [self._pp_hook(job)],
        }
        runtimes = js_runtimes()
        if runtimes:
            opts["js_runtimes"] = runtimes
        pps: list[dict] = []
        fmt = s.get("format", "video+audio")
        if fmt == "audio-only":
            opts["format"] = "bestaudio/best"
            pps.append({"key": "FFmpegExtractAudio",
                        "preferredcodec": s.get("audio_codec", "mp3"),
                        "preferredquality": str(s.get("audio_bitrate", "192"))})
        else:
            height = s.get("quality", "best")
            hs = "" if height in ("", "best") else f"[height<={height}]"
            codec = s.get("video_codec", "any")
            vs = "" if codec in ("", "any") else f"[vcodec^={codec}]"
            if fmt == "video-only":
                opts["format"] = f"bestvideo{hs}{vs}/bestvideo{hs}/best"
            else:
                opts["format"] = (f"bestvideo{hs}{vs}+bestaudio/"
                                  f"bestvideo{hs}+bestaudio/best{hs}/best")
                opts["merge_output_format"] = "mkv"
        if s.get("embed_metadata") or s.get("embed_chapters"):
            pps.append({"key": "FFmpegMetadata",
                        "add_metadata": bool(s.get("embed_metadata"))})
            if s.get("embed_chapters"):
                opts["embedchapters"] = True
        if s.get("embed_thumbnail"):
            opts["writethumbnail"] = True
            pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        if s.get("sponsorblock"):
            cats = {"sponsor", "selfpromo", "interaction", "intro", "outro"}
            # SponsorBlockPP only fetches the segments; ModifyChapters cuts them,
            # and must run before FFmpegMetadata or they are re-added.
            pps.insert(0, {"key": "SponsorBlock", "categories": cats,
                           "when": "after_filter"})
            cut = next((i for i, p in enumerate(pps) if p["key"] == "FFmpegMetadata"),
                       len(pps))
            pps.insert(cut, {"key": "ModifyChapters", "remove_sponsor_segments": cats})
        if s.get("subtitles"):
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitlesformat"] = "srt/best"
            langs = [x.strip() for x in str(s.get("subtitle_langs", "en")).split(",")]
            opts["subtitleslangs"] = [x for x in langs if x] or ["en"]
        if pps:
            opts["postprocessors"] = pps
        rate = _rate_bytes(str(s.get("rate_limit", "")))
        if rate:
            opts["ratelimit"] = rate
        proxy = str(s.get("proxy", "")).strip()
        if proxy:
            opts["proxy"] = proxy
        cookies_file = str(s.get("cookies_file", "")).strip()
        if cookies_file:
            jar = Path(cookies_file).expanduser()
            if jar.exists():
                opts["cookiefile"] = str(jar)
        browser = str(s.get("cookies_from_browser", "")).strip()
        if browser:
            opts["cookiesfrombrowser"] = (browser, None, None, None)
        return opts

    # ---------------------------------------------------------- expansion
    @staticmethod
    def expand(url: str) -> list[tuple[str, str]]:
        """Return (url, title) pairs; a playlist expands to one pair per item."""
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": "in_playlist"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return []
        entries = info.get("entries")
        if not entries:
            return [(url, info.get("title") or url)]
        pairs: list[tuple[str, str]] = []
        for e in entries:
            if not e:
                continue
            link = e.get("url") or e.get("webpage_url") or ""
            if not link.startswith("http") and e.get("id"):
                link = f"https://www.youtube.com/watch?v={e['id']}"
            if link:
                pairs.append((link, e.get("title") or link))
        return pairs