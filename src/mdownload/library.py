"""What is already on disk: the dedupe index, and the duplicate finder.

Names come from two sources that rarely agree — yt-dlp writes `%(title)s` from
YouTube, Spotify gives `artist - title` — so a match is a normalised token
overlap, not string equality.  Two caches back it:

  * `<dest>/.mdownload-archive.txt` — ids this tool already fetched, with the
    file each one produced, so a re-queued playlist costs no network at all;
  * a scan of `<dest>` for media files, extended as jobs finish.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

MEDIA_EXT = frozenset({".mp3", ".m4a", ".flac", ".opus", ".ogg", ".oga", ".wav",
                       ".aac", ".wma", ".m4b", ".mp4", ".mkv", ".webm", ".m4v"})
# best copy first when picking what to keep out of a duplicate group
PREFERRED = (".flac", ".wav", ".m4a", ".mp3", ".opus", ".ogg", ".oga", ".aac",
             ".wma", ".mkv", ".mp4", ".m4v", ".webm")
ARCHIVE_NAME = ".mdownload-archive.txt"
# anything over this is hashed only when a same-size twin exists
HASH_MIN = 1024

_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NOISE = re.compile(
    r"\b(official(?: music)? (?:audio|video)|official|lyrics?|audio|video|"
    r"visuali[sz]er|full album|hd|hq|uhd|4k|remaster(?:ed)?|explicit|clean|"
    r"feat|ft|prod)\b")
_NONWORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def norm(text: str) -> str:
    """Lower-case, drop bracketed decorations and release junk, keep words."""
    s = str(text or "").lower()
    s = _BRACKETS.sub(" ", s)
    s = _NOISE.sub(" ", s)
    s = _NONWORD.sub(" ", s)
    return _SPACE.sub(" ", s).strip()


def tokens(text: str) -> frozenset[str]:
    return frozenset(norm(text).split())


def similar(a: frozenset[str], b: frozenset[str], floor: float = 0.8) -> bool:
    """Share at least `floor` of the shorter title's words."""
    if not a or not b:
        return False
    return len(a & b) / min(len(a), len(b)) >= floor


def _rank(path: Path) -> tuple[int, int, int]:
    try:
        ext = path.suffix.lower()
        pref = PREFERRED.index(ext)
    except ValueError:
        pref = len(PREFERRED)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    # no "· (1)" leftovers, no ".part" — shorter name wins the tie
    return (pref, -size, len(path.name))


@dataclass
class Group:
    """One track that exists more than once on disk."""

    key: str
    keep: Path
    dupes: list[Path] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        total = 0
        for p in self.dupes:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total


def _head_hash(path: Path, limit: int = 262144) -> str:
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()


class Library:
    """The destination folder seen as a set of tracks already downloaded."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.archive_path = self.root / ARCHIVE_NAME
        self._files: list[Path] | None = None
        self._tok: list[tuple[frozenset[str], Path]] = []
        self._ids: dict[str, str] | None = None

    # ---------------------------------------------------------- file index
    def scan(self) -> list[Path]:
        if self._files is None:
            out: list[Path] = []
            try:
                for p in self.root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in MEDIA_EXT \
                            and not p.name.endswith(".part"):
                        out.append(p)
            except OSError:
                pass
            self._files = out
            self._tok = [(tokens(p.stem), p) for p in out]
        return self._files

    def rescore(self) -> None:
        self._files = None
        self.scan()

    def add_file(self, path: str | Path) -> None:
        p = Path(path)
        # the download hook reports the pre-postprocess file (.webm next to the
        # .mp3) - only index what is actually on disk
        if not p.suffix or p.suffix.lower() not in MEDIA_EXT or not p.is_file():
            return
        if self._files is None:
            self.scan()
            return
        self._files.append(p)
        self._tok.append((tokens(p.stem), p))

    def match(self, text: str, floor: float = 0.8) -> Path | None:
        """The file this track already exists as, if any."""
        want = tokens(text)
        if not want:
            return None
        self.scan()
        best, score = None, 0.0
        for have, path in self._tok:
            if have == want:
                inter = 1.0
            elif similar(want, have, floor):
                inter = len(want & have) / min(len(want), len(have))
            else:
                continue
            # same confidence: prefer the better container (.mp3 over .webm)
            if inter > score or (inter == score and _rank(path) < _rank(best)):
                best, score = path, inter
        return best

    # ------------------------------------------------------------- archive
    def ids(self) -> dict[str, str]:
        if self._ids is None:
            out: dict[str, str] = {}
            try:
                for line in self.archive_path.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        out[parts[1]] = parts[2] if len(parts) > 2 else ""
            except OSError:
                pass
            self._ids = out
        return self._ids

    def has_id(self, vid: str) -> bool:
        if not vid:
            return False
        path = self.ids().get(vid)
        if path is None:
            return False
        if not path:            # recorded before the file name was known
            return True
        p = Path(path)
        p = p if p.is_absolute() else self.root / p
        return p.exists()

    def note_id(self, vid: str, path: str | Path = "") -> None:
        if not vid:
            return
        ids = self.ids()
        if path:
            p = Path(str(path))
            try:
                path = str(p.relative_to(self.root))
            except ValueError:
                path = str(p)
        if vid in ids and ids[vid] == str(path):
            return
        ids[vid] = str(path)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as fh:
                fh.write(f"youtube {vid}\t{path}\n")
        except OSError as exc:  # a read-only library must not kill the job
            import logging
            logging.getLogger("mdownload.library").warning(
                "archive write failed: %s", exc)

    # ---------------------------------------------------------- duplicates
    def duplicates(self) -> list[Group]:
        """Group files that are the same track: same name, or same bytes."""
        files = list(self.scan())
        groups: dict[str, list[Path]] = {}
        for p in files:
            key = norm(p.stem)
            if key:
                groups.setdefault(key, []).append(p)

        out: list[Group] = []
        claimed: set[Path] = set()
        for key, paths in groups.items():
            if len(paths) > 1:
                ordered = sorted(paths, key=_rank)
                out.append(Group(key=key, keep=ordered[0], dupes=ordered[1:]))
                claimed.update(paths)

        # different names, identical bytes: only worth hashing same-size pairs
        by_size: dict[int, list[Path]] = {}
        for p in files:
            if p in claimed:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= HASH_MIN:
                by_size.setdefault(size, []).append(p)
        for size, paths in by_size.items():
            if len(paths) < 2:
                continue
            buckets: dict[str, list[Path]] = {}
            for p in paths:
                try:
                    buckets.setdefault(_head_hash(p), []).append(p)
                except OSError:
                    continue
            for digest, same in buckets.items():
                if len(same) > 1:
                    ordered = sorted(same, key=_rank)
                    out.append(Group(key=ordered[0].stem, keep=ordered[0],
                                     dupes=ordered[1:]))
        return sorted(out, key=lambda g: (-g.bytes, g.key))

    @staticmethod
    def delete(paths: list[Path]) -> tuple[int, list[str]]:
        """Remove duplicate copies.  Returns (removed, errors)."""
        removed, errors = 0, []
        for p in paths:
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                errors.append(f"{p}: {exc}")
        return removed, errors
