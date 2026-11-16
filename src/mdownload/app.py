"""CLI entry point for mdownload.

Only the standard library is imported at module level so ``mdownload --version``
works even when textual / yt-dlp are missing.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import __version__, config as config_mod

INSTALL = """\
mdownload needs two backends:

  yt-dlp   the download engine
  ffmpeg   muxing, audio conversion, thumbnails
  deno     JavaScript runtime + yt-dlp-ejs, to solve YouTube challenges
           (node or bun work too)

Install them into the same environment as mdownload:

  python -m pip install -U yt-dlp yt-dlp-ejs textual spotipy ytmusicapi
  # ffmpeg comes from your package manager:
  #   Debian/Ubuntu   sudo apt install ffmpeg
  #   Fedora          sudo dnf install ffmpeg
  #   Arch            sudo pacman -S ffmpeg
  #   macOS           brew install ffmpeg
  # the JS runtime:
  #   curl -fsSL https://deno.land/install.sh | sh     # or: apt/dnf/pacman install nodejs

If mdownload itself is missing from the shell, see README "Unknown command"
(entry point on PATH, or the fish function in
~/.config/fish/functions/mdownload.fish).
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mdownload",
        description="Full-screen TUI media downloader (yt-dlp + Spotify / YT Music).",
        epilog="Running with no arguments opens the TUI.",
    )
    p.add_argument("urls", nargs="*", help="URLs to preload into the queue")
    p.add_argument("--version", action="version", version=f"mdownload {__version__}")
    p.add_argument("--config", type=Path, default=None,
                   help="config file (default ~/.config/mdownload/config.toml)")
    p.add_argument("--dest", "-d", default=None, help="destination directory")
    p.add_argument("--format", "-f", choices=["video+audio", "video-only", "audio-only"],
                   default=None, help="output format")
    p.add_argument("--quality", "-q", default=None,
                   help="max height: best|2160|1440|1080|720|480|360")
    p.add_argument("--concurrent", "-j", type=int, default=None,
                   help="simultaneous downloads (default from config, 2)")
    p.add_argument("--login", action="store_true",
                   help="authorise Spotify / YT Music once, then exit")
    p.add_argument("--self-test", action="store_true",
                   help="check imports, config and history store, then exit")
    p.add_argument("--force-tui", action="store_true",
                   help="start the TUI even when stdout is not a terminal")
    return p


def backend_error() -> str | None:
    try:
        import yt_dlp  # noqa: F401
    except ImportError as exc:
        return f"yt-dlp is not importable ({exc})"
    if shutil.which("ffmpeg") is None:
        return "ffmpeg is not on PATH (audio conversion and muxing will fail)"
    return None


def _apply_overrides(cfg, args) -> None:
    if args.dest:
        cfg.dest_dir = str(Path(args.dest).expanduser())
        cfg.remember_dir(cfg.dest_dir)
    if args.format:
        cfg.format = args.format
    if args.quality:
        cfg.quality = args.quality
    if args.concurrent:
        cfg.concurrent = max(1, args.concurrent)


def self_test(cfg) -> int:
    from .downloader import DownloadManager, js_runtimes
    from .history import History

    h = History()
    n = len(h.search("", limit=1))
    h.close()
    err = backend_error()
    print(f"mdownload {__version__} self-test ok")
    print(f"  python      {sys.version.split()[0]}")
    print(f"  config      {config_mod.CONFIG_PATH}")
    print(f"  history     {config_mod.HISTORY_PATH} ({n or 'no'} rows)")
    print(f"  dest        {cfg.dest_dir}")
    print(f"  concurrent  {cfg.concurrent}")
    if err:
        print(f"  backend     MISSING: {err}")
        return 3
    import yt_dlp

    print(f"  yt-dlp      {yt_dlp.version.__version__}")
    print(f"  ffmpeg      {shutil.which('ffmpeg')}")
    runtimes = js_runtimes()
    if runtimes:
        print(f"  js runtime  {next(iter(runtimes))} ({shutil.which(next(iter(runtimes)))})")
    else:
        print("  js runtime  MISSING: no deno/node/bun on PATH — YouTube will "
              "return no formats (see INSTALL; yt-dlp-ejs is also required)")
    return 0


def do_login(cfg) -> int:
    from . import sources

    return 0 if sources.login(cfg) else 4


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    path = Path(args.config).expanduser() if args.config else config_mod.CONFIG_PATH
    try:
        cfg = config_mod.load(path)
    except Exception as exc:  # noqa: BLE001
        print(f"mdownload: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    _apply_overrides(cfg, args)

    if args.self_test:
        return self_test(cfg)
    if args.login:
        return do_login(cfg)

    try:
        import textual  # noqa: F401
    except ImportError:
        print("mdownload: the TUI needs textual.\n", file=sys.stderr)
        print(INSTALL, file=sys.stderr)
        return 3

    err = backend_error()
    if err:
        print(f"mdownload: {err}\n", file=sys.stderr)
        print(INSTALL, file=sys.stderr)
        return 3

    if not sys.stdout.isatty() and not args.force_tui:
        print("mdownload: stdout is not a terminal; the TUI needs one.", file=sys.stderr)
        print("  use --self-test to check the install, or --force-tui to override.",
              file=sys.stderr)
        return 2

    from .tui import run

    return run(cfg, args.urls)


if __name__ == "__main__":
    raise SystemExit(main())
