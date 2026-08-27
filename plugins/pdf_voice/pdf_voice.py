# plugins/pdf_voice/pdf_voice.py
"""PDF Voice — turn a PDF document into per-chapter spoken audio.

Synth reads a PDF file (from the shared agent filesystem sandbox), splits it
into chapters/sections, and synthesises one TTS clip per chapter through the
public Vox API (``VoxPlugin.speak(generate_only=True)``). Each clip is then
delivered to a chat interface (as playable audio) or broadcast to the shared
avatar on the WebUI.

This plugin has **no LLM of its own** — it drives Vox and therefore subclasses
``PluginBase``. Chapter detection is purely **structural** (document parsing,
which the project rules explicitly allow):

* **outline mode** — boundaries come from the PDF's outline / bookmark tree
  (``PdfReader.outline``). Pages before the first entry form an "Introduction"
  chapter; each entry owns ``[page, next_entry_page)``.
* **size mode** — pages are accumulated so every chunk stays within
  ``PDFVOICE_MAX_CHUNK_CHARS`` (an oversized single page is hard-split), and
  the result is capped at ``PDFVOICE_MAX_CHAPTERS``.

Everything is fail-safe: a missing/invalid file, a broken PDF, a disabled Vox
engine, or a per-chapter synthesis error all degrade to a structured status
dict instead of raising into the message chain.
"""

from __future__ import annotations

import inspect
from typing import Any

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.outbound_file_utils import resolve_safe_outbound_path
from core.plugin_base import PluginBase

LOG_PREFIX = "[pdf_voice]"

# ---------------------------------------------------------------------------
# Exposed config (WebUI) — registration is best-effort so import never breaks.
# ---------------------------------------------------------------------------

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "PDFVOICE_MAX_CHUNK_CHARS",
        label="Max chunk characters",
        default=8000,
        value_type=int,
        ui_type="number",
        description=(
            "Maximum characters per TTS chunk in size-split mode. A single "
            "oversized page is hard-split so no chapter ever exceeds this."
        ),
        scope="plugins",
        component="pdf_voice",
        tags=["plugin"],
    )
    register_exposed_var(
        "PDFVOICE_MAX_CHAPTERS",
        label="Max chapters",
        default=30,
        value_type=int,
        ui_type="number",
        description="Hard cap on the number of chapters produced per PDF.",
        scope="plugins",
        component="pdf_voice",
        tags=["plugin"],
    )
    register_exposed_var(
        "PDFVOICE_SPLIT_MODE",
        label="Split mode",
        default="outline",
        value_type=str,
        ui_type="select",
        options=["outline", "size"],
        description=(
            "'outline' splits on the PDF's table-of-contents/bookmark tree when "
            "present (falling back to size); 'size' always chunks by character "
            "count."
        ),
        scope="plugins",
        component="pdf_voice",
        tags=["plugin"],
    )
except Exception:  # pragma: no cover - import-time safety
    pass


# ---------------------------------------------------------------------------
# Pure chapter-splitting helpers (unit-testable without TTS / DB / FS)
# ---------------------------------------------------------------------------


def _join_pages(pages: list[str], start: int, end: int) -> str:
    """Join non-empty page texts in the inclusive ``[start, end)`` range."""
    parts: list[str] = []
    for i in range(start, min(end, len(pages))):
        text = str(pages[i] or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _make_chapter(title: str, text: str, start_page: int, end_page: int) -> dict:
    """Build a chapter dict with 0-based inclusive page bounds."""
    return {
        "title": str(title or ""),
        "text": text,
        "start_page": int(start_page),
        "end_page": int(end_page),
        "chars": len(text),
    }


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit``.

    Prefers a paragraph break, then a sentence break, then whitespace, then a
    hard character cut. Never returns empty strings.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", ". ", "! ", "? ", " "):
            idx = window.rfind(sep)
            if idx > 0:
                cut = idx + len(sep)
                break
        if cut <= 0:
            cut = limit
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _outline_chapters(pages: list[str], outline: list[dict], cap: int) -> list[dict]:
    """Split pages on structural outline/bookmark boundaries.

    ``outline`` is a list of ``{"title", "page"}`` dicts with 0-based ``page``.
    Entries are sorted by page (stable), deduplicated per page, and clamped to
    the document length. Pages before the first entry become an "Introduction"
    chapter; each entry owns ``[page, next_entry_page)``.
    """
    entries: list[tuple[int, str, int]] = []
    for raw_i, entry in enumerate(outline or []):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        try:
            page = int(entry.get("page"))
        except (TypeError, ValueError):
            continue
        if page < 0 or page >= len(pages):
            continue
        entries.append((page, title or f"Chapter {len(entries) + 1}", raw_i))
    entries.sort(key=lambda t: (t[0], t[2]))

    deduped: list[tuple[int, str]] = []
    for page, title, _ in entries:
        if deduped and deduped[-1][0] == page:
            continue
        deduped.append((page, title))

    # No usable outline boundary → signal the caller to fall back to size mode.
    if not deduped:
        return []

    total = len(pages)
    chapters: list[dict] = []
    first_page = deduped[0][0]
    if first_page > 0:
        text = _join_pages(pages, 0, first_page)
        if text:
            chapters.append(_make_chapter("Introduction", text, 0, first_page - 1))

    for k, (page, title) in enumerate(deduped):
        end_page = deduped[k + 1][0] if k + 1 < len(deduped) else total
        text = _join_pages(pages, page, end_page)
        if not text:
            continue
        chapters.append(_make_chapter(title, text, page, end_page - 1))
    return chapters[:cap]


def _size_chapters(pages: list[str], limit: int, cap: int) -> list[dict]:
    """Accumulate pages into chunks bounded by ``limit`` (hard-split oversized)."""
    raw: list[dict] = []
    buf: list[str] = []
    start: int | None = None
    end: int | None = None

    def flush() -> None:
        nonlocal buf, start, end
        if buf:
            text = "\n\n".join(buf).strip()
            if text:
                raw.append(
                    {
                        "title": "",
                        "text": text,
                        "start_page": start,
                        "end_page": end,
                    }
                )
            buf = []
            start = None
            end = None

    for i, raw_page in enumerate(pages):
        page = str(raw_page or "").strip()
        if not page:
            continue
        if start is None:
            start = i
        if buf and (len("\n\n".join(buf)) + 2 + len(page)) > limit:
            flush()
            start = i
        if len(page) > limit:
            flush()
            for piece in _chunk_text(page, limit):
                raw.append(
                    {
                        "title": "",
                        "text": piece,
                        "start_page": i,
                        "end_page": i,
                    }
                )
            start = None
            end = None
            continue
        buf.append(page)
        end = i
    flush()

    chapters: list[dict] = []
    for n, chunk in enumerate(raw, start=1):
        chapters.append(
            _make_chapter(
                f"Chapter {n}",
                chunk["text"],
                chunk["start_page"],
                chunk["end_page"],
            )
        )
    return chapters[:cap]


def split_into_chapters(
    pages: list[str],
    mode: str = "outline",
    max_chunk_chars: int = 8000,
    max_chapters: int = 30,
    outline: list[dict] | None = None,
) -> list[dict]:
    """Split extracted PDF pages into chapter/section dicts.

    Args:
        pages: Per-page extracted text, in order.
        mode: ``"outline"`` (use the outline/bookmark tree, fall back to size
            when the outline is absent or yields no boundary) or ``"size"``.
        max_chunk_chars: Size-mode chunk budget (clamped to ``>= 1``).
        max_chapters: Hard cap on the number of chapters (clamped to ``>= 1``).
        outline: Optional ``[{"title": str, "page": int}]`` entries with 0-based
            page numbers (already normalised from the PDF reader).

    Returns:
        A list of ``{"title", "text", "start_page", "end_page", "chars"}``
        dicts (``start_page``/``end_page`` are 0-based and inclusive).
    """
    if not pages:
        return []

    try:
        limit = int(max_chunk_chars)
    except (TypeError, ValueError):
        limit = 8000
    limit = max(1, limit)

    try:
        cap = int(max_chapters)
    except (TypeError, ValueError):
        cap = 30
    cap = max(1, cap)

    mode_norm = str(mode or "").strip().lower()
    if mode_norm == "outline" and outline:
        chapters = _outline_chapters(pages, outline, cap)
        if chapters:
            return chapters
        # The outline existed but produced no usable boundary — fall back.
    return _size_chapters(pages, limit, cap)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class PDFVoicePlugin(PluginBase):
    """Drive Vox to read a PDF aloud, one chapter per TTS clip."""

    display_name = "PDF Voice"

    def __init__(self) -> None:
        super().__init__()
        try:
            from core.core_initializer import register_plugin

            register_plugin("pdf_voice", self)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} register_plugin failed: {exc}")
        log_info(f"{LOG_PREFIX} PDFVoicePlugin registered")

    def get_metadata(self) -> dict:
        return {
            "name": "pdf_voice",
            "display_name": "PDF Voice",
            "description": (
                "Reads a PDF file aloud: splits it into chapters/sections and "
                "synthesises one spoken audio clip per chapter through Vox, "
                "delivering each to a chat interface or the shared avatar."
            ),
            "category": "Various",
            "icon": "icon.svg",
            "guide": "guide.md",
            "disable_allowed": True,
        }

    def get_supported_actions(self) -> dict:
        return {
            "pdf_to_voice": {
                "description": (
                    "Convert a PDF document into spoken audio, one chapter at a "
                    "time. Point 'path' at a PDF inside the agent filesystem "
                    "sandbox; the document is split into chapters (by its "
                    "table-of-contents when available, otherwise by size) and "
                    "each chapter is synthesised as a separate voice message. "
                    "Set 'interface_path' to deliver the audio files to a "
                    "specific chat; otherwise they are played on the shared "
                    "avatar. Use 'voice' to pick a specific voice, 'language' "
                    "to route to that language's configured engine/voice, and "
                    "'max_chapters' to cap how many chapters are spoken."
                ),
                "required_fields": ["path"],
                "optional_fields": [
                    "interface_path",
                    "max_chapters",
                    "language",
                    "voice",
                ],
                "security_level": "medium",
                "scope": "core",
                "external_effects": ["filesystem"],
            },
        }

    # ------------------------------------------------------------------
    # Config helpers (always-fresh reads, fail-safe)
    # ------------------------------------------------------------------

    @staticmethod
    def _int_config(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = config_registry.get_value(
                key,
                default,
                value_type=int,
                group="plugins",
                component="pdf_voice",
            )
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _split_mode() -> str:
        try:
            raw = config_registry.get_value(
                "PDFVOICE_SPLIT_MODE",
                "outline",
                value_type=str,
                group="plugins",
                component="pdf_voice",
            )
            mode = str(raw).strip().lower()
        except Exception:
            mode = "outline"
        return mode if mode in ("outline", "size") else "outline"

    @staticmethod
    def _read_outline(reader: Any) -> list[dict]:
        """Normalise a ``PdfReader`` outline into ``[{"title", "page"}]`` (0-based).

        pypdf exposes the bookmark tree as a flat list of ``Destination``
        objects. The page number is resolved structurally via
        ``get_destination_page_number`` (never by parsing title text). Returns
        ``[]`` on any failure.
        """
        outline: list[dict] = []
        try:
            raw = list(reader.outline or [])
        except Exception:
            return []

        for idx, entry in enumerate(raw):
            title = str(getattr(entry, "title", "") or "").strip()
            try:
                page_num = reader.get_destination_page_number(entry)
            except Exception:
                try:
                    dest = getattr(entry, "page", None)
                    page_num = getattr(dest, "page_number", None)
                    if page_num is None and isinstance(dest, int):
                        page_num = dest
                except Exception:
                    continue
            if page_num is None:
                continue
            try:
                page_num = int(page_num)
            except (TypeError, ValueError):
                continue
            if page_num < 0:
                continue
            outline.append(
                {
                    "title": title or f"Chapter {idx + 1}",
                    "page": page_num,
                }
            )
        return outline

    @staticmethod
    def _find_vox_plugin() -> Any | None:
        """Return the live Vox plugin instance from the registry, or ``None``."""
        try:
            from core.core_initializer import PLUGIN_REGISTRY
            from plugins.vox_plugin import VoxPlugin

            if isinstance(PLUGIN_REGISTRY, dict):
                for plugin in PLUGIN_REGISTRY.values():
                    if isinstance(plugin, VoxPlugin):
                        return plugin
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} could not resolve Vox plugin: {exc}")
        return None

    async def _deliver(
        self,
        vox: Any,
        audio_path: str,
        text: str,
        interface_path: str | None,
    ) -> bool:
        """Deliver one chapter clip to a chat interface or the shared avatar."""
        iface_name = ""
        if interface_path:
            try:
                from core.core_initializer import INTERFACE_REGISTRY
                from core.interface_path_utils import parse_interface_path

                iface_name, _ = parse_interface_path(interface_path)
                target_iface = INTERFACE_REGISTRY.get(iface_name)
                if (
                    target_iface is not None
                    and iface_name != "synth_webui"
                    and hasattr(target_iface, "send_message")
                ):
                    result = target_iface.send_message(
                        {
                            "interface_path": interface_path,
                            "audio": audio_path,
                            "text": text,
                        }
                    )
                    if inspect.iscoroutine(result):
                        result = await result
                    return bool(result)
            except Exception as exc:
                log_warning(
                    f"{LOG_PREFIX} interface '{iface_name}' delivery failed: {exc}"
                )

        # No usable chat interface (or it failed): broadcast to the shared
        # avatar / WebUI so the chapter is still heard.
        try:
            return bool(await vox.broadcast_audio_to_webui(audio_path, text=text))
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} webui broadcast failed: {exc}")
            return False

    async def execute_action(
        self,
        action: dict,
        context: dict | None = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> dict:
        action = action or {}
        payload = action.get("payload") or {}
        ctx = context if isinstance(context, dict) else {}

        raw_path = str(payload.get("path") or "").strip()
        resolved, err = resolve_safe_outbound_path(raw_path)
        if err or resolved is None:
            return {"status": "error", "reason": err or "invalid path"}

        # Structural PDF magic check (mirrors agent_read_file).
        try:
            with resolved.open("rb") as fh:
                head = fh.read(5)
        except Exception as exc:
            return {"status": "error", "reason": f"cannot read file: {exc}"}
        if not head.startswith(b"%PDF-"):
            return {"status": "error", "reason": "not a PDF file"}

        # Extract per-page text (fail-safe per page).
        try:
            from pypdf import PdfReader
        except Exception as exc:
            return {"status": "error", "reason": f"pypdf unavailable: {exc}"}

        reader: Any = None
        pages: list[str] = []
        try:
            with resolved.open("rb") as fh:
                reader = PdfReader(fh)
                for page in reader.pages:
                    try:
                        pages.append(str(page.extract_text() or ""))
                    except Exception:
                        pages.append("")
        except Exception as exc:
            return {"status": "error", "reason": f"pdf text extraction failed: {exc}"}

        if not any(p.strip() for p in pages):
            return {"status": "error", "reason": "pdf contains no extractable text"}

        # Config + optional per-call overrides.
        split_mode = self._split_mode()
        max_chunk_chars = self._int_config(
            "PDFVOICE_MAX_CHUNK_CHARS", 8000, 1, 1_000_000
        )
        max_chapters = self._int_config("PDFVOICE_MAX_CHAPTERS", 30, 1, 500)
        max_chapters_override = payload.get("max_chapters")
        if max_chapters_override is not None:
            try:
                max_chapters = max(1, min(int(max_chapters_override), 500))
            except (TypeError, ValueError):
                pass

        outline: list[dict] | None = None
        if split_mode == "outline" and reader is not None:
            outline = self._read_outline(reader)

        chapters = split_into_chapters(
            pages,
            mode=split_mode,
            max_chunk_chars=max_chunk_chars,
            max_chapters=max_chapters,
            outline=outline,
        )
        if not chapters:
            return {"status": "error", "reason": "no chapters produced"}

        # Vox gating — mirror Vox's own "skipped" contract when disabled.
        vox_enabled = False
        try:
            from plugins.vox_plugin import is_vox_enabled as _is_vox_enabled

            vox_enabled = bool(_is_vox_enabled())
        except Exception:
            vox_enabled = False
        if not vox_enabled:
            return {"status": "skipped", "reason": "vox_disabled"}

        vox = self._find_vox_plugin()
        if vox is None:
            return {"status": "error", "reason": "vox plugin not loaded"}

        # Per-call voice / language hints.
        voice = payload.get("voice") or None
        language = payload.get("language") or None
        engine_name: str | None = None
        resolved_voice = voice
        if language:
            try:
                from core.config import get_vox_language_override_async

                override = await get_vox_language_override_async(language)
                if isinstance(override, dict):
                    ov_engine = override.get("engine")
                    if ov_engine and str(ov_engine) != "disabled":
                        engine_name = str(ov_engine)
                    if not resolved_voice and override.get("voice"):
                        resolved_voice = override.get("voice")
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} language override lookup failed: {exc}")

        interface_path = payload.get("interface_path") or ctx.get("interface_path")

        results: list[dict] = []
        for chapter in chapters:
            title = str(chapter.get("title") or "")
            try:
                result = await vox.speak(
                    text=str(chapter.get("text") or ""),
                    generate_only=True,
                    voice=resolved_voice,
                    engine_name=engine_name,
                )
                audio_path = (
                    str(result.get("audio_path"))
                    if isinstance(result, dict) and result.get("audio_path")
                    else ""
                )
                if result.get("status") == "success" and audio_path:
                    await self._deliver(vox, audio_path, title, interface_path)
                    results.append(
                        {
                            "title": title,
                            "audio_path": audio_path,
                            "chars": int(chapter.get("chars") or 0),
                        }
                    )
                else:
                    log_warning(
                        f"{LOG_PREFIX} chapter '{title}' synthesis skipped: "
                        f"{result.get('status')}"
                    )
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} chapter '{title}' synthesis failed: {exc}")

        if not results:
            return {
                "status": "error",
                "reason": "all chapters failed to synthesize",
                "chapters": [],
            }

        log_info(
            f"{LOG_PREFIX} produced {len(results)} chapter clip(s) from {raw_path!r}"
        )
        return {"status": "ok", "chapters": results}


PLUGIN_CLASS = PDFVoicePlugin
