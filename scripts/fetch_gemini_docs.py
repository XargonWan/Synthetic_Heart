#!/usr/bin/env python3
"""Fetch updated Gemini API docs and write them to docs/gemini/."""
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "html2text", "beautifulsoup4"]
# ///

import html2text
import requests
from bs4 import BeautifulSoup
from pathlib import Path

DOCS_DIR = Path(__file__).parent.parent / "docs" / "gemini"

PAGES: dict[str, list[str]] = {
    "caching.md": ["https://ai.google.dev/gemini-api/docs/caching"],
    "document-processing.md": [
        "https://ai.google.dev/gemini-api/docs/document-processing"
    ],
    "function-calling.md": ["https://ai.google.dev/gemini-api/docs/function-calling"],
    "gemini-3.md": [
        "https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview",
        "https://ai.google.dev/gemini-api/docs/models/gemini-3-pro-preview",
    ],
    "gemini-3-1.md": [
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview",
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview",
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-preview",
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image-preview",
    ],
    "image-understanding.md": [
        "https://ai.google.dev/gemini-api/docs/image-understanding"
    ],
    "live.md": [
        "https://ai.google.dev/gemini-api/docs/live-guide",
        "https://ai.google.dev/gemini-api/docs/live-api/best-practices",
    ],
    "liveapi.md": ["https://ai.google.dev/gemini-api/docs/live-api/capabilities"],
    "live-session-management.md": [
        "https://ai.google.dev/gemini-api/docs/live-api/session-management"
    ],
    "live-tools.md": ["https://ai.google.dev/gemini-api/docs/live-api/tools"],
    "media-resolution.md": ["https://ai.google.dev/gemini-api/docs/media-resolution"],
    "rate-limits.md": ["https://ai.google.dev/gemini-api/docs/rate-limits"],
    "structured-output.md": ["https://ai.google.dev/gemini-api/docs/structured-output"],
    "thought-signatures.md": [
        "https://ai.google.dev/gemini-api/docs/thought-signatures",
        "https://ai.google.dev/gemini-api/docs/thinking",
    ],
    "video-understanding.md": [
        "https://ai.google.dev/gemini-api/docs/video-understanding"
    ],
    "gemini-3-guide.md": ["https://ai.google.dev/gemini-api/docs/gemini-3"],
    "deprecations.md": ["https://ai.google.dev/gemini-api/docs/deprecations"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SyntH-docs-fetcher/1.0)",
    "Accept": "text/html,application/xhtml+xml",
}

h2t = html2text.HTML2Text()
h2t.ignore_links = False
h2t.ignore_images = True
h2t.body_width = 0


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Try to get just the main article content
    main = soup.find("article") or soup.find("main") or soup.find("body")
    # Remove nav/footer/aside noise
    for tag in main.find_all(["nav", "footer", "aside", "script", "style"]):
        tag.decompose()
    return h2t.handle(str(main))


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # Files to preserve (SyntH-specific)
    preserve = {
        "synth-live-voice-integration.md",
        "synth-live-voice-integration.rst",
        "gemini_api_engine.rst",
    }

    # Delete old non-preserved files
    for f in DOCS_DIR.iterdir():
        if f.name not in preserve:
            f.unlink()
            print(f"  deleted: {f.name}")

    for filename, urls in PAGES.items():
        parts: list[str] = []
        for url in urls:
            print(f"  fetching {url} ...")
            try:
                parts.append(f"<!-- source: {url} -->\n\n" + fetch_page(url))
            except Exception as e:
                print(f"    WARN: {e}")

        if parts:
            out = DOCS_DIR / filename
            out.write_text("\n\n---\n\n".join(parts), encoding="utf-8")
            print(f"  wrote: {filename}")
        else:
            print(f"  SKIP (no content): {filename}")

    print("\nDone.")


if __name__ == "__main__":
    main()
