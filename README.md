# mdownload

A full-screen terminal media downloader. `yt-dlp` is the engine; the library
browser reads your **Spotify** and **YouTube Music** accounts, so you can queue
"the track I liked last week" without hunting for a link.

```
┌ mdownload — paste a URL, press a · F4 = whole library ───────┐
│ Source        │  #  Title          Status   Progress  …    │
│ [ URL box ]   │  a1  Bad Guy       running  ████░░  62.1%   │
│ Format        │  b2  Playlist item  queued   ░░░░░░   0.0%   │
│ Quality       │                                              │
│ Codecs        │  log: ✓ Bad Guy (done)                       │
│ Options       │  log: ✗ track.mp3 — DownloadError: …         │
│ Network       │                                              │
│ Destination   │                                              │
└ f1 help · f2 settings · f3 dest · f4 browse · f5 history ────┘
```

## Install

Requires Python 3.11+, `ffmpeg` on `PATH`, and a JavaScript runtime (`deno`,
`node` or `bun`) for YouTube. Everything else is installed for you.

### Linux / macOS

```bash
git clone <repo-url> ~/mdownload
~/mdownload/install.sh
```

The script makes a venv at `~/.venvs/download-music`, installs the package into
it editable, links `~/.local/bin/mdownload`, writes the fish function when fish
is present, and runs `--self-test`. Re-run it to upgrade. Override with
`PYTHON=python3.12 MDOWNLOAD_VENV=~/venvs/md ./install.sh`.

By hand, if you prefer:

```bash
python3 -m venv ~/.venvs/download-music
~/.venvs/download-music/bin/pip install -U pip
~/.venvs/download-music/bin/pip install -e ~/mdownload
ln -sfn ~/.venvs/download-music/bin/mdownload ~/.local/bin/mdownload
```

`~/.local/bin` is on `PATH` for most distros. If not: `fish_add_path
~/.local/bin`, or `export PATH="$HOME/.local/bin:$PATH"` in `~/.bashrc`.

### Windows

```powershell
git clone <repo-url> $HOME\mdownload
powershell -ExecutionPolicy Bypass -File $HOME\mdownload\install.ps1
```

The script finds Python (`py -3`, then `python`), makes a venv at
`%LOCALAPPDATA%\mdownload\venv`, installs the package into it, and appends that
venv's `Scripts` folder to your **user** PATH — open a new terminal and
`mdownload` is there. Re-run it to upgrade.

By hand:

```powershell
py -3 -m venv "$env:LOCALAPPDATA\mdownload\venv"
& "$env:LOCALAPPDATA\mdownload\venv\Scripts\pip" install -e "$HOME\mdownload"
# then add %LOCALAPPDATA%\mdownload\venv\Scripts to PATH
```

Windows-specific behaviour: settings live in `%APPDATA%\mdownload\config.toml`
and the history db in `%LOCALAPPDATA%\mdownload\history.db` (Linux keeps
`~/.config/mdownload` and `~/.local/state/mdownload`). The TUI, clipboard paste
and every keybind are identical. There are no symlinks and no fish.

### fish (Linux / macOS)

A function file is installed at `~/.config/fish/functions/mdownload.fish`, so
fresh fish sessions autoload `mdownload` whether or not `~/.local/bin` is on
`PATH`. Reload an already-running shell with `exec fish`; `fish_config` also
shows and edits the function.

Verify:

```bash
mdownload --version            # mdownload 0.1.0
mdownload --self-test          # backend, paths, yt-dlp and ffmpeg versions
fish -l -c 'mdownload --version'
```

### ffmpeg and a JavaScript runtime

```bash
sudo apt install ffmpeg        # Debian / Ubuntu
sudo dnf install ffmpeg        # Fedora     (needs rpmfusion)
sudo pacman -S ffmpeg          # Arch
brew install ffmpeg            # macOS
winget install --id Gyan.FFmpeg -e          # Windows
```

```bash
# yt-dlp decodes and downloads no YouTube video without one of these plus the
# yt-dlp-ejs package (a mdownload requirement):
curl -fsSL https://deno.land/install.sh | sh    # deno, yt-dlp's default pick
sudo apt install nodejs        # or node / bun -- picked up from PATH
winget install --id OpenJS.NodeJS.LTS -e        # Windows
```

`mdownload --self-test` prints which runtime it found, or `MISSING` when there
is none.

## Use

```bash
mdownload                      # the TUI
mdownload 'https://youtu.be/…' # preload the queue and open the TUI
mdownload --login              # authorise Spotify / YT Music once, print counts, exit
mdownload --self-test          # no TUI, checks the install
```

Paste a URL into **Source**, press `enter` (or `a`), pick format and quality on
the left, watch the queue fill. A playlist URL expands to one job per item. A
pasted `open.spotify.com/playlist|album|track/...` link and a `ytmusic:<id>`
reference are read through their own API, not by yt-dlp.

### Download all liked songs, or a whole playlist

Everything happens in the TUI — there is no headless download mode.

1. `f4` opens the library browser.
2. The **first two rows** are `Spotify · queue ALL liked songs` and
   `YouTube Music · queue ALL liked songs`. Pick one and press `enter`: the
   whole library is fetched and queued, and the browser closes.
3. For one playlist, pick `Spotify · Playlists` / `YouTube Music · Library
   playlists`, `enter` on the playlist to open it, then `A` to queue every
   track in it.
4. `space` marks individual rows and `a` queues just the marked ones.

A 700-track library is queued in one action; concurrency (default 2) drains it.
Set `rate_limit` to `5M` and `concurrent` to 1 in `f2` before a full library
run — YouTube treats a bulk drain as abuse.

Spotify has no audio streams, so each Spotify track is matched to YouTube Music
first (the same scoring as the browser). `mdownload --login` in a shell once is
still needed to authorise the accounts.

A playlist that belongs to someone else is **not** refused: Spotify's Web API
serves `/playlists/{id}/items` only to the playlist's owner or a collaborator,
so those playlists are read from their public embed page instead. That page
publishes the first 100 tracks only, and the status line says so. To get the
whole list, copy the playlist into your own Spotify account (its owner rule no
longer applies) or paste it a track at a time.

### Duplicate protection

The same song never lands twice, even across sessions.

* **Nothing is queued twice.** A track id (or a URL) already sitting in the queue
  is refused before a worker ever picks it up. Queueing 689 liked songs twice
  adds zero rows the second time.
* **Nothing already on disk is downloaded again.** Before a job resolves or
  fetches, the destination folder is indexed and the track's name compared
  against it. Matching is normalised — `Artist - Title (Official Audio)` finds
  `Artist - Title.mp3` — so a folder filled by an older tool still counts.
* **Every finished job is recorded in `<destination>/.mdownload-archive.txt`**
  as `youtube <video-id>\t<file>`. Re-queueing a whole playlist costs no network
  at all. The file is checked for existence, so deleting a track by hand makes
  it downloadable again.

A refused job shows as `skipped` with the reason (`already have it: <file>` or
`already downloaded`), is not written to history, and is not counted as a
failure. Select it and press `r` to download it anyway — that one job bypasses
both guards.

### Downloading into Liner

Liner (`liner-bin`) keeps its own local library, and a file
dropped into its folder is **invisible** to it: the UI lists only what
`library.json` names. When `dest_dir` *is* a Liner library (a folder holding
`library.json` and `audio/`), mdownload files each finished track the way
Liner's own importer does:

* the audio moves to `audio/<sha1-of-content[:20]>.<ext>`;
* its embedded art is extracted to `covers/<same id>.<ext>`;
* a record — title, artist, album, `durationMs`, size, `addedAt` — is appended
  to `library.json` (written atomically).

Nothing else changes: the queue, the log and history still show the job, and
`.mdownload-archive.txt` sits at the library root, so `skipped` still means
"already have it". Identity here is the **content hash**, not the name: a
byte-identical copy that arrives twice collapses into one record, and nothing
depends on how the track is named.

Two rules come from Liner, not from mdownload:

* **Liner has to restart to see the new tracks.** It reads `library.json` once
  per process and keeps it in memory — closing the window and opening it again
  is the only thing that reloads the list, and mdownload does that for you. The
  restart runs on its own thread, so the queue keeps downloading through it, and
  the downloads are in mdownload's process — a Liner restart never touches them.
  A batch that never drains is refreshed every 3 minutes (`REFRESH_EVERY` in
  `downloader.py`); when the queue goes quiet for a few seconds the last track is
  flushed straight away. Each restart costs a couple of seconds of Liner's
  playback. `app_pid` finds the process through Electron's `SingletonLock`.
  Nothing happens when Liner is closed: the next start reads the file anyway.
* **Do not remove tracks in Liner while a batch is importing.** A running Liner
  writes `library.json` from its cached copy when you import or remove there, and
  that drops records added underneath it. mdownload merges its rows back if a
  restart sees the index shrink, but the two sides writing at once is a race.

To reload Liner by hand at any moment, without touching the queue:

```bash
~/.venvs/download-music/bin/python -c "import time; from mdownload import liner; \
from mdownload.config import load; print(liner.refresh_app(liner.store(load().dest_dir), time.time()))"
```

Any other destination folder is left alone — mdownload works identically on a
machine without Liner.

### Cleaning up duplicates you already have

`f12` opens the duplicate finder. It groups tracks two ways: the same normalised
name (including across subfolders), and files with identical heads even when the
names differ. The best copy is kept by container preference — `flac`, `wav`,
`m4a`, `mp3`, then the lossy stream formats — then larger size, then shorter
name.

Nothing is deleted on one keystroke: `d` marks the selected group's extra copies
and the second `d` removes them; `D` does the whole library the same way; `r`
cancels the arming or rescans, `o` opens the folder. The status line totals what
would go.

Inside a Liner library this finder is the wrong tool: deleting an `audio/<hash>.mp3`
by hand leaves its `library.json` record pointing at nothing. Remove tracks from
Liner's own UI, or drop the record too.

### Keys

F-keys and `ctrl+q` always work, even while the URL box has focus and is eating
printable keys. The single letters are the fast path once the queue or the
options panel has focus.

| Key | Action |
| --- | --- |
| `ctrl+q` | quit (a modal asks first if jobs are in flight) |
| `f1` / `?` | help overlay |
| `f2` | settings: Spotify credentials, concurrency, match threshold |
| `f3` | destination picker (recent paths, free text) |
| `f4` | browse Spotify / YouTube Music library |
| `f5` | history: search, re-run, export |
| `f6` | export the queue as JSON |
| `f7` / `f8` | pause all / resume all |
| `f9` / `f10` | clear finished / retry failed |
| `f11` | batch mode: one URL per line, `#` for comments |
| `f12` | duplicate finder: clean repeated tracks, `d` twice to delete |
| `enter` (Source) | queue the URL(s); playlists expand |
| `q` | quit (with the queue focused) |
| `a` | queue from the Source box |
| `p` | pause / resume the selected row |
| `c` | cancel selected |
| `r` | retry selected (failed or cancelled) |
| `o` | open the selected job's destination folder |
| `x` | remove the selected row |
| `P` / `R` | pause all / resume all |
| `C` / `F` | clear finished / retry failed |

Browse (`f4`): the first two rows queue your entire Spotify / YT Music liked
library in one pick. `space` marks an item, `a` queues the marked items (the
cursor row when nothing is marked), `A` queues everything in the current view —
open a playlist with `enter` and `A` then queues all of its tracks. `backspace`
backs out, `esc` closes.

History (`f5`): typing filters title / URL / destination, `r` re-queues the
selected row, `d` deletes it, `e` exports the listing as JSON.

### Formats

* **audio-only** — the default: audio stream converted by ffmpeg to mp3 / m4a /
  flac / opus at the chosen bitrate.
* **video+audio** — `bestvideo[height≤Q]+bestaudio`, muxed to MKV; `best` picks
  the best available height.
* **video-only** — video stream only, no audio track.

Switch with the **Format** and **Codec** pickers on the left; the choice is saved
to `config.toml`, so audio-only + mp3 stays the default for every later run.

Codec preference maps to a yt-dlp format filter: `avc1` (H.264, plays
everywhere), `vp9`, `av01`, `h265`.

## Sources

**YouTube Music** works anonymously for search, but liked songs and library
playlists need an auth file. mdownload reads the path in Settings →
*YouTube Music auth json* (default
`~/.config/download-music/ytmusic_oauth.json`). If you have a logged-in
Netscape cookie jar, the older `download_music.py` builds that file from it —
run `musicdl --self-test` / the collector once, or use `mdownload --login`.

**Spotify** needs a client id (and optionally a secret) from
<https://developer.spotify.com/dashboard>. Set them in `f2` → Settings. The
redirect URI must match the one registered for the app — the default is
`http://127.0.0.1:8888/callback`. First authorisation opens a browser and caches
a token at the path in *Spotify token cache*.

Spotify tracks carry no stream, so each is resolved to a YouTube Music match by
title/artist/duration scoring (0.7·title + 0.3·artist, discounted on a duration
mismatch). The threshold is `match_threshold` in Settings; raise it to be
stricter. A track that fails to match is a failed job with the best score in the
log.

## Config

`~/.config/mdownload/config.toml`, rewritten on every change, mode `0600`
because it holds the Spotify secret. CLI flags override the file.

| Key | Meaning |
| --- | --- |
| `format` | `video+audio` / `video-only` / `audio-only` (**default: `audio-only`**) |
| `quality` | `best`, `2160`, `1440`, `1080`, `720`, `480`, `360` (max height, video only) |
| `video_codec` | `any`, `avc1`, `vp9`, `av01`, `h265` |
| `audio_codec` | `mp3`, `m4a`, `flac`, `opus` (**default: `mp3`**) |
| `audio_bitrate` | kbps, e.g. `192` |
| `subtitles` / `subtitle_langs` | write subtitles; comma-separated language list |
| `embed_thumbnail` | download and embed the cover |
| `embed_metadata` | write tags/container metadata |
| `embed_chapters` | embed chapter markers |
| `sponsorblock` | cut sponsor / intro / outro segments |
| `cookies_file` | Netscape jar passed to yt-dlp (age-restricted, private) |
| `cookies_from_browser` | `firefox`, `chrome`, `chromium`, `edge`, `brave`, … |
| `rate_limit` | e.g. `5M` bytes/s — the polite throttle |
| `retries` | per-fragment and whole-file retries |
| `proxy` | e.g. `socks5://127.0.0.1:1080` |
| `concurrent` | simultaneous downloads (default 2) |
| `dest_dir` / `recent_dirs` | output directory and its recent-path history; a Liner library is filed into its store (see *Downloading into Liner*) |
| `spotify_client_id` / `spotify_client_secret` / `spotify_redirect_uri` / `spotify_cache` | Spotify OAuth |
| `ytmusic_auth` | ytmusicapi auth json |
| `match_threshold` | 0–1 minimum score for a Spotify→YouTube match |

State lives in `~/.local/state/mdownload/history.db` (SQLite). Deleting the
file clears the history and nothing else.

## Troubleshooting

### `Unknown command: mdownload` (fish)

The function file is missing or the shell has not autoloaded it.

1. Check the file exists:
   `cat ~/.config/fish/functions/mdownload.fish`
   If it does not, recreate it (see *Install → fish*) or symlink the entry
   point into `PATH`: `ln -sfn ~/.venvs/download-music/bin/mdownload ~/.local/bin/mdownload`.
2. Check `~/.local/bin` is in `PATH`: `echo $PATH | tr ' ' '\n' | grep .local/bin`.
   Add it permanently with `fish_add_path ~/.local/bin`.
3. Autoload the function into the running shell: `exec fish`, or
   `source ~/.config/fish/functions/mdownload.fish`.
4. As a fallback that needs no PATH at all, call the executable directly:
   `~/.venvs/download-music/bin/mdownload`.
5. `fish_config` lists the functions the shell can see; if `mdownload` is not
   there, the file name does not match the function name.

Or just re-run the installer — `install.sh` is idempotent and rewrites both the
symlink and the function.

### `mdownload` not recognized (Windows)

`%LOCALAPPDATA%\mdownload\venv\Scripts` is not on this shell's `PATH`. Open a
**new** terminal (the installer sets the user PATH, but a running shell keeps
its own copy), or check with:

```powershell
[Environment]::GetEnvironmentVariable("Path", "User") -split ";" | Select-String mdownload
```

Call it directly meanwhile:

```powershell
& "$env:LOCALAPPDATA\mdownload\venv\Scripts\mdownload.exe" --self-test
```

Re-running `install.ps1` re-appends the folder if it went missing.

### `python: No module named mdownload`

The package is not installed into the venv that the script runs:

```bash
~/.venvs/download-music/bin/pip install -e ~/mdownload
```

### The TUI will not start

`mdownload: stdout is not a terminal; the TUI needs one.` — you are in a pipe,
cron or a non-interactive SSH command. Use `--self-test`, or `--force-tui` to
override. Also check `mdownload --self-test` for a missing `yt-dlp` / `ffmpeg`.

### Spotify link → `HTTP Error 403`

A pasted `open.spotify.com/...` link is read through the Spotify Web API, not by
`yt-dlp` (yt-dlp cannot read Spotify and answers a bare `403`). The 403 itself
is not a token or scope problem:

- **`spotify: a playlist is not yours — Spotify serves a playlist's tracks only
  to its owner or a collaborator (HTTP 403)`** — the playlist is **followed**,
  not owned. There is no scope, re-login or client-credentials fix. `mdownload`
  reads such a playlist from its public embed page, which lists **at most 100
  tracks**; the status line reports the cut. To get all of it, open the playlist
  in Spotify and *Add to your library → … → Add to other playlist* (a copy in
  your account has no limit), or paste the tracks individually.
- **`spotify: the token was refused ... (HTTP 401)`** — the saved token is gone,
  or lacks a scope. Re-grant it once:
  `mdownload --login` (opens a browser, loopback `127.0.0.1:8888`).
- **`spotify: no client_id`** — fill `client_id` / `client_secret` in `f2`
  settings (or `~/.config/mdownload/config.toml`), then `mdownload --login`.

Owned playlists and albums page the full list through the API; albums use
`/albums/{id}/tracks`.

For a **YouTube Music** playlist, `youtube music: no such a playlist` or a 401/403
means a wrong/private id or an expired cookie jar — re-export the cookies and run
`mdownload --login`.

### Failed jobs

Every failure keeps its message in the Log panel and in `f5` history, and the
job row shows `error`. `r` retries it.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Unsupported URL` | the string is not a supported site | paste the page URL, not an id |
| `Failed to resolve` / `getaddrinfo` | no DNS | check network / `proxy` |
| `This video is unavailable` | deleted, private or region-locked | `cookies_from_browser`, or a `proxy` |
| `Sign in to confirm your age` | needs a session | set `cookies_file` or `cookies_from_browser` |
| `HTTP Error 429` | rate limited | raise `rate_limit` (e.g. `2M`), lower `concurrent` to 1 |
| `ffmpeg not found` | ffmpeg missing from `PATH` | install ffmpeg |
| `Requested format is not available` | that height/codec does not exist — **or** no JavaScript runtime, so YouTube hands back images only | set quality to `best`; check `mdownload --self-test` shows a `js runtime`, else install deno/node/bun and `yt-dlp-ejs` |
| `no YouTube match (best score 0.42)` | Spotify track has no close YT match | lower `match_threshold`, or queue the YouTube URL directly |
| `spotify: no client_id` | not configured | `f2` → Settings, paste the client id |
| Spotify link → `HTTP Error 403` | link read through yt-dlp, or the playlist is not owned by you | links go through the Web API; a followed playlist is read from its public embed (first 100 tracks) — copy it into your own account for all of it |
| Spotify browser opens on every launch | token cache unwritable/missing | correct `spotify_cache`; it is created on first auth |

Batch-downloading with account cookies is what YouTube's abuse detection
watches. Keep `rate_limit` at or above `2M` and `concurrent` at 1–2.

## Layout

```
mdownload/
  pyproject.toml            console script: mdownload = mdownload.app:main
  README.md
  src/mdownload/
    app.py        CLI: flags, backend check, --login, --self-test
    tui.py        the Textual app: options panel, queue table, keybinds
    screens.py    modals: help, Browse, History, destination, Settings, confirm
    downloader.py Job/Control, ThreadPoolExecutor, yt-dlp hooks, playlist expansion
    library.py    destination index, archive, duplicate finder
    liner.py      Liner library adapter: content id, cover, library.json record
    sources.py    Spotify and YouTube Music library browsing
    config.py     TOML load/save, seeded from ~/.config/download-music
    history.py    SQLite history
    engine.py     thin bridge onto the vendored _engine.py
    _engine.py    vendored copy of ~/download_music.py (matching + ytmusic auth)
    styles.tcss   dark theme
```

`_engine.py` is a verbatim copy of `~/download_music.py`, so a clone runs with
nothing beside it; `engine.py` re-exports the names the package uses. When the
original changes, re-copy it:

```bash
cp ~/download_music.py src/mdownload/_engine.py
```
