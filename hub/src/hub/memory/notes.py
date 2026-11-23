"""Markdown notes: the memory's source of truth (docs/adr/0005).

A note is a UTF-8 ``.md`` file under the memory directory with optional YAML
front matter::

    ---
    title: Bolt preload rule of thumb
    sensitivity: personal        # public | personal | private
    tags: [fasteners, nx]
    created: 2026-11-23T14:05:09Z
    source: cli
    ---
    Body in markdown...

**Id** = the file's path relative to the memory dir, POSIX style, without
``.md`` (``inbox/2026-11-23-bolt-preload``). Hand-edited files need no id field,
and the id is stable for as long as the file isn't moved.

**Sensitivity fails closed.** It is the *most* restrictive of:

* the front matter ``sensitivity`` (if present and valid), and
* the top-level folder: ``private/``, ``personal/`` or ``public/``.

If neither gives a value (or the front matter is unreadable), the note is
PRIVATE, so a note you forgot to tag is only ever shown to local models.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from hub.memory.protocol import Sensitivity

FOLDER_SENSITIVITY = {
    "private": Sensitivity.PRIVATE,
    "personal": Sensitivity.PERSONAL,
    "public": Sensitivity.PUBLIC,
}
MAX_NOTE_BYTES = 1_000_000
_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    heading: str
    """Heading path, e.g. 'Bolts > Preload'; empty for text before the first heading."""
    text: str


@dataclass(frozen=True)
class Note:
    id: str
    path: Path
    title: str
    body: str
    sensitivity: Sensitivity
    tags: tuple[str, ...]
    created: datetime
    source: str
    sha256: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- ids


def note_id(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve())
    return str(PurePosixPath(*rel.parts).with_suffix(""))


def path_for_id(root: Path, item_id: str) -> Path | None:
    """Map an id back to a file, refusing anything that escapes ``root``."""
    if not item_id or "\x00" in item_id or item_id.startswith(("/", "\\")):
        return None
    parts = PurePosixPath(item_id).parts
    if any(p in {"..", "."} or ":" in p or "\\" in p for p in parts):
        return None
    candidate = (root / Path(*parts)).with_suffix(".md")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def slugify(text: str, max_len: int = 48) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "note"


# --------------------------------------------------------------------------- parse


def _split_front_matter(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """Returns (front_matter or None, body, error)."""
    m = _FRONT_MATTER.match(text)
    if not m:
        return None, text, None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, text[m.end() :], "front matter is not valid YAML"
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, text[m.end() :], "front matter is not a mapping"
    return data, text[m.end() :], None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def resolve_sensitivity(
    front: dict[str, Any] | None, rel_parts: tuple[str, ...], *, front_unreadable: bool = False
) -> tuple[Sensitivity, list[str]]:
    """Most restrictive of front matter and top-level folder; PRIVATE if neither.

    Unreadable front matter forces PRIVATE even inside ``public/``: the broken
    block may well have been a mistyped ``sensitivity: private``.
    """
    warnings: list[str] = []
    candidates: list[Sensitivity] = []
    if front_unreadable:
        warnings.append("unreadable front matter; treated as private")
        candidates.append(Sensitivity.PRIVATE)
    if front is not None and "sensitivity" in front:
        parsed = Sensitivity.parse(front["sensitivity"])
        if parsed is None:
            warnings.append(f"unknown sensitivity {front['sensitivity']!r}; treated as private")
            candidates.append(Sensitivity.PRIVATE)
        else:
            candidates.append(parsed)
    if len(rel_parts) > 1 and rel_parts[0].lower() in FOLDER_SENSITIVITY:
        candidates.append(FOLDER_SENSITIVITY[rel_parts[0].lower()])
    if not candidates:
        return Sensitivity.PRIVATE, warnings
    return max(candidates), warnings


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group(2).strip()
    return None


def parse_note(root: Path, path: Path, raw: bytes) -> Note:
    sha = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace")
    front, body, error = _split_front_matter(text)
    warnings = [error] if error else []
    rel_parts = path.resolve().relative_to(root.resolve()).parts
    sensitivity, sens_warn = resolve_sensitivity(
        front, rel_parts, front_unreadable=error is not None
    )
    warnings += sens_warn
    fm = front or {}

    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _first_heading(body) or path.stem.replace("-", " ").replace("_", " ")
    raw_tags = fm.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",")]
    tags = tuple(str(t) for t in raw_tags if str(t).strip()) if isinstance(raw_tags, list) else ()
    created = _parse_datetime(fm.get("created")) or datetime.fromtimestamp(
        path.stat().st_mtime, tz=UTC
    )
    source = fm.get("source")
    return Note(
        id=note_id(root, path),
        path=path,
        title=title.strip(),
        body=body.strip("\n"),
        sensitivity=sensitivity,
        tags=tags,
        created=created,
        source=source if isinstance(source, str) else "file",
        sha256=sha,
        warnings=tuple(warnings),
    )


def read_note(root: Path, path: Path) -> Note:
    size = path.stat().st_size
    if size > MAX_NOTE_BYTES:
        raise ValueError(f"note larger than {MAX_NOTE_BYTES} bytes")
    return parse_note(root, path, path.read_bytes())


def iter_note_paths(root: Path) -> list[Path]:
    """All ``*.md`` files under root, skipping hidden files/dirs (e.g. .obsidian, .git)."""
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*.md"):
        rel = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel) or not p.is_file():
            continue
        out.append(p)
    return sorted(out)


# --------------------------------------------------------------------------- chunk


def chunk_note(note: Note, max_chars: int) -> list[Chunk]:
    """Split by markdown headings, then pack paragraphs up to ``max_chars``.

    Each chunk keeps its heading path so a hit can be shown and embedded with
    its context ("Bolts > Preload"), not as an orphan paragraph.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    stack: list[tuple[int, str]] = []
    in_fence = False
    for line in note.body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            stack = [(lvl, t) for lvl, t in stack if lvl < level] + [(level, title)]
            names = [t for _, t in stack]
            if names and names[0] == note.title:  # "# Title" duplicates the note title
                names = names[1:]
            sections.append((" > ".join(names), []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for heading, lines in sections:
        paragraphs = [p.strip() for p in "\n".join(lines).split("\n\n") if p.strip()]
        buf = ""
        for para in paragraphs:
            while len(para) > max_chars:  # hard-split a giant paragraph
                if buf:
                    chunks.append(Chunk(len(chunks), heading, buf))
                    buf = ""
                cut = para.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else max_chars
                chunks.append(Chunk(len(chunks), heading, para[:cut].strip()))
                para = para[cut:].strip()
            if buf and len(buf) + 2 + len(para) > max_chars:
                chunks.append(Chunk(len(chunks), heading, buf))
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            chunks.append(Chunk(len(chunks), heading, buf))
    return chunks


def embedding_text(note: Note, chunk: Chunk) -> str:
    """What gets embedded: title and heading give the vector its context."""
    head = note.title if not chunk.heading else f"{note.title} > {chunk.heading}"
    return f"{head}\n\n{chunk.text}"


# --------------------------------------------------------------------------- write


def render_note(
    *,
    title: str,
    body: str,
    sensitivity: Sensitivity,
    tags: tuple[str, ...],
    created: datetime,
    source: str,
) -> str:
    front = {
        "title": title,
        "sensitivity": sensitivity.label,
        "tags": list(tags),
        "created": created.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "source": source,
    }
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n{body.strip()}\n"


def atomic_write(path: Path, content: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written note."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def new_note_path(root: Path, title: str, created: datetime, sensitivity: Sensitivity) -> Path:
    """<sensitivity>/<date>-<slug>.md, with -2, -3... on collision.

    Filing by sensitivity folder means the folder and front matter agree, so a
    later edit that drops the front matter still leaves the note protected.
    """
    base = f"{created.astimezone(UTC):%Y-%m-%d}-{slugify(title)}"
    folder = root / sensitivity.label
    candidate = folder / f"{base}.md"
    n = 2
    while candidate.exists():
        candidate = folder / f"{base}-{n}.md"
        n += 1
    return candidate
