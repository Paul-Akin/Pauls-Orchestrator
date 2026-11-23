from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from hub.memory.notes import (
    atomic_write,
    chunk_note,
    iter_note_paths,
    new_note_path,
    path_for_id,
    read_note,
    render_note,
)
from hub.memory.protocol import Sensitivity

P, PE, PR = Sensitivity.PUBLIC, Sensitivity.PERSONAL, Sensitivity.PRIVATE


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    ("rel", "front", "expected"),
    [
        ("note.md", None, PR),  # nothing at all: fail closed
        ("note.md", "sensitivity: public", P),
        ("note.md", "sensitivity: Personal", PE),
        ("note.md", "sensitivity: secret-ish", PR),  # unknown value: fail closed
        ("public/note.md", None, P),
        ("personal/note.md", None, PE),
        ("private/note.md", "sensitivity: public", PR),  # most restrictive wins
        ("public/note.md", "sensitivity: private", PR),
        ("projects/note.md", None, PR),  # unknown folder: fail closed
        ("note.md", "title: [unclosed", PR),  # broken YAML: fail closed
    ],
)
def test_sensitivity_resolution(
    tmp_path: Path, rel: str, front: str | None, expected: Sensitivity
) -> None:
    text = f"---\n{front}\n---\nbody\n" if front else "body\n"
    note = read_note(tmp_path, write(tmp_path, rel, text))
    assert note.sensitivity is expected


def test_front_matter_fields(tmp_path: Path) -> None:
    p = write(
        tmp_path,
        "personal/bolts.md",
        "---\ntitle: Bolt preload\ntags: [fasteners, nx]\ncreated: 2026-01-02T03:04:05Z\n"
        "source: cli\n---\n# Ignored heading\nUse 75% of proof load.\n",
    )
    n = read_note(tmp_path, p)
    assert n.id == "personal/bolts"
    assert n.title == "Bolt preload"
    assert n.tags == ("fasteners", "nx")
    assert n.created == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert n.source == "cli"
    assert n.body.endswith("Use 75% of proof load.")


def test_title_falls_back_to_heading_then_filename(tmp_path: Path) -> None:
    assert read_note(tmp_path, write(tmp_path, "a.md", "# Heading Title\ntext")).title == (
        "Heading Title"
    )
    assert read_note(tmp_path, write(tmp_path, "my-note_x.md", "text")).title == "my note x"


def test_chunking_by_heading_path_and_size(tmp_path: Path) -> None:
    body = (
        "intro para\n\n# Bolts\nbolt text\n\n## Preload\n"
        + ("word " * 150).strip()
        + "\n\n"
        + ("more " * 150).strip()
        + "\n\n# Welds\n```\n# not a heading inside a fence\n```\nweld text\n"
    )
    n = read_note(tmp_path, write(tmp_path, "public/n.md", body))
    chunks = chunk_note(n, max_chars=800)
    headings = [c.heading for c in chunks]
    assert headings[0] == ""
    assert "Bolts" in headings
    assert headings.count("Bolts > Preload") == 2  # 2 x ~750 chars cannot share one chunk
    assert "Welds" in headings
    assert all(len(c.text) <= 800 for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_giant_paragraph_is_hard_split(tmp_path: Path) -> None:
    n = read_note(tmp_path, write(tmp_path, "public/n.md", "x" * 5000))
    chunks = chunk_note(n, max_chars=1000)
    assert len(chunks) == 5
    assert all(len(c.text) <= 1000 for c in chunks)


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "/abs/path", "a/../../x", "", "c:/windows", "a\\..\\b", "x\x00y"]
)
def test_path_for_id_rejects_escapes(tmp_path: Path, bad: str) -> None:
    assert path_for_id(tmp_path, bad) is None


def test_path_for_id_roundtrip(tmp_path: Path) -> None:
    p = write(tmp_path, "personal/sub/n.md", "x")
    n = read_note(tmp_path, p)
    assert path_for_id(tmp_path, n.id) == p


def test_iter_skips_hidden(tmp_path: Path) -> None:
    write(tmp_path, "a.md", "x")
    write(tmp_path, ".obsidian/b.md", "x")
    write(tmp_path, "sub/.hidden.md", "x")
    write(tmp_path, "sub/c.txt", "x")
    assert [p.name for p in iter_note_paths(tmp_path)] == ["a.md"]
    assert iter_note_paths(tmp_path / "missing") == []


def test_render_and_new_path_roundtrip(tmp_path: Path) -> None:
    created = datetime(2026, 11, 23, 14, 5, 9, tzinfo=UTC)
    path = new_note_path(tmp_path, "Bolt Preload: rule!", created, PR)
    assert path == tmp_path / "private" / "2026-11-23-bolt-preload-rule.md"
    text = render_note(
        title="Bolt Preload: rule!", body="75% proof", sensitivity=PR, tags=("a",),
        created=created, source="cli",
    )  # fmt: skip
    atomic_write(path, text)
    n = read_note(tmp_path, path)
    assert (n.title, n.sensitivity, n.tags, n.source) == ("Bolt Preload: rule!", PR, ("a",), "cli")
    assert n.created == created
    path.touch()
    assert new_note_path(tmp_path, "Bolt Preload: rule!", created, PR).name.endswith("-2.md")
    assert not list(path.parent.glob(".tmp-*"))  # no temp files left behind
