#!/usr/bin/env bash
# mdownload - Linux / macOS setup.  Idempotent: re-run to upgrade in place.
#
#   curl -fsSL <raw-url>/install.sh | bash        (or: ./install.sh from a clone)
#   PYTHON=python3.12 MDOWNLOAD_VENV=~/venvs/md ./install.sh
set -euo pipefail

cd "$(dirname "$0")" 2>/dev/null || true

PY=${PYTHON:-python3}
VENV=$(eval echo "${MDOWNLOAD_VENV:-$HOME/.venvs/download-music}")
BIN="$HOME/.local/bin"

say()  { printf '%s\n' "$*"; }
die()  { printf 'mdownload: %s\n' "$*" >&2; exit 1; }

command -v "$PY" >/dev/null 2>&1 || die "$PY not found (set PYTHON=...)"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python 3.11+ required, $("$PY" -V 2>&1) found"

say "==> venv $VENV"
"$PY" -m venv "$VENV" >/dev/null 2>&1 || "$PY" -m venv --without-pip "$VENV"
VPY="$VENV/bin/python"
[ -x "$VPY" ] || die "no python in $VENV"

say "==> installing mdownload"
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet --upgrade -e .

say "==> console script -> $BIN/mdownload"
mkdir -p "$BIN"
ln -sfn "$VENV/bin/mdownload" "$BIN/mdownload"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "    NOTE: $BIN is not on PATH - add it:"
     say "      bash/zsh: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
     say "      fish:     fish_add_path ~/.local/bin" ;;
esac

# fish autoloads functions from ~/.config/fish/functions, so a fresh fish
# session gets `mdownload` even when ~/.local/bin is not on PATH.
if command -v fish >/dev/null 2>&1; then
  FD="$HOME/.config/fish/functions"
  mkdir -p "$FD"
  cat > "$FD/mdownload.fish" <<EOF
function mdownload --description 'Full-screen TUI media downloader (yt-dlp + Spotify / YouTube Music)'
    set -l exe $VENV/bin/mdownload
    if not test -x \$exe
        echo "mdownload: \$exe not found." >&2
        echo "Reinstall: see README 'Install'." >&2
        return 3
    end
    command \$exe \$argv
end
EOF
  say "==> fish function $FD/mdownload.fish (new sessions; \`exec fish\` now)"
fi

command -v ffmpeg >/dev/null 2>&1 || say "    NOTE: ffmpeg missing - muxing audio fails without it
      Debian/Ubuntu: sudo apt install ffmpeg
      Fedora:        sudo dnf install ffmpeg   (rpmfusion)
      Arch:          sudo pacman -S ffmpeg
      macOS:         brew install ffmpeg"

if ! command -v deno >/dev/null 2>&1 && ! command -v node >/dev/null 2>&1 \
   && ! command -v bun >/dev/null 2>&1; then
  say "    NOTE: no JavaScript runtime - YouTube returns images only
      curl -fsSL https://deno.land/install.sh | sh   (or install node/bun)"
fi

say "==> self-test"
"$BIN/mdownload" --self-test || die "--self-test failed"
say "done.  Run: mdownload        (or: $VENV/bin/mdownload)"
