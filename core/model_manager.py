# core/model_manager.py
"""Model Manager — single source of truth for downloadable AI model management.

Plugins register their available models at import time using
``MODEL_MANAGER.register()``.  The WebUI reads ``MODEL_MANAGER.catalog()`` to
build a list of all known models together with their current download state
(downloaded or not).  Download, deletion and update operations are all routed
through this module so no plugin needs to implement its own storage logic.

Storage layout
--------------
All models are kept under *SYNTH_MODELS_DIR* (env var; defaults to
``~/.cache/synth/models``).  Each model gets its own sub-directory named after
the ``model_id``::

    $SYNTH_MODELS_DIR/
        kitten-tts-nano-0.8/
            .manifest.json          ← written after a successful download
            <model weights …>
        vosk-en-us/
            .manifest.json
            <model weights …>

Sample MP3 files
----------------
On first request a tiny MP3 sample for each model+voice is generated and
cached under ``res/synth_webui/static/audio/model_samples/<model_id>/``.
The sample text is always: *"Hello, I am a synth, and this is synthetic heart"*.

Plugin integration
------------------
.. code-block:: python

    from core.model_manager import MODEL_MANAGER, ModelSpec

    MODEL_MANAGER.register(ModelSpec(
        model_id="kitten-tts-nano-0.8",
        plugin_id="vox_kitten",
        display_name="KittenTTS Nano 0.8",
        description="Compact TTS model (~150 MB), 8 voices, CPU-ready.",
        tags=["tts", "local", "cpu"],
        size_mb=150,
        voices=["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"],
        language="en",
        hf_repo_id="KittenML/kitten-tts-nano-0.8",
    ))
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from core.logging_utils import log_error, log_info, log_warning

# ---------------------------------------------------------------------------
# Storage root
# ---------------------------------------------------------------------------
_ENV_MODELS_DIR = "SYNTH_MODELS_DIR"
_DEFAULT_MODELS_DIR = Path.home() / ".cache" / "synth" / "models"

# Static sample directory (relative to repo root).
# Must be reachable under /static/audio/model_samples/ in the WebUI.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SAMPLES_STATIC_DIR = (
    _REPO_ROOT / "res" / "synth_webui" / "static" / "audio" / "model_samples"
)

_SAMPLE_TEXT = "Hello, I am a synth, and this is synthetic heart"
_MANIFEST_FILENAME = ".manifest.json"

# ---------------------------------------------------------------------------
# Per-language sample texts
# ---------------------------------------------------------------------------
#: Texts used to generate sample audio files for each language.
#: Plugins should prefer ``SAMPLE_TEXT_BY_LANG[lang]`` over hard-coded strings.
SAMPLE_TEXT_BY_LANG: dict[str, str] = {
    "en": "Hello, I am a synth, and this is synthetic heart",
    "it": "Ciao, sono un synth, e questo è un cuore sintetico",
    "de": "Hallo, ich bin ein Synth, und das ist ein synthetisches Herz",
    "fr": "Bonjour, je suis un synth, et ceci est un cœur synthétique",
    "es": "Hola, soy un synth, y este es un corazón sintético",
    "ja": "こんにちは、私はシンスです。これはシンセティックハートです",
    "zh": "你好，我是一个合成人，这是合成心脏",
    "ko": "안녕하세요, 저는 신스입니다. 이것은 합성 심장입니다",
    "pt": "Olá, sou um synth, e este é um coração sintético",
    "ru": "Привет, я синт, и это синтетическое сердце",
}

# ---------------------------------------------------------------------------
# VoiceSpec dataclass
# ---------------------------------------------------------------------------


@dataclass
class VoiceSpec:
    """Metadata for a single voice offered by a TTS model."""

    name: str
    """Voice identifier, e.g. ``Bella`` or ``Bruno``."""

    gender: Literal["M", "F", "N"] = "N"
    """Gender hint for TTS engine selection.

    ``M`` — male, ``F`` — female, ``N`` — neutral / unspecified.
    """

    languages: list[str] = field(default_factory=lambda: ["*"])
    """Language codes supported by this voice.

    ``["*"]`` means the voice inherits the owner model's
    ``supported_languages`` list.  Explicit codes (e.g. ``["en", "it"]``)
    override the model-level list for this specific voice.
    """


# ---------------------------------------------------------------------------
# ModelSpec dataclass
# ---------------------------------------------------------------------------
@dataclass
class ModelSpec:
    """Describes a single downloadable model that a plugin can use."""

    model_id: str
    """Unique slug, e.g. ``kitten-tts-nano-0.8`` or ``vosk-en-us``."""

    plugin_id: str
    """The plugin that owns this model, e.g. ``vox_kitten`` or ``auris_vosk``."""

    display_name: str
    """Human-readable name shown in the UI."""

    description: str
    """Short description of the model."""

    tags: list[str] = field(default_factory=list)
    """Free-form tags: ``tts``, ``stt``, ``local``, ``cpu``, ``gpu``, …"""

    size_mb: int = 0
    """Approximate download size in megabytes (informational only)."""

    voices: list[str] = field(default_factory=list)
    """Available voice IDs (plain strings).  Empty for STT models.

    When ``voices_meta`` is provided this list is derived from it automatically
    via the ``__post_init__`` method and should not be set manually.
    """

    voices_meta: list[VoiceSpec] = field(default_factory=list)
    """Rich voice metadata.  When set, ``voices`` is derived from this list."""

    language: str = "en"
    """Primary language code (backward compat).  Prefer ``supported_languages``."""

    supported_languages: list[str] = field(default_factory=lambda: ["en"])
    """All language codes the model can synthesise, e.g. ``["en", "it"]``.

    This list drives which language selectors appear in the WebUI and which
    per-language sample files are generated.
    """

    # Source: mutually exclusive — set one (or both if a model can be obtained
    # either from HuggingFace or via a direct URL).
    hf_repo_id: str | None = None
    """HuggingFace repo, e.g. ``KittenML/kitten-tts-nano-0.8``."""

    download_url: str | None = None
    """Direct URL to a zip/tar archive to be extracted into model_dir."""

    sample_text: str = _SAMPLE_TEXT
    """Text used to generate sample audio files (English fallback)."""

    def __post_init__(self) -> None:
        # Derive plain ``voices`` list from ``voices_meta`` when rich metadata
        # is provided.  This keeps backward compatibility for all callers that
        # only read ``spec.voices``.
        if self.voices_meta and not self.voices:
            self.voices = [v.name for v in self.voices_meta]
        # Keep ``language`` in sync with the first entry of ``supported_languages``
        # for backward-compat code that reads the singular field.
        if self.supported_languages and self.language == "en":
            self.language = self.supported_languages[0]


# ---------------------------------------------------------------------------
# ModelManager
# ---------------------------------------------------------------------------
class ModelManager:
    """Central registry and download manager for all plugin models."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._models: dict[str, ModelSpec] = {}
        self._active_downloads: dict[str, float] = {}  # model_id → progress 0-1

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, spec: ModelSpec) -> None:
        """Register a model spec.  Called by plugins at import time."""
        with self._lock:
            if spec.model_id in self._models:
                # Allow re-registration (idempotent on identical id)
                return
            self._models[spec.model_id] = spec
            log_info(
                f"[model_manager] Registered model '{spec.model_id}' (plugin={spec.plugin_id})"
            )

    # ------------------------------------------------------------------
    # Storage helpers
    # ------------------------------------------------------------------

    @property
    def models_dir(self) -> Path:
        """Root directory where all models are stored."""
        raw = os.environ.get(_ENV_MODELS_DIR, "")
        if raw:
            return Path(raw).expanduser()
        return _DEFAULT_MODELS_DIR

    def model_dir(self, model_id: str) -> Path:
        """Return the storage directory for a single model (may not exist)."""
        return self.models_dir / model_id

    def _manifest_path(self, model_id: str) -> Path:
        return self.model_dir(model_id) / _MANIFEST_FILENAME

    def _write_manifest(
        self, model_id: str, extra: dict[str, Any] | None = None
    ) -> None:
        manifest = {
            "model_id": model_id,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            manifest.update(extra)
        path = self._manifest_path(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)

    def _read_manifest(self, model_id: str) -> dict[str, Any] | None:
        path = self._manifest_path(model_id)
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_downloaded(self, model_id: str) -> bool:
        """Return True if the model directory exists and has a valid manifest."""
        return self._manifest_path(model_id).exists()

    def download_progress(self, model_id: str) -> float | None:
        """Return progress in range [0, 1] if a download is in progress, else None."""
        return self._active_downloads.get(model_id)

    def catalog(self) -> list[dict]:
        """Return all registered models with their current download status."""
        with self._lock:
            specs = list(self._models.values())
        result: list[dict] = []
        for s in specs:
            manifest = self._read_manifest(s.model_id)
            progress = self.download_progress(s.model_id)
            # Serialize voices_meta as plain dicts for JSON serialisation
            voices_meta_serialized = [
                {"name": v.name, "gender": v.gender, "languages": v.languages}
                for v in s.voices_meta
            ]
            entry: dict[str, Any] = {
                "model_id": s.model_id,
                "plugin_id": s.plugin_id,
                "display_name": s.display_name,
                "description": s.description,
                "tags": s.tags,
                "size_mb": s.size_mb,
                "voices": s.voices,
                "voices_meta": voices_meta_serialized,
                "language": s.language,
                "supported_languages": s.supported_languages,
                "downloaded": manifest is not None,
                "downloaded_at": (manifest or {}).get("downloaded_at"),
                "downloading": progress is not None,
                "download_progress": progress,
                "sample_url": self._sample_url(
                    s.model_id, s.voices[0] if s.voices else None
                ),
            }
            result.append(entry)
        return result

    def catalog_by_plugin(self) -> dict[str, list[dict]]:
        """Return catalog grouped by plugin_id."""
        all_items = self.catalog()
        grouped: dict[str, list[dict]] = {}
        for item in all_items:
            grouped.setdefault(item["plugin_id"], []).append(item)
        return grouped

    def downloaded_models(self) -> list[dict]:
        """Return only models that are currently downloaded."""
        return [m for m in self.catalog() if m["downloaded"]]

    def get_spec(self, model_id: str) -> ModelSpec | None:
        with self._lock:
            return self._models.get(model_id)

    # ------------------------------------------------------------------
    # Sample helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Sample migration helper
    # ------------------------------------------------------------------

    def _migrate_legacy_samples(self, model_id: str) -> None:
        """Rename old ``<voice>.mp3`` files to ``<voice>_en.mp3`` (one-shot migration).

        Called lazily the first time samples are queried for a model.
        """
        subdir = _SAMPLES_STATIC_DIR / model_id
        if not subdir.is_dir():
            return
        spec = self.get_spec(model_id)
        voice_names: set[str] = set(spec.voices) if spec else set()
        for mp3 in list(subdir.glob("*.mp3")):
            stem = mp3.stem  # e.g. "Bella" or "Bella_en"
            # Skip files that already have a language suffix
            if "_" in stem:
                parts = stem.rsplit("_", 1)
                if len(parts[1]) == 2 or len(parts[1]) == 3:  # «en», «de», «zh»…
                    continue
            # Only migrate files whose stem is a known voice name
            if stem not in voice_names and voice_names:
                continue
            new_path = mp3.with_name(f"{stem}_en.mp3")
            if not new_path.exists():
                try:
                    mp3.rename(new_path)
                    log_info(
                        f"[model_manager] Migrated legacy sample {mp3.name} → {new_path.name}"
                    )
                except Exception as exc:
                    log_warning(f"[model_manager] Could not migrate {mp3}: {exc}")

    # ------------------------------------------------------------------
    # Sample helpers
    # ------------------------------------------------------------------

    def _sample_url(
        self, model_id: str, voice: str | None, lang: str = "en"
    ) -> str | None:
        """Return WebUI-accessible URL for the MP3 sample, if it exists."""
        path = self._sample_path(model_id, voice, lang)
        if path and path.exists():
            rel = path.relative_to(_REPO_ROOT / "res" / "synth_webui")
            return f"/static/{rel.as_posix().lstrip('static/')}"
        return None

    def _sample_path(
        self, model_id: str, voice: str | None, lang: str = "en"
    ) -> Path | None:
        """Return the filesystem path for a sample MP3 (may not exist yet)."""
        subdir = _SAMPLES_STATIC_DIR / model_id
        if voice:
            return subdir / f"{voice}_{lang}.mp3"
        return subdir / f"sample_{lang}.mp3"

    def sample_exists(self, model_id: str, voice: str | None, lang: str = "en") -> bool:
        """Return True if a pre-generated sample file exists for the given combination."""
        self._migrate_legacy_samples(model_id)
        path = self._sample_path(model_id, voice, lang)
        return bool(path and path.exists())

    def list_samples(self, model_id: str, lang: str | None = None) -> list[dict]:
        """Return all available sample MP3s for a model.

        :param lang: If given, only return samples for this language.
                     Otherwise returns samples for all discovered languages.
        Each entry: ``{voice, lang, url, path}``
        """
        self._migrate_legacy_samples(model_id)
        spec = self.get_spec(model_id)
        if not spec:
            return []
        result: list[dict] = []
        voices = spec.voices if spec.voices else [None]  # type: ignore[list-item]
        langs_to_check: list[str]
        if lang:
            langs_to_check = [lang]
        else:
            langs_to_check = (
                spec.supported_languages if spec.supported_languages else ["en"]
            )
        for v in voices:
            for lng in langs_to_check:
                path = self._sample_path(model_id, v, lng)
                if path and path.exists():
                    url = self._sample_url(model_id, v, lng)
                    result.append(
                        {"voice": v, "lang": lng, "url": url, "path": str(path)}
                    )
        return result

    def ensure_sample(
        self,
        model_id: str,
        voice: str | None = None,
        generate_fn: Callable[[str, str | None], bytes | None] | None = None,
        lang: str = "en",
    ) -> Path | None:
        """Return path to a sample MP3, generating it on first call if possible.

        :param generate_fn: Optional callable(sample_text, voice) → MP3 bytes.
                            The model manager will call this to generate missing
                            samples; the bytes must be valid MP3 audio.
        :param lang: Language code for the sample (default ``en``).
        """
        self._migrate_legacy_samples(model_id)
        spec = self.get_spec(model_id)
        if not spec:
            return None
        path = self._sample_path(model_id, voice, lang)
        if path is None:
            return None
        if path.exists():
            return path
        if generate_fn is None:
            return None
        # Use language-specific sample text when available
        sample_text = SAMPLE_TEXT_BY_LANG.get(lang, spec.sample_text)
        try:
            audio_bytes = generate_fn(sample_text, voice)
            if audio_bytes:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(audio_bytes)
                log_info(f"[model_manager] Generated sample: {path}")
                return path
        except Exception as exc:
            log_warning(
                f"[model_manager] Sample generation failed for {model_id}/{voice}/{lang}: {exc}"
            )
        return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(
        self,
        model_id: str,
        on_progress: Callable[[float], None] | None = None,
    ) -> bool:
        """Download a model asynchronously.

        Returns True on success, False on failure.  Progress is reported via
        *on_progress* (float in [0, 1]) if provided.  Also updates
        ``download_progress(model_id)`` for polling clients.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._download_sync, model_id, on_progress
        )
        return result

    def _download_sync(
        self,
        model_id: str,
        on_progress: Callable[[float], None] | None,
    ) -> bool:
        """Synchronous download implementation (runs in thread pool)."""
        spec = self.get_spec(model_id)
        if not spec:
            log_error(f"[model_manager] Unknown model: {model_id}")
            return False

        if model_id in self._active_downloads:
            log_warning(f"[model_manager] Download already in progress for {model_id}")
            return False

        dest = self.model_dir(model_id)
        dest.mkdir(parents=True, exist_ok=True)

        def _progress(p: float) -> None:
            self._active_downloads[model_id] = max(0.0, min(1.0, p))
            if on_progress:
                try:
                    on_progress(p)
                except Exception:
                    pass

        self._active_downloads[model_id] = 0.0
        try:
            if spec.hf_repo_id:
                ok = self._download_hf(spec, dest, _progress)
            elif spec.download_url:
                ok = self._download_url(spec, dest, _progress)
            else:
                log_error(f"[model_manager] No download source for {model_id}")
                return False

            if ok:
                self._write_manifest(model_id)
                _progress(1.0)
                log_info(f"[model_manager] ✓ Model '{model_id}' downloaded to {dest}")
            return ok
        except Exception as exc:
            log_error(f"[model_manager] Download failed for {model_id}: {exc}")
            return False
        finally:
            self._active_downloads.pop(model_id, None)

    def _download_hf(
        self,
        spec: ModelSpec,
        dest: Path,
        progress: Callable[[float], None],
    ) -> bool:
        """Download from HuggingFace Hub using ``huggingface_hub``."""
        try:
            from huggingface_hub import snapshot_download  # type: ignore[import]

            progress(0.05)
            log_info(f"[model_manager] Downloading HF repo {spec.hf_repo_id} → {dest}")
            snapshot_download(
                repo_id=spec.hf_repo_id,
                local_dir=str(dest),
                local_dir_use_symlinks=False,
            )
            progress(0.95)
            return True
        except ImportError:
            log_error(
                "[model_manager] 'huggingface_hub' is not installed. "
                "Run: uv add huggingface_hub"
            )
            return False
        except Exception as exc:
            log_error(f"[model_manager] HuggingFace download failed: {exc}")
            return False

    def _download_url(
        self,
        spec: ModelSpec,
        dest: Path,
        progress: Callable[[float], None],
    ) -> bool:
        """Download from a direct URL and extract (zip supported)."""
        import tempfile
        import urllib.request
        import zipfile

        url = spec.download_url
        if not url:
            return False
        zip_path = dest.parent / f"_{spec.model_id}_download.zip"
        try:
            log_info(f"[model_manager] Downloading {url} …")
            progress(0.05)

            # Stream download with progress
            with urllib.request.urlopen(url) as resp:  # noqa: S310
                total = int(resp.headers.get("Content-Length", 0)) or 0
                downloaded = 0
                chunk = 65536
                with open(zip_path, "wb") as out:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        out.write(buf)
                        downloaded += len(buf)
                        if total:
                            progress(0.05 + 0.85 * min(downloaded / total, 1.0))

            progress(0.90)
            # Extract
            if zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zf:
                    # Most vosk zips contain a single top-level dir — extract directly
                    names = zf.namelist()
                    top_dirs = {n.split("/")[0] for n in names if "/" in n}
                    if len(top_dirs) == 1:
                        (top_name,) = top_dirs
                        for member in zf.infolist():
                            member_path = Path(member.filename)
                            # strip top-level dir
                            try:
                                rel = member_path.relative_to(top_name)
                            except ValueError:
                                rel = member_path
                            out_path = dest / rel
                            if member.is_dir():
                                out_path.mkdir(parents=True, exist_ok=True)
                            else:
                                out_path.parent.mkdir(parents=True, exist_ok=True)
                                with (
                                    zf.open(member) as src,
                                    open(out_path, "wb") as dst,
                                ):
                                    dst.write(src.read())
                    else:
                        zf.extractall(dest)
            else:
                # Assume raw tarball or other archive; try shutil
                with tempfile.TemporaryDirectory() as tmp:
                    shutil.unpack_archive(str(zip_path), tmp)
                    for item in Path(tmp).iterdir():
                        shutil.move(str(item), str(dest / item.name))
            progress(0.98)
            return True
        except Exception as exc:
            log_error(f"[model_manager] URL download/extract failed: {exc}")
            return False
        finally:
            try:
                zip_path.unlink(missing_ok=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete(self, model_id: str) -> bool:
        """Delete a downloaded model from disk.  Returns True on success."""
        dest = self.model_dir(model_id)
        if not self.is_downloaded(model_id):
            log_warning(f"[model_manager] delete: {model_id} is not downloaded")
            return False
        try:
            shutil.rmtree(dest, ignore_errors=False)
            log_info(f"[model_manager] Deleted model '{model_id}' from {dest}")
            return True
        except Exception as exc:
            log_error(f"[model_manager] Failed to delete {model_id}: {exc}")
            return False

    async def update(self, model_id: str) -> bool:
        """Re-download a model (delete old, then download fresh)."""
        self.delete(model_id)
        return await self.download(model_id)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
MODEL_MANAGER: ModelManager = ModelManager()
"""Global singleton — import and use this everywhere."""
