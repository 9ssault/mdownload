"""The Textual application.  Imported lazily so `mdownload --version` works
without textual installed."""
from __future__ import annotations

import json
import time
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import (Button, DataTable, Footer, Input, Label, RichLog, Select,
                             Static, Switch, TextArea)
from textual.widgets.data_table import RowKey

from . import config as config_mod
from .downloader import ACTIVE, DownloadManager, clock, human
from .history import History
from .screens import (BrowseScreen, ConfirmQuit, DestScreen, DupesScreen, HelpScreen,
                      HistoryScreen, SettingsScreen, open_path)

COLS = ["id", "title", "status", "progress", "speed", "eta", "size", "frag"]

FORMATS = [("Video + audio", "video+audio"), ("Video only", "video-only"),
           ("Audio only", "audio-only")]
QUALITIES = [("Best available", "best"), ("2160p", "2160"), ("1440p", "1440"),
             ("1080p", "1080"), ("720p", "720"), ("480p", "480"), ("360p", "360")]
VCODECS = [("Any", "any"), ("H.264 / avc1", "avc1"), ("VP9 / vp9", "vp9"),
           ("AV1 / av01", "av01"), ("H.265 / h265", "h265")]
ACODECS = [("mp3", "mp3"), ("m4a", "m4a"), ("flac", "flac"), ("opus", "opus")]


def bar(p: float, width: int = 16) -> str:
    p = max(0.0, min(1.0, float(p or 0)))
    n = int(p * width)
    return "█" * n + "░" * (width - n) + f" {p * 100:5.1f}%"


def _sw(label: str, value: bool, widget_id: str) -> ComposeResult:
    """A Switch with its label to the left (textual 8 dropped Switch.label)."""
    with Horizontal(classes="sw-row"):
        yield Label(label, classes="sw-label")
        yield Switch(value=value, id=widget_id)


class MDownloadApp(App):
    TITLE = "mdownload"
    SUB_TITLE = "yt-dlp + spotify / youtube-music"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        # F-keys and ctrl+q are priority: they work even while the URL box has
        # focus and consumes printable keys.  The single letters are the fast
        # path when the queue or the options panel has focus.
        Binding("ctrl+q", "quit_app", "quit", priority=True),
        Binding("f1,question_mark", "help", "help", priority=True),
        Binding("f2", "settings", "settings", priority=True),
        Binding("f3", "dest", "dest", priority=True),
        Binding("f4", "browse", "browse", priority=True),
        Binding("f5", "history", "history", priority=True),
        Binding("f6", "export", "export", priority=True),
        Binding("f7", "pause_all", "pause all", priority=True),
        Binding("f8", "resume_all", "resume all", priority=True),
        Binding("f9", "clear_finished", "clear done", priority=True),
        Binding("f10", "retry_failed", "retry failed", priority=True),
        Binding("f11", "batch_mode", "batch", priority=True),
        Binding("f12", "dupes", "dupes", priority=True),
        Binding("q", "quit_app", "quit"),
        Binding("a", "add", "queue"),
        Binding("p", "pause", "pause", show=False),
        Binding("c", "cancel", "cancel", show=False),
        Binding("r", "retry", "retry", show=False),
        Binding("o", "open_dest", "open", show=False),
        Binding("x", "remove", "remove", show=False),
        Binding("P", "pause_all", "pause all", show=False),
        Binding("R", "resume_all", "resume all", show=False),
        Binding("C", "clear_finished", "clear done", show=False),
        Binding("F", "retry_failed", "retry failed", show=False),
    ]

    def __init__(self, cfg, initial_urls: list[str] | None = None):
        super().__init__()
        self.cfg = cfg
        self.history = History()
        self.manager = DownloadManager(cfg, self.history)
        self.initial_urls = initial_urls or []
        self._rows: dict[str, RowKey] = {}
        self._status_seen: dict[str, str] = {}
        self._quitting = False

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("mdownload — paste a URL and press a · F4 → first row "
                         "queues your whole liked library", id="topbar")
            with Horizontal(id="main"):
                with VerticalScroll(id="options"):
                    yield Static("Source", classes="sec")
                    yield Input(placeholder="URL — F11 for a batch list / playlist",
                                id="url")
                    yield TextArea("", id="urlbatch", classes="hidden")
                    yield Static("Format", classes="sec")
                    yield Select(FORMATS, value=self.cfg.format, allow_blank=False,
                                 id="fmt")
                    yield Static("Quality / resolution", classes="sec")
                    yield Select(QUALITIES,
                                 value=self.cfg.quality if self.cfg.quality in
                                 [v for _, v in QUALITIES] else "best",
                                 allow_blank=False, id="quality")
                    yield Static("Video codec", classes="sec")
                    yield Select(VCODECS,
                                 value=self.cfg.video_codec if self.cfg.video_codec in
                                 [v for _, v in VCODECS] else "any",
                                 allow_blank=False, id="vcodec")
                    yield Static("Audio codec / bitrate", classes="sec")
                    yield Select(ACODECS,
                                 value=self.cfg.audio_codec if self.cfg.audio_codec in
                                 [v for _, v in ACODECS] else "mp3",
                                 allow_blank=False, id="acodec")
                    yield Input(value=self.cfg.audio_bitrate, placeholder="bitrate kbps",
                                id="bitrate")
                    yield Static("Options", classes="sec")
                    yield from _sw("subtitles", self.cfg.subtitles, "sw-subs")
                    yield Input(value=self.cfg.subtitle_langs, placeholder="subtitle langs",
                                id="sublangs")
                    yield from _sw("thumbnail", self.cfg.embed_thumbnail, "sw-thumb")
                    yield from _sw("metadata", self.cfg.embed_metadata, "sw-meta")
                    yield from _sw("chapters", self.cfg.embed_chapters, "sw-chapters")
                    yield from _sw("sponsorblock", self.cfg.sponsorblock, "sw-sponsor")
                    yield Static("Network / limits", classes="sec")
                    yield Input(value=self.cfg.cookies_from_browser,
                                placeholder="cookies from browser (firefox, chrome, …)",
                                id="cookies-browser")
                    yield Input(value=self.cfg.rate_limit, placeholder="rate limit (5M)",
                                id="rate")
                    yield Input(value=str(self.cfg.retries), placeholder="retries",
                                id="retries")
                    yield Input(value=self.cfg.proxy, placeholder="proxy URL", id="proxy")
                    yield Static("Destination", classes="sec")
                    yield Input(value=self.cfg.dest_dir, id="dest")
                    with Horizontal(id="dest-row"):
                        yield Button("Pick…", id="btn-dest")
                        yield Button("Browse library", id="btn-browse", variant="primary")
                    yield Static("f2 settings · f3 destination · f11 batch · ? help",
                                 classes="hint")
                with Vertical(id="right"):
                    yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)
                    yield RichLog(id="log", markup=True, wrap=True, highlight=False)
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        for label, key in (("#", "id"), ("Title", "title"), ("Status", "status"),
                           ("Progress", "progress"), ("Speed", "speed"), ("ETA", "eta"),
                           ("Size", "size"), ("Frag", "frag")):
            table.add_column(label, key=key, width=None if key == "title" else 14)
        self.query_one("#url", Input).focus()
        self.set_interval(0.4, self.refresh_queue)
        self.log_line("[b]mdownload ready[/b] — yt-dlp engine, Spotify + YT Music browse")
        if self.initial_urls:
            self.submit_urls("\n".join(self.initial_urls))

    def log_line(self, text: str) -> None:
        # a pushed modal screen has no #log; the callbacks that log from
        # workers can fire while one is open
        try:
            self.query_one("#log", RichLog).write(text)
        except NoMatches:
            pass

    # --------------------------------------------------------- queue table
    def _cells(self, job) -> list[str]:
        frag = ""
        if job.frag_index and job.frag_count:
            frag = f"{job.frag_index}/{job.frag_count}"
        return [job.id, job.title[:90], job.status, bar(job.progress),
                f"{human(job.speed)}/s" if job.speed else "-", clock(job.eta),
                human(job.total or job.downloaded), frag]

    def refresh_queue(self) -> None:
        try:
            table = self.query_one("#queue", DataTable)
        except NoMatches:   # a modal screen is on top
            return
        jobs = self.manager.snapshot()
        want = {j.id for j in jobs}
        for jid in list(self._rows):
            if jid not in want:
                try:
                    table.remove_row(self._rows.pop(jid))
                except Exception:  # noqa: BLE001 - row may already be gone
                    pass
        for job in jobs:
            cells = self._cells(job)
            if job.id not in self._rows:
                self._rows[job.id] = table.add_row(*cells, key=job.id)
            else:
                for label, value in zip(COLS, cells):
                    table.update_cell(self._rows[job.id], label, value, update_width=False)
            prev = self._status_seen.get(job.id)
            if prev != job.status:
                self._status_seen[job.id] = job.status
                if job.status == "error":
                    self.log_line(f"[red]✗[/red] {job.title[:70]} — {job.error}")
                elif job.status == "skipped":
                    self.log_line(f"[dim]⊘ skipped[/dim] {job.title[:70]}"
                                  + (f" — {job.error}" if job.error else ""))
                elif job.status in ("done", "cancelled"):
                    self.log_line(f"{'✓' if job.status == 'done' else '⊘'} "
                                  f"{job.title[:70]} ({job.status})")

    def selected_job(self) -> str | None:
        try:
            table = self.query_one("#queue", DataTable)
        except NoMatches:
            return None
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return str(key.value) if key.value else None

    # -------------------------------------------------------------- adding
    def _pending_text(self) -> str:
        area = self.query_one("#urlbatch", TextArea)
        if area.display:
            return area.text
        return self.query_one("#url", Input).value

    def _clear_pending(self) -> None:
        self.query_one("#urlbatch", TextArea).text = ""
        self.query_one("#url", Input).value = ""

    def action_add(self) -> None:
        text = self._pending_text().strip()
        if not text:
            self.notify("nothing to queue", severity="warning")
            return
        self.submit_urls(text)

    def submit_urls(self, text: str) -> None:
        urls = [x.strip() for x in text.replace(",", "\n").splitlines()]
        urls = [x for x in urls if x and not x.startswith("#")]
        if not urls:
            return
        self._clear_pending()
        self.log_line(f"expanding {len(urls)} input URL(s) …")
        self._expand(urls)

    @work(thread=True, exclusive=True)
    def _expand(self, urls: list[str]) -> None:
        from . import sources

        for url in urls:
            # A Spotify link carries no stream yt-dlp can read — it has to go
            # through the Web API (a pasted spotify link is a bare HTTP 403).
            try:
                tracks = sources.tracks_for_url(
                    self.cfg, url,
                    note=lambda m: self.call_from_thread(self.log_line, m))
            except Exception as exc:  # noqa: BLE001 - shown in the log
                self.call_from_thread(self.log_line,
                                      f"[red]source failed[/red] {url}: "
                                      f"{type(exc).__name__}: {exc}")
                continue
            if tracks:
                self.call_from_thread(self.add_tracks, tracks)
                continue
            try:
                pairs = DownloadManager.expand(url)
            except Exception as exc:  # noqa: BLE001 - the job itself will report it
                self.call_from_thread(self._add_one, url, url)
                self.call_from_thread(self.log_line,
                                      f"[yellow]expand failed[/yellow] {url}: "
                                      f"{type(exc).__name__}: {exc}")
                continue
            if len(pairs) > 1:
                self.app.call_from_thread(self.log_line,
                                          f"playlist → {len(pairs)} items: {url}")
            added = dup = 0
            for link, title in pairs:
                if self.app.call_from_thread(self._add_one, link, title):
                    added += 1
                else:
                    dup += 1
            if dup:
                self.app.call_from_thread(
                    self.log_line,
                    f"queued {added} of {len(pairs)} — skipped {dup} duplicate(s)")

    def _add_one(self, url: str, title: str) -> bool:
        # no per-job notify: a 500-track playlist would drown the screen
        return self.manager.add(url=url, title=title) is not None

    def add_tracks(self, tracks: list) -> None:
        added = sum(1 for t in tracks
                    if self.manager.add(track=t, title=str(t), source=t.source,
                                        url=""))
        dup = len(tracks) - added
        msg = f"queued {added} track(s) for YouTube matching"
        if dup:
            msg += f" — skipped {dup} already queued or already on disk"
        self.log_line(msg)
        self.notify(msg)

    # ------------------------------------------------------------ actions
    def _job_action(self, fn) -> None:
        jid = self.selected_job()
        if not jid:
            self.notify("no row selected", severity="warning")
            return
        fn(jid)
        self.refresh_queue()

    def action_pause(self) -> None:
        jid = self.selected_job()
        if not jid:
            return
        job = self.manager.get(jid)
        if job and job.status == "paused":
            self.manager.resume(jid)
        else:
            self.manager.pause(jid)

    def action_cancel(self) -> None:
        self._job_action(self.manager.cancel)

    def action_retry(self) -> None:
        jid = self.selected_job()
        if jid and not self.manager.retry(jid):
            self.notify("only failed, cancelled or skipped jobs retry",
                        severity="warning")

    def action_remove(self) -> None:
        jid = self.selected_job()
        if jid:
            self.manager.remove(jid)
            self.refresh_queue()

    def action_open_dest(self) -> None:
        jid = self.selected_job()
        job = self.manager.get(jid) if jid else None
        target = job.dest if job else self.cfg.dest_dir
        open_path(target)
        self.log_line(f"opened {target}")

    def action_pause_all(self) -> None:
        self.manager.pause_all()
        self.log_line("paused all active jobs")

    def action_resume_all(self) -> None:
        self.manager.resume_all()

    def action_clear_finished(self) -> None:
        n = self.manager.clear_finished()
        self.refresh_queue()
        self.notify(f"cleared {n} finished job(s)")

    def action_retry_failed(self) -> None:
        n = self.manager.retry_failed()
        self.notify(f"retrying {n} failed job(s)")

    def action_batch_mode(self) -> None:
        area = self.query_one("#urlbatch", TextArea)
        one = self.query_one("#url", Input)
        area.display = not area.display
        one.display = not area.display
        (area if area.display else one).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.cfg), self._after_settings)

    def _after_settings(self, result) -> None:
        if not result:
            return
        config_mod.save(self.cfg)
        self.manager.reconfig()
        self.query_one("#url", Input).focus()
        self.log_line("settings saved")

    def action_dest(self) -> None:
        self.push_screen(DestScreen(self.cfg), self._after_dest)

    def _after_dest(self, path) -> None:
        if not path:
            return
        self.cfg.dest_dir = path
        self.cfg.remember_dir(path)
        config_mod.save(self.cfg)
        self.query_one("#dest", Input).value = path
        self.log_line(f"destination → {path}")

    def action_browse(self) -> None:
        self.push_screen(BrowseScreen(self.cfg), self._after_browse)

    def _after_browse(self, picks) -> None:
        if picks:
            self.add_tracks(picks)
            self._refresh_dest_input()

    def _refresh_dest_input(self) -> None:
        self.query_one("#dest", Input).value = self.cfg.dest_dir

    def action_dupes(self) -> None:
        self.push_screen(DupesScreen(self.cfg))

    def action_history(self) -> None:
        self.push_screen(HistoryScreen(self.history), self._after_history)

    def _after_history(self, result) -> None:
        if not isinstance(result, dict):
            return
        if "rerun" in result:
            row = result["rerun"]
            self._add_one(row["url"], row["title"])
            self.log_line(f"re-queued from history: {row['title'][:70]}")
        elif "export_history" in result:
            self._write_json(result["export_history"], "history")

    def action_export(self) -> None:
        rows = [{"id": j.id, "url": j.url, "title": j.title, "status": j.status,
                 "dest": j.dest, "format": j.spec.get("format"), "progress": j.progress,
                 "error": j.error} for j in self.manager.snapshot()]
        self._write_json(rows, "queue")

    def _write_json(self, rows: list, kind: str) -> None:
        path = Path.cwd() / f"mdownload-{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
        try:
            path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self.notify(f"export failed: {exc}", severity="error")
            return
        self.log_line(f"exported {len(rows)} row(s) → {path}")
        self.notify(f"exported → {path.name}")

    # --------------------------------------------------------- option sync
    def on_select_changed(self, event: Select.Changed) -> None:
        mapping = {"fmt": "format", "quality": "quality", "vcodec": "video_codec",
                   "acodec": "audio_codec"}
        attr = mapping.get(event.select.id or "")
        if attr and event.value is not Select.BLANK:
            setattr(self.cfg, attr, str(event.value))
            config_mod.save(self.cfg)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        mapping = {"sw-subs": "subtitles", "sw-thumb": "embed_thumbnail",
                   "sw-meta": "embed_metadata", "sw-chapters": "embed_chapters",
                   "sw-sponsor": "sponsorblock"}
        attr = mapping.get(event.switch.id or "")
        if attr:
            setattr(self.cfg, attr, bool(event.value))
            config_mod.save(self.cfg)

    def on_input_changed(self, event: Input.Changed) -> None:
        mapping = {"bitrate": "audio_bitrate", "sublangs": "subtitle_langs",
                   "cookies-browser": "cookies_from_browser", "rate": "rate_limit",
                   "proxy": "proxy"}
        attr = mapping.get(event.input.id or "")
        if attr:
            setattr(self.cfg, attr, event.value)
        elif event.input.id == "dest" and event.value:
            self.cfg.dest_dir = event.value
        elif event.input.id == "retries":
            try:
                self.cfg.retries = int(event.value or 0)
            except ValueError:
                return
        else:
            return
        config_mod.save(self.cfg)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "url":
            self.action_add()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-dest":
            self.action_dest()
        elif event.button.id == "btn-browse":
            self.action_browse()

    # ------------------------------------------------------------- teardown
    def action_quit_app(self) -> None:
        if self.manager.active() and not self._quitting:
            self.push_screen(ConfirmQuit(self.manager.active()), self._confirmed)
            return
        self.exit()

    def _confirmed(self, yes) -> None:
        if yes:
            self._quitting = True
            self.exit()

    def on_unmount(self) -> None:
        self.manager.shutdown()
        self.history.close()


def run(cfg, initial_urls: list[str] | None = None) -> int:
    app = MDownloadApp(cfg, initial_urls)
    app.run()
    return 0
