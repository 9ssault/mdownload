"""Modal screens: help, source browsing, history, destination picker, settings."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Input, Label, OptionList, Static)
from textual.widgets.option_list import Option

from . import sources
from .library import Library

CATEGORIES = [
    # The two "all likes" rows skip the table: one pick queues the whole library.
    ("spotify", "all", "Spotify · queue ALL liked songs"),
    ("ytmusic", "all", "YouTube Music · queue ALL liked songs"),
    ("spotify", "recent", "Spotify · Recently played"),
    ("spotify", "likes", "Spotify · Liked songs"),
    ("spotify", "playlists", "Spotify · Playlists"),
    ("ytmusic", "likes", "YouTube Music · Liked songs"),
    ("ytmusic", "playlists", "YouTube Music · Library playlists"),
]


def open_path(path: str) -> None:
    p = str(Path(path).expanduser())
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", p])
        elif sys.platform.startswith("win"):
            subprocess.Popen(["explorer", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except OSError:
        pass


# ------------------------------------------------------------------- help
class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape,question_mark,q", "dismiss", "close", show=False),
        Binding("f1", "dismiss", "close", show=False),
    ]

    HELP = """[b]mdownload — keys[/b]

  [b]Always available (work while the URL box has focus)[/b]
    ctrl+q         quit (asks if work is in flight)
    F1  /  ?       this help
    F2             settings (Spotify creds, concurrency)
    F3             destination picker
    F4             browse Spotify / YouTube Music library
    F5             history (search, re-run, export)
    F6             export queue as JSON
    F7  /  F8      pause all / resume all
    F9  /  F10     clear finished / retry failed
    F11            batch mode (multi-line list, one URL per line)
    F12            duplicates: find and clean repeated tracks in the library

  [b]Input[/b]
    enter          queue the URL(s); a playlist URL expands to one job per item
    F11            paste a batch: one URL per line, comments with #
    tab            move focus to the queue: single letters then work

  [b]Options panel[/b]
    tab / shift+tab  move between fields
    every change is written to ~/.config/mdownload/config.toml

  [b]Queue (when the table has focus)[/b]
    q              quit          a  queue from the URL box
    p              pause / resume selected
    c              cancel selected
    r              retry selected (failed or cancelled)
    o              open destination folder of selected
    x              remove selected row
    P / R          pause all / resume all
    C / F          clear finished / retry failed

  [b]Browse (F4)[/b]
    the first two rows queue your whole Spotify / YT Music liked library
    space          mark item
    a              queue marked items (cursor row if none marked)
    A              queue everything in the current view (all likes, or the
                   whole playlist after you open it with enter)
    enter          open the playlist under the cursor
    backspace      back out of a playlist
    esc            close

  [b]Duplicate protection[/b]
    mdownload never queues the same track twice, and skips anything whose file
    is already in the destination folder — even in a different session.  A
    skipped row says why; select it and press r to download it anyway.

  [b]Duplicates (F12)[/b]
    d              remove the selected group's extra copies (press d twice)
    D              remove every duplicate copy in the library (press D twice)
    r              rescan · o open the folder · esc close

  [b]History (F5)[/b]
    r  re-run · d delete · e export json · esc close

Press ? or esc to close."""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.HELP, id="help-body", markup=True)

    def action_dismiss(self, result=None) -> None:
        self.dismiss(None)


# ----------------------------------------------------------------- browse
class BrowseScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "close", show=False),
        Binding("a", "queue_marked", "queue marked"),
        Binding("A", "queue_all", "queue all"),
        Binding("space", "toggle_mark", "mark", show=False),
        Binding("backspace", "pop", "back", show=False),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.items: list = []          # payloads aligned with table rows
        self.marks: set[int] = set()
        self.trail: list[tuple[str, list]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-box"):
            yield Static("Browse library", id="browse-title")
            with Horizontal(id="browse-body"):
                yield OptionList(*[Option(label, id=f"{s}:{k}") for s, k, label in CATEGORIES],
                                 id="browse-cats")
                with Vertical(id="browse-right"):
                    yield Static("select a category", id="browse-status")
                    yield DataTable(id="browse-table", cursor_type="row", zebra_stripes=True)
            yield Static("space mark · a queue marked · A queue all · enter open playlist · "
                         "backspace back · esc close", id="browse-help")

    def on_mount(self) -> None:
        table = self.query_one("#browse-table", DataTable)
        table.add_column("·", width=2)
        table.add_column("Title", width=60)
        table.add_column("Artist", width=28)
        table.add_column("Album / count", width=24)
        self.query_one("#browse-cats", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        source, key = str(event.option.id).split(":", 1)
        label = next(l for s, k, l in CATEGORIES if s == source and k == key)
        self.trail = []
        if key == "all":
            self._fetch_all(label, lambda: sources.load(source, "likes", self.cfg))
            return
        self._fetch(label, lambda: sources.load(source, key, self.cfg))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row >= len(self.items):
            return
        item = self.items[event.cursor_row]
        if isinstance(item, dict):
            source, pid, name = item["source"], item["id"], item["name"]
            notes: list[str] = []
            if source == "spotify":
                loader = lambda: sources.spotify_playlist_tracks(  # noqa: E731
                    self.cfg, pid, note=notes.append)
            else:
                loader = lambda: sources.yt_playlist_tracks(self.cfg, pid)  # noqa: E731
            self.trail.append((name, self.items))
            self._fetch(name, loader, notes)

    @work(thread=True, exclusive=True)
    def _fetch(self, label: str, loader, notes: list | None = None) -> None:
        app = self.app
        app.call_from_thread(self._set_status, f"loading {label} …")
        try:
            items = loader()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            app.call_from_thread(self._set_status, f"{type(exc).__name__}: {exc}")
            return
        app.call_from_thread(self._show_items, label, items)
        if notes:
            # after _show_items, which writes its own "N items" status
            app.call_from_thread(self._set_status, notes[-1])

    @work(thread=True, exclusive=True)
    def _fetch_all(self, label: str, loader) -> None:
        """Fetch a whole library and close the screen with every track queued."""
        app = self.app
        app.call_from_thread(self._set_status, f"loading every {label} ...")
        try:
            items = loader()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            app.call_from_thread(self._set_status, f"{type(exc).__name__}: {exc}")
            return
        if items:
            app.call_from_thread(self.dismiss, items)
        else:
            app.call_from_thread(self._set_status, f"{label}: nothing to queue")

    def _set_status(self, text: str) -> None:
        self.query_one("#browse-status", Static).update(text)

    def _show_items(self, label: str, items: list) -> None:
        table = self.query_one("#browse-table", DataTable)
        table.clear()
        self.items = list(items)
        self.marks.clear()
        for i, item in enumerate(items):
            if isinstance(item, dict):
                table.add_row("", item.get("name", ""), "", f"{item.get('count', 0)} tracks")
            else:
                table.add_row("", getattr(item, "title", ""), getattr(item, "artist", ""),
                              getattr(item, "album", ""))
        self._set_status(f"{label} — {len(items)} items"
                         + (f"  [{len(self.trail)} deep]" if self.trail else ""))

    def action_toggle_mark(self) -> None:
        table = self.query_one("#browse-table", DataTable)
        row = table.cursor_row
        if row >= len(self.items):
            return
        # update_cell wants the key add_column generated; positional updates
        # take a Coordinate instead.
        if row in self.marks:
            self.marks.discard(row)
            table.update_cell_at(Coordinate(row, 0), "")
        else:
            self.marks.add(row)
            table.update_cell_at(Coordinate(row, 0), "[b]✓[/b]")

    def action_queue_marked(self) -> None:
        rows = sorted(self.marks) or []
        if not rows:
            cursor = self.query_one("#browse-table", DataTable).cursor_row
            if cursor < len(self.items) and not isinstance(self.items[cursor], dict):
                rows = [cursor]
        picks = [self.items[i] for i in rows if not isinstance(self.items[i], dict)]
        if picks:
            self.dismiss(picks)

    def action_queue_all(self) -> None:
        """Queue every track in the current view (all likes, or the open playlist)."""
        tracks = [i for i in self.items if not isinstance(i, dict)]
        if not tracks:
            self._set_status("this is a list of playlists — enter one, then A "
                             "queues every track in it")
            return
        self.dismiss(tracks)

    def action_pop(self) -> None:
        if self.trail:
            label, items = self.trail.pop()
            self._show_items(label, items)


# ---------------------------------------------------------------- history
class HistoryScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "close", show=False),
        Binding("r", "rerun", "re-run"),
        Binding("d", "delete", "delete"),
        Binding("e", "export", "export json"),
    ]

    def __init__(self, history):
        super().__init__()
        self.history = history
        self.rows: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="history-box"):
            yield Static("History", id="history-title")
            yield Input(placeholder="search title / url / destination", id="history-search")
            yield DataTable(id="history-table", cursor_type="row", zebra_stripes=True)
            yield Static("r re-run · d delete · e export json · esc close", id="history-help")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        for col, width in (("When", 19), ("Status", 10), ("Title", 52), ("Dest", 30),
                           ("Size", 9), ("Error", 30)):
            table.add_column(col, width=width)
        self._reload()
        self.query_one("#history-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._reload(event.value)

    def _reload(self, query: str = "") -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        self.rows = self.history.search(query)
        for r in self.rows:
            import time
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["finished_at"]))
            size = r["size"]
            human = f"{size / 1048576:.1f}M" if size else "-"
            table.add_row(when, r["status"], r["title"][:52], r["dest"][-30:], human,
                          (r["error"] or "")[:30])

    def _selected(self) -> dict | None:
        row = self.query_one("#history-table", DataTable).cursor_row
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def action_rerun(self) -> None:
        row = self._selected()
        if row and row["url"]:
            self.dismiss({"rerun": row})

    def action_delete(self) -> None:
        row = self._selected()
        if row:
            self.history.delete(row["id"])
            self._reload(self.query_one("#history-search", Input).value)

    def action_export(self) -> None:
        self.dismiss({"export_history": self.rows})


# ---------------------------------------------------------- duplicates
class DupesScreen(ModalScreen):
    """Find tracks that exist twice in the destination folder.

    Nothing is removed on the first keystroke: `d` arms the deletion and the
    second `d` does it, because a music folder is expensive to rebuild.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "close", show=False),
        Binding("r", "rescan", "rescan"),
        Binding("d", "delete_group", "delete dupes"),
        Binding("D", "delete_all", "delete all dupes"),
        Binding("o", "reveal", "open folder", show=False),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.library = Library(cfg.dest_dir)
        self.groups: list = []
        self.armed: tuple[str, int | None] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dupes-box"):
            yield Static(f"Duplicates under {self.cfg.dest_dir}", id="dupes-title")
            yield DataTable(id="dupes-table", cursor_type="row", zebra_stripes=True)
            yield Static("scanning ...", id="dupes-status")
            yield Static("d delete the selected group's copies · D delete every copy · "
                         "r rescan · o open folder · esc close", id="dupes-help")

    def on_mount(self) -> None:
        table = self.query_one("#dupes-table", DataTable)
        for col, width in (("Keep", 46), ("Size", 9), ("Copies", 7),
                           ("Removed with d", 56)):
            table.add_column(col, width=width)
        self.action_rescan()

    @work(thread=True, exclusive=True)
    def action_rescan(self) -> None:
        app = self.app
        app.call_from_thread(self._status, "scanning for duplicates ...")
        groups = self.library.duplicates()
        app.call_from_thread(self._show, groups)

    def _status(self, text: str) -> None:
        self.query_one("#dupes-status", Static).update(text)

    def _show(self, groups: list) -> None:
        table = self.query_one("#dupes-table", DataTable)
        table.clear()
        self.groups = list(groups)
        self.armed = None
        if not self.groups:
            self._status(f"no duplicates in {len(self.library.scan())} file(s) — "
                         "nothing to clean")
            return
        for i, g in enumerate(self.groups):
            try:
                size = f"{g.keep.stat().st_size / 1048576:.1f}M"
            except OSError:
                size = "-"
            table.add_row(g.keep.name, size, str(len(g.dupes)),
                          "\n".join(p.name for p in g.dupes)[:400], key=str(i))
        total = sum(g.bytes for g in self.groups)
        files = sum(len(g.dupes) for g in self.groups)
        self._status(f"{len(self.groups)} track(s) duplicated · {files} removable "
                     f"file(s) · {total / 1048576:.1f}M")

    def _selected_group(self):
        row = self.query_one("#dupes-table", DataTable).cursor_row
        return self.groups[row] if 0 <= row < len(self.groups) else None

    def action_delete_group(self) -> None:
        row = self.query_one("#dupes-table", DataTable).cursor_row
        if self.armed == ("one", row):
            self.armed = None
            if 0 <= row < len(self.groups):
                self._delete(list(self.groups[row].dupes))
            return
        if not 0 <= row < len(self.groups):
            return
        group = self.groups[row]
        self.armed = ("one", row)
        self._status(f"{len(group.dupes)} file(s) will be removed, keeping "
                     f"{group.keep.name} — press d again to do it, r to cancel")

    def action_delete_all(self) -> None:
        if self.armed and self.armed[0] == "all":
            self.armed = None
            self._delete([p for g in self.groups for p in g.dupes])
            return
        files = sum(len(g.dupes) for g in self.groups)
        if not files:
            self._status("nothing to delete")
            return
        self.armed = ("all", None)
        self._status(f"remove {files} duplicate file(s) in "
                     f"{len(self.groups)} group(s)? — press D again, r to cancel")

    def action_reveal(self) -> None:
        group = self._selected_group()
        open_path(str((group.keep if group else Path(self.cfg.dest_dir)).parent))

    @work(thread=True, exclusive=True)
    def _delete(self, paths: list) -> None:
        app = self.app
        app.call_from_thread(self._status, f"removing {len(paths)} file(s) ...")
        removed, errors = Library.delete(paths)
        app.call_from_thread(
            self._status,
            f"removed {removed} file(s)" + (f", {len(errors)} failed: {errors[0]}" if errors else ""))
        app.call_from_thread(self.log_line, f"[dim]dupes[/dim] removed {removed} file(s)")
        self.library.rescore()
        groups = self.library.duplicates()
        app.call_from_thread(self._show, groups)

    def log_line(self, text: str) -> None:
        try:
            self.app.log_line(text)
        except Exception:  # noqa: BLE001 - the log lives on the main screen
            pass


# ------------------------------------------------------- destination picker
class DestScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "close", show=False)]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="dest-box"):
            yield Static("Destination folder", id="dest-title")
            yield Input(value=self.cfg.dest_dir, id="dest-input")
            yield OptionList(*[Option(p, id=f"d{i}") for i, p in enumerate(self.cfg.recent_dirs)],
                             id="dest-recent")
            with Horizontal(id="dest-buttons"):
                yield Button("Use this folder", variant="primary", id="dest-ok")
                yield Button("Cancel", id="dest-cancel")
            yield Static("pick a recent path, or type any path · ~ expands", id="dest-help")

    def on_mount(self) -> None:
        self.query_one("#dest-input", Input).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int(str(event.option.id)[1:])
        self.query_one("#dest-input", Input).value = self.cfg.recent_dirs[index]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dest-ok":
            self._accept()
        else:
            self.dismiss(None)

    def _accept(self) -> None:
        raw = self.query_one("#dest-input", Input).value.strip()
        if not raw:
            self.dismiss(None)
            return
        path = Path(raw).expanduser()
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.query_one("#dest-help", Static).update(f"cannot create {path}")
                return
        self.dismiss(str(path))


# ---------------------------------------------------------------- settings
class SettingsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "close", show=False),
        Binding("ctrl+s", "save", "save"),
    ]

    FIELDS = [
        ("spotify_client_id", "Spotify client id"),
        ("spotify_client_secret", "Spotify client secret", True),
        ("spotify_redirect_uri", "Spotify redirect URI"),
        ("spotify_cache", "Spotify token cache"),
        ("ytmusic_auth", "YouTube Music auth json"),
        ("cookies_file", "cookies.txt (Netscape jar)"),
        ("dest_dir", "default destination"),
        ("concurrent", "concurrent downloads (int)"),
        ("match_threshold", "match threshold 0-1"),
        ("cookie_browser", "cookies from browser (firefox/chrome/…)"),
    ]

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-box"):
            yield Static("Settings  (ctrl+s saves to ~/.config/mdownload/config.toml)",
                         id="settings-title")
            for spec in self.FIELDS:
                name, label = spec[0], spec[1]
                secret = len(spec) > 2
                if name == "cookie_browser":
                    value = self.cfg.cookies_from_browser
                else:
                    value = str(getattr(self.cfg, name))
                yield Label(label, classes="field-label")
                yield Input(value=value, password=secret, id=f"set-{name}",
                            classes="field-input")
            with Horizontal(id="settings-buttons"):
                yield Button("Save", variant="primary", id="set-save")
                yield Button("Cancel", id="set-cancel")
            yield Static("", id="settings-status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "set-save":
            self.action_save()
        else:
            self.dismiss(None)

    def action_save(self) -> None:
        for spec in self.FIELDS:
            name = spec[0]
            raw = self.query_one(f"#set-{name}", Input).value.strip()
            if name == "cookie_browser":
                self.cfg.cookies_from_browser = raw
            elif name in ("concurrent", "match_threshold"):
                try:
                    setattr(self.cfg, name, int(raw) if name == "concurrent" else float(raw))
                except ValueError:
                    pass
            else:
                setattr(self.cfg, name, raw)
        self.dismiss({"saved": True})

    def action_dismiss(self, result=None) -> None:
        self.dismiss(result)


# ------------------------------------------------------------ confirm quit
class ConfirmQuit(ModalScreen):
    BINDINGS = [
        Binding("y,enter", "dismiss(True)", "quit anyway"),
        Binding("n,escape,q", "dismiss(False)", "stay"),
    ]

    def __init__(self, active: int):
        super().__init__()
        self.active = active

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(f"[b]{self.active} job(s) still downloading.[/b]\n\n"
                         "Quit anyway? Active downloads are cancelled and their "
                         "partial files are left on disk.\n\n"
                         "[b]y[/b] / enter — quit      [b]n[/b] / esc — stay")
