"""Tests for :mod:`stackroom.ingest.discover`.

The three things that must never drift are document *order*, document *slugs*
and the *digest* they are deduplicated by, because all three end up in published
URLs and in the manifest. Most of what follows is checking those, one property
at a time, on a folder built in the test itself.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import pytest

from stackroom.ingest.discover import (
    SourceFile,
    discover,
    natural_key,
    slugify,
)

PDF = b"%PDF-1.4\n% a header is all discover ever reads\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def write(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def slugs(files: list[SourceFile]) -> list[str]:
    return [f.slug for f in files]


def names(files: list[SourceFile]) -> list[str]:
    return [f.path.name for f in files]


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------


def test_natural_key_reads_digit_runs_as_numbers() -> None:
    given = ["doc10.pdf", "doc2.pdf", "doc1.pdf", "doc100.pdf", "doc20.pdf"]
    assert sorted(given, key=natural_key) == [
        "doc1.pdf",
        "doc2.pdf",
        "doc10.pdf",
        "doc20.pdf",
        "doc100.pdf",
    ]


def test_documents_come_back_in_natural_order(tmp_path: Path) -> None:
    for n in (1, 2, 10, 11, 100):
        write(tmp_path, f"doc{n}.pdf", PDF + str(n).encode())
    usable, _ = discover(tmp_path)
    assert names(usable) == ["doc1.pdf", "doc2.pdf", "doc10.pdf", "doc11.pdf", "doc100.pdf"]


def test_directories_order_before_the_files_inside_them(tmp_path: Path) -> None:
    write(tmp_path, "box2/page1.pdf", PDF + b"a")
    write(tmp_path, "box10/page1.pdf", PDF + b"b")
    write(tmp_path, "box2/page10.pdf", PDF + b"c")
    usable, _ = discover(tmp_path)
    assert [f.path.relative_to(tmp_path).as_posix() for f in usable] == [
        "box2/page1.pdf",
        "box2/page10.pdf",
        "box10/page1.pdf",
    ]


def test_order_and_slugs_do_not_depend_on_creation_order(tmp_path: Path) -> None:
    """Two folders with the same names, created in different orders, agree.

    This is guarantee 6 in miniature. If the walk order leaked into the output,
    two people publishing the same documents would publish different URLs.
    """
    relatives = [f"case{n}/exhibit{m}.pdf" for n in (1, 2, 10) for m in (1, 2, 10)]
    payloads = {r: PDF + r.encode() for r in relatives}

    forward = tmp_path / "forward"
    backward = tmp_path / "backward"
    for r in relatives:
        write(forward, r, payloads[r])
    for r in reversed(relatives):
        write(backward, r, payloads[r])

    a, _ = discover(forward)
    b, _ = discover(backward)
    assert [f.path.relative_to(forward).as_posix() for f in a] == [
        f.path.relative_to(backward).as_posix() for f in b
    ]
    assert slugs(a) == slugs(b)


def test_repeated_walks_of_one_folder_agree(tmp_path: Path) -> None:
    for n in range(12):
        write(tmp_path, f"sub{n % 3}/file{n}.pdf", PDF + bytes([n]))
    first, _ = discover(tmp_path)
    second, _ = discover(tmp_path)
    assert [(f.slug, f.sha256, str(f.path)) for f in first] == [
        (f.slug, f.sha256, str(f.path)) for f in second
    ]


# --------------------------------------------------------------------------
# digests and duplicates
# --------------------------------------------------------------------------


def test_digest_is_streamed_but_still_correct(tmp_path: Path) -> None:
    """Three megabytes, hashed a megabyte at a time, must equal hashlib's answer."""
    blob = PDF + random.Random(4).randbytes(3 * (1 << 20))
    path = write(tmp_path, "big.pdf", blob)
    usable, _ = discover(tmp_path)
    assert usable[0].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert usable[0].size == len(blob)


def test_identical_bytes_collapse_to_one_document(tmp_path: Path) -> None:
    write(tmp_path, "release/memo.pdf", PDF + b"same")
    write(tmp_path, "release/memo copy.pdf", PDF + b"same")
    write(tmp_path, "second-production/annex.pdf", PDF + b"same")
    write(tmp_path, "release/other.pdf", PDF + b"different")

    usable, skipped = discover(tmp_path)
    assert names(usable) == ["memo copy.pdf", "other.pdf"]

    survivor = usable[0]
    assert sorted(p.name for p in survivor.aliases) == ["annex.pdf", "memo.pdf"]

    duplicates = [f for f in skipped if f.is_duplicate]
    assert sorted(f.path.name for f in duplicates) == ["annex.pdf", "memo.pdf"]
    assert all(f.duplicate_of == survivor.path for f in duplicates)
    assert all(f.sha256 == survivor.sha256 for f in duplicates)
    assert "duplicate of" in duplicates[0].reason


def test_the_surviving_copy_is_the_first_in_collection_order(tmp_path: Path) -> None:
    write(tmp_path, "b.pdf", PDF + b"same")
    write(tmp_path, "a.pdf", PDF + b"same")
    usable, skipped = discover(tmp_path)
    assert names(usable) == ["a.pdf"]
    assert skipped[0].path.name == "b.pdf"


# --------------------------------------------------------------------------
# slugs
# --------------------------------------------------------------------------


def test_colliding_names_get_numbered_suffixes(tmp_path: Path) -> None:
    for box in ("box1", "box2", "box3"):
        write(tmp_path, f"{box}/Report.pdf", PDF + box.encode())
    usable, _ = discover(tmp_path)
    assert slugs(usable) == ["report", "report-2", "report-3"]


def test_slug_stays_within_the_cap_even_with_a_suffix(tmp_path: Path) -> None:
    long = "x" * 90
    for box in ("a", "b"):
        write(tmp_path, f"{box}/{long}.pdf", PDF + box.encode())
    usable, _ = discover(tmp_path)
    assert all(len(f.slug) <= 60 for f in usable)
    assert usable[1].slug.endswith("-2")
    assert usable[0].slug != usable[1].slug


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Отчёт о расследовании.pdf", "otchet-o-rassledovanii"),  # noqa: RUF001
        ("МЕМОРАНДУМ №17.pdf", "memorandum-no17"),
        ("Київ 2021.pdf", "kiiv-2021"),
        ("Rapport Financier — Été 2019.pdf", "rapport-financier-ete-2019"),
        ("Ünïcödé  file__name.pdf", "unicode-file-name"),
        ("doc2.pdf", "doc2"),
    ],
)
def test_slugs_are_readable_ascii(tmp_path: Path, filename: str, expected: str) -> None:
    write(tmp_path, filename, PDF)
    usable, _ = discover(tmp_path)
    assert usable[0].slug == expected


def test_a_name_of_pure_punctuation_falls_back_to_the_digest(tmp_path: Path) -> None:
    write(tmp_path, "…‽…….pdf", PDF + b"one")
    write(tmp_path, "文書.pdf", PDF + b"two")
    usable, _ = discover(tmp_path)
    for source in usable:
        assert source.slug == f"doc-{source.sha256[:8]}"
    assert usable[0].slug != usable[1].slug


def test_slugify_is_a_pure_function_of_the_name() -> None:
    assert slugify("A  B__C--D") == "a-b-c-d"
    assert slugify("---trimmed---") == "trimmed"
    assert slugify("", digest="deadbeefcafe") == "doc-deadbeef"


# --------------------------------------------------------------------------
# what gets in
# --------------------------------------------------------------------------


def test_kind_comes_from_the_bytes_not_the_extension(tmp_path: Path) -> None:
    write(tmp_path, "actually-a-png.pdf", PNG)
    write(tmp_path, "actually-a-pdf.bin", PDF)
    write(tmp_path, "lying.pdf", b"Dear colleague,\n\nplease find attached.\n")
    write(tmp_path, "notes.txt", b"a plain note\n")

    usable, skipped = discover(tmp_path)
    kinds = {f.path.name: f.kind for f in usable}
    assert kinds == {
        "actually-a-png.pdf": "image",
        "actually-a-pdf.bin": "pdf",
        "notes.txt": "text",
    }
    liar = next(f for f in skipped if f.path.name == "lying.pdf")
    assert liar.kind == "unsupported"
    assert "no PDF header" in liar.reason


def test_zero_byte_files_are_skipped_with_a_reason(tmp_path: Path) -> None:
    write(tmp_path, "empty.pdf", b"")
    usable, skipped = discover(tmp_path)
    assert usable == []
    assert skipped[0].reason == "empty file"
    assert skipped[0].sha256 == ""


def test_os_junk_never_appears_anywhere(tmp_path: Path) -> None:
    write(tmp_path, "real.pdf", PDF)
    write(tmp_path, ".DS_Store", b"\x00\x01")
    write(tmp_path, "Thumbs.db", b"\x00\x01")
    write(tmp_path, ".hidden.pdf", PDF + b"hidden")
    write(tmp_path, "__MACOSX/._real.pdf", PDF + b"junk")
    write(tmp_path, ".git/objects/ab/cdef", b"\x00")

    usable, skipped = discover(tmp_path)
    assert names(usable) == ["real.pdf"]
    assert skipped == []


def test_exclude_patterns_match_name_or_path(tmp_path: Path) -> None:
    write(tmp_path, "keep.pdf", PDF + b"1")
    write(tmp_path, "drafts/skip.pdf", PDF + b"2")
    write(tmp_path, "working-copy.pdf", PDF + b"3")

    usable, skipped = discover(
        tmp_path, exclude=["drafts/*", "working-*.pdf"]
    )
    assert names(usable) == ["keep.pdf"]
    assert {f.path.name for f in skipped} == {"skip.pdf", "working-copy.pdf"}
    assert all(f.reason == "excluded by pattern" for f in skipped)


def test_include_is_a_whitelist(tmp_path: Path) -> None:
    write(tmp_path, "a.pdf", PDF + b"1")
    write(tmp_path, "b.txt", b"plain\n")
    usable, skipped = discover(tmp_path, include=["*.pdf"])
    assert names(usable) == ["a.pdf"]
    assert names(skipped) == ["b.txt"]


def test_patterns_ignore_case_the_same_way_everywhere(tmp_path: Path) -> None:
    write(tmp_path, "SCAN.PDF", PDF)
    usable, _ = discover(tmp_path, include=["*.pdf"])
    assert names(usable) == ["SCAN.PDF"]


def test_excluded_files_still_report_what_they_were(tmp_path: Path) -> None:
    write(tmp_path, "drafts/skip.pdf", PDF)
    _, skipped = discover(tmp_path, exclude=["drafts/*"])
    assert skipped[0].kind == "pdf"
    assert skipped[0].reason == "excluded by pattern"
    assert skipped[0].sha256 == ""  # never hashed


def test_a_root_that_is_not_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        discover(tmp_path / "typo")
    lonely = write(tmp_path, "file.pdf", PDF)
    with pytest.raises(NotADirectoryError):
        discover(lonely)


def test_skipped_files_carry_no_slug(tmp_path: Path) -> None:
    write(tmp_path, "notes.docx", b"PK\x03\x04binary")
    _, skipped = discover(tmp_path)
    assert skipped[0].slug == ""
