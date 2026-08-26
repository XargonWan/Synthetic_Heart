# plugins/document_skill/document_skill.py

"""Generic document skill for the Agentic Runtime.

Lets Synth read the *content* of a document (PDF / DOCX) and split it into its
structural sections (chapters / headings) so it can act on each part — e.g.
produce a separate deliverable per chapter. This is a *generic* capability: the
audio-per-chapter use case is just one consumer; the same actions serve
conversion, summarisation, quoting, etc.

Design notes
------------
* **Structural, not keyword-based.** Section detection uses document *shape*
  (numbered headings, short standalone lines, page boundaries) — never a
  language-specific word list. It stays language-agnostic.
* **Sandboxed.** Every path is resolved against the same agent filesystem roots
  as the Agent plugin (``AGENT_FS_ROOTS`` / ``AGENT_FS_ROOT``), so a document
  action can never read outside the declared roots.
* **Fail-safe.** Missing files, unsupported formats, and extraction errors
  return a structured error dict — never raise into the agent loop.
* **Optional dependency.** ``python-docx`` is imported lazily; if it is missing,
  DOCX extraction degrades to a clear error while PDF (``pypdf``) keeps working.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_info


display_name = "Document Skill"

# Structural heading heuristics (language-agnostic).
# A numbered heading: leading digits, optionally dotted (1, 1.2, 1.2.3),
# followed by a separator and a short title.
_NUMBERED_HEADING_RE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3}){0,2}\s*[.)\-\s]\s*\S")
# A short standalone line that does not end with sentence punctuation.
_SHORT_LINE_MAX = 80
_SENTENCE_END = (".", "!", "?", ":", ";", ",")


def _safe_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    """Parse an int safely and clamp to bounds."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


class DocumentSkillPlugin(AIPluginBase):
    """Generic document reading + section-splitting skill for the agent."""

    display_name = display_name

    def get_metadata(self) -> dict:
        return {
            "name": "document_skill",
            "display_name": self.display_name,
            "description": (
                "Generic document skill: extract text from PDF/DOCX and split "
                "it into structural sections so Synth can act on each part."
            ),
            "category": "agent",
        }

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
            "document_extract_text": {
                "required_fields": ["path"],
                "optional_fields": ["max_chars", "page_start", "page_end"],
                "scope": "agent",
                "description": (
                    "Extract the text of a document (PDF or DOCX) inside the "
                    "allowed agent filesystem roots. Returns the text with "
                    "structural page markers. Use this to read a document's "
                    "content before acting on it."
                ),
            },
            "document_list_sections": {
                "required_fields": ["path"],
                "optional_fields": ["max_sections", "max_chars"],
                "scope": "agent",
                "description": (
                    "Split a document (PDF or DOCX) into its structural "
                    "sections (chapters/headings) and return a list of section "
                    "titles with their text. Use this to enumerate the parts of "
                    "a document so you can act on each one separately."
                ),
            },
        }

    # ------------------------------------------------------------------ #
    # Sandbox path resolution (mirrors the Agent plugin's roots).
    # ------------------------------------------------------------------ #
    def _allowed_roots(self) -> List[Path]:
        roots_raw = os.getenv("AGENT_FS_ROOTS")
        if roots_raw:
            roots = [p.strip() for p in roots_raw.split(":") if p.strip()]
        else:
            roots = [
                os.getenv("AGENT_FS_ROOT", "/app"),
                os.getenv("SYNTH_LOG_DIR", "/app/logs"),
            ]
        out: List[Path] = []
        for root in roots:
            try:
                out.append(Path(root).resolve())
            except Exception:
                continue
        return out

    def _resolve_safe_path(self, raw_path: str) -> Tuple[Optional[Path], Optional[str]]:
        if not raw_path or not str(raw_path).strip():
            return None, "Missing path"
        p = Path(str(raw_path).strip())
        if not p.is_absolute():
            roots = self._allowed_roots()
            if not roots:
                return None, "No allowed roots configured"
            p = roots[0] / p
        try:
            resolved = p.resolve()
        except Exception as exc:
            return None, f"Invalid path: {exc}"
        for root in self._allowed_roots():
            try:
                resolved.relative_to(root)
                return resolved, None
            except ValueError:
                continue
        return None, "Path is outside allowed roots"

    # ------------------------------------------------------------------ #
    # Extraction.
    # ------------------------------------------------------------------ #
    def _extract_pdf(
        self, path: Path, *, max_chars: int, page_start: int, page_end: int
    ) -> Tuple[str, str]:
        """Extract text from a PDF via pypdf, with structural page markers."""
        try:
            from pypdf import PdfReader
        except Exception as exc:  # pragma: no cover - dependency present in repo
            return "", f"pypdf unavailable: {exc}"

        chunks: List[str] = []
        try:
            with path.open("rb") as fh:
                reader = PdfReader(fh)
                total = len(reader.pages)
                start = max(1, page_start)
                end = min(total, page_end) if page_end else total
                for page_num in range(start, end + 1):
                    page = reader.pages[page_num - 1]
                    page_text = str(page.extract_text() or "").strip()
                    if page_text:
                        chunks.append(f"[Page {page_num}]\n{page_text}")
        except Exception as exc:
            return "", f"pdf text extraction failed: {exc}"

        content = "\n\n".join(chunks)
        if not content.strip():
            return "", "pdf contains no extractable text"
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content, ""

    def _extract_docx(self, path: Path, *, max_chars: int) -> Tuple[str, str]:
        """Extract text from a DOCX via python-docx (lazy import)."""
        try:
            import docx
        except Exception as exc:
            return "", f"python-docx unavailable (install 'python-docx'): {exc}"

        try:
            document = docx.Document(str(path))
            paragraphs = [
                p.text for p in document.paragraphs if p.text and p.text.strip()
            ]
        except Exception as exc:
            return "", f"docx text extraction failed: {exc}"

        content = "\n".join(paragraphs)
        if not content.strip():
            return "", "docx contains no extractable text"
        if len(content) > max_chars:
            content = content[:max_chars] + "\n... (truncated)"
        return content, ""

    def _extract(self, path: Path, *, max_chars: int, page_start: int, page_end: int):
        """Dispatch to the right extractor by file signature."""
        try:
            with path.open("rb") as fh:
                head = fh.read(5)
        except Exception as exc:
            return "", f"cannot read file: {exc}"
        if head.startswith(b"%PDF-"):
            return self._extract_pdf(
                path, max_chars=max_chars, page_start=page_start, page_end=page_end
            )
        if path.suffix.lower() in (".docx", ".docm"):
            return self._extract_docx(path, max_chars=max_chars)
        return "", "unsupported document format (expected .pdf or .docx)"

    # ------------------------------------------------------------------ #
    # Structural section detection.
    # ------------------------------------------------------------------ #
    def _detect_sections(self, text: str, *, max_sections: int) -> List[Dict[str, Any]]:
        """Split extracted text into structural sections.

        Heuristics (all structural, language-agnostic):
        * A **numbered heading** line (``1``, ``1.2``, ``1.2.3``) starts a new
          section.
        * A **short standalone line** (≤ 80 chars) that does not end in sentence
          punctuation and is followed by a blank line or more content also
          starts a new section (a generic heading shape).
        * Page markers (``[Page N]``) are kept as anchors but never treated as
          headings.
        """
        lines = text.splitlines()
        sections: List[Dict[str, Any]] = []
        current_title = ""
        current_body: List[str] = []

        def _flush() -> None:
            nonlocal current_title, current_body
            if current_title or current_body:
                sections.append(
                    {
                        "title": current_title or "(untitled)",
                        "text": "\n".join(current_body).strip(),
                    }
                )
            current_title = ""
            current_body = []

        for i, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("[Page "):
                # Page anchor — keep as body context, not a heading.
                current_body.append(raw)
                continue

            is_numbered = bool(_NUMBERED_HEADING_RE.match(line))
            is_short = (
                len(line) <= _SHORT_LINE_MAX
                and not line.endswith(_SENTENCE_END)
                and len(line.split()) <= 12
            )
            # A short line followed by a blank line (or end) reads as a heading.
            next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()

            if (is_numbered or (is_short and next_blank)) and len(
                sections
            ) < max_sections:
                _flush()
                current_title = line
            else:
                current_body.append(raw)

        _flush()

        # Hard cap: never return more than ``max_sections``. Any overflow is
        # folded into the last kept section so no content is lost.
        if len(sections) > max_sections:
            overflow = sections[max_sections:]
            last = sections[max_sections - 1]
            merged = "\n\n".join([last["text"]] + [s["text"] for s in overflow]).strip()
            last["text"] = merged
            sections = sections[:max_sections]

        return sections

    # ------------------------------------------------------------------ #
    # Action dispatch.
    # ------------------------------------------------------------------ #
    async def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "document_extract_text":
            raw_path = str(payload.get("path") or "").strip()
            safe_path, err = self._resolve_safe_path(raw_path)
            if err or safe_path is None:
                return {"status": "error", "reason": err or "invalid path"}
            if not safe_path.exists():
                return {"status": "error", "reason": "file not found"}
            if safe_path.is_dir():
                return {"status": "error", "reason": "path is a directory"}

            max_chars = _safe_int(
                payload.get("max_chars"), 60_000, min_value=500, max_value=400_000
            )
            page_start = _safe_int(
                payload.get("page_start"), 1, min_value=1, max_value=1_000_000
            )
            page_end = _safe_int(
                payload.get("page_end"), 0, min_value=0, max_value=1_000_000
            )

            content, extract_err = self._extract(
                safe_path,
                max_chars=max_chars,
                page_start=page_start,
                page_end=page_end,
            )
            if extract_err:
                return {"status": "error", "reason": extract_err}
            log_info(
                f"[document_skill] Extracted text from {safe_path} "
                f"({len(content)} chars)"
            )
            return {
                "status": "ok",
                "path": str(safe_path),
                "chars": len(content),
                "content": content,
            }

        if action_type == "document_list_sections":
            raw_path = str(payload.get("path") or "").strip()
            safe_path, err = self._resolve_safe_path(raw_path)
            if err or safe_path is None:
                return {"status": "error", "reason": err or "invalid path"}
            if not safe_path.exists():
                return {"status": "error", "reason": "file not found"}
            if safe_path.is_dir():
                return {"status": "error", "reason": "path is a directory"}

            max_sections = _safe_int(
                payload.get("max_sections"), 50, min_value=1, max_value=500
            )
            max_chars = _safe_int(
                payload.get("max_chars"), 200_000, min_value=500, max_value=1_000_000
            )

            content, extract_err = self._extract(
                safe_path,
                max_chars=max_chars,
                page_start=1,
                page_end=0,
            )
            if extract_err:
                return {"status": "error", "reason": extract_err}

            sections = self._detect_sections(content, max_sections=max_sections)
            log_info(
                f"[document_skill] Detected {len(sections)} sections in {safe_path}"
            )
            return {
                "status": "ok",
                "path": str(safe_path),
                "count": len(sections),
                "sections": sections,
            }

        return {"status": "skipped", "reason": f"unhandled action {action_type}"}


# Auto-register this plugin
PLUGIN_CLASS = DocumentSkillPlugin
