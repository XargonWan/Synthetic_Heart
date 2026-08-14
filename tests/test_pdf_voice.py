"""Tests for the PDF → per-chapter voice plugin (``plugins/pdf_voice``).

These cover only the **pure** chapter-splitting helper ``split_into_chapters``
— no TTS, no DB, no filesystem — verifying that:

* outline mode honours outline/bookmark boundaries (front-matter
  "Introduction" + each entry owning ``[page, next_page)``);
* size mode keeps every chapter within ``max_chunk_chars`` (hard-splitting an
  oversized page) and caps at ``max_chapters``;
* invalid input degrades to an empty list and an unusable outline falls back
  to size-based chunking.
"""

from __future__ import annotations

from plugins.pdf_voice.pdf_voice import split_into_chapters


def test_outline_mode_uses_outline_boundaries() -> None:
    pages = [
        "Front matter A",
        "Front matter B",
        "Chapter 1 text",
        "Chapter 1 more",
        "Chapter 2 text",
    ]
    outline = [
        {"title": "One", "page": 2},
        {"title": "Two", "page": 4},
    ]

    chapters = split_into_chapters(
        pages, mode="outline", max_chunk_chars=100, max_chapters=10, outline=outline
    )

    assert [c["title"] for c in chapters] == ["Introduction", "One", "Two"]

    assert chapters[0]["start_page"] == 0
    assert chapters[0]["end_page"] == 1
    assert chapters[0]["text"] == "Front matter A\n\nFront matter B"

    assert chapters[1]["start_page"] == 2
    assert chapters[1]["end_page"] == 3
    assert chapters[1]["text"] == "Chapter 1 text\n\nChapter 1 more"
    assert chapters[1]["chars"] == len(chapters[1]["text"])

    assert chapters[2]["start_page"] == 4
    assert chapters[2]["end_page"] == 4


def test_outline_mode_falls_back_to_size_when_outline_missing() -> None:
    pages = ["aaa", "bbb"]

    chapters = split_into_chapters(
        pages, mode="outline", max_chunk_chars=3, max_chapters=10, outline=None
    )

    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2"]
    assert all(c["chars"] <= 3 for c in chapters)


def test_outline_mode_falls_back_to_size_when_outline_yields_nothing() -> None:
    pages = ["aaa", "bbb"]
    # Entries are all out of range → no usable boundary → size mode.
    outline = [{"title": "Ghost", "page": 99}]

    chapters = split_into_chapters(
        pages, mode="outline", max_chunk_chars=3, max_chapters=10, outline=outline
    )

    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2"]


def test_size_mode_respects_max_chunk_chars() -> None:
    pages = ["aa", "bb", "cc", "dd"]  # 2 chars each

    chapters = split_into_chapters(
        pages, mode="size", max_chunk_chars=6, max_chapters=10
    )

    # "aa"+"bb" = "aa\n\nbb" (6) fits; "cc"+"dd" = "cc\n\ndd" (6) fits.
    assert len(chapters) == 2
    assert chapters[0]["text"] == "aa\n\nbb"
    assert chapters[0]["chars"] == 6
    assert chapters[1]["text"] == "cc\n\ndd"
    assert chapters[1]["chars"] == 6


def test_size_mode_hard_splits_oversized_page() -> None:
    pages = ["x" * 25]

    chapters = split_into_chapters(
        pages, mode="size", max_chunk_chars=10, max_chapters=10
    )

    assert len(chapters) == 3
    assert all(c["chars"] <= 10 for c in chapters)
    assert all(c["start_page"] == 0 and c["end_page"] == 0 for c in chapters)


def test_size_mode_caps_at_max_chapters() -> None:
    pages = ["aaaa", "bbbb", "cccc", "dddd"]

    chapters = split_into_chapters(
        pages, mode="size", max_chunk_chars=4, max_chapters=2
    )

    assert len(chapters) == 2


def test_outline_mode_caps_at_max_chapters() -> None:
    pages = ["p0", "p1", "p2", "p3", "p4", "p5"]
    outline = [{"title": f"C{i}", "page": i} for i in range(6)]

    chapters = split_into_chapters(
        pages, mode="outline", max_chunk_chars=100, max_chapters=3, outline=outline
    )

    assert len(chapters) == 3
    assert [c["title"] for c in chapters] == ["C0", "C1", "C2"]


def test_empty_pages_return_empty_list() -> None:
    assert split_into_chapters([], "outline", 100, 10, []) == []
    assert split_into_chapters(["   ", ""], "size", 100, 10) == []


def test_outline_empty_title_gets_fallback() -> None:
    pages = ["front", "body", "tail"]
    outline = [{"title": "", "page": 1}]

    chapters = split_into_chapters(
        pages, mode="outline", max_chunk_chars=100, max_chapters=10, outline=outline
    )

    assert chapters[0]["title"] == "Introduction"
    assert chapters[1]["title"] == "Chapter 1"


def test_invalid_mode_falls_back_to_size() -> None:
    pages = ["aa", "bb"]

    chapters = split_into_chapters(
        pages, mode="bogus", max_chunk_chars=2, max_chapters=10
    )

    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2"]
    assert all(c["chars"] <= 2 for c in chapters)
