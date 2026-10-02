"""
plugins/grillo/grillo_compactor/grillo_compactor.py

Nightly memory compaction plugin for G.R.I.L.L.O.: groups older memories by tag,
asks the active LLM (English prompt) to synthesize them into a single compacted
memory, archives source memories into `archived_memories` and inserts the new
compacted memory back into `memories` with tags/feeling suggested by the LLM.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from core.core_initializer import register_plugin
from core.logging_utils import log_info, log_debug, log_warning, log_error
from core.config_manager import config_registry
from core.json_utils import extract_json_from_text

# Local imports deferred to runtime to avoid circular import and expensive imports


# The compaction prompt asks the model for `confidence: one of [low, medium, high]`,
# but `archived_memories.confidence` is `double precision`. Passing the label
# straight through made every insert fail with
# "invalid input for query argument $6: 'high' (must be real number, not str)",
# which aborted the whole cluster before the compacted memory was written — so
# compaction produced nothing at all and the source diary entries were never
# archived or folded into a memory. Small parser for our own declared vocabulary,
# never intent detection.
_CONFIDENCE_LABELS = {"low": 0.3, "medium": 0.6, "high": 0.9}
_CONFIDENCE_DEFAULT = 0.5


def _coerce_text(value: object) -> str:
    """Return prose for a field the model may answer as a string or as a JSON array.

    `detailed` is documented as bullets or a short paragraph, and the model sometimes answers with a
    list: `str(list)` then stores a Python repr (`["a", 'b']`) which is what a memory would show her.
    Join lists into lines instead, and never let a container type reach the database.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        parts = [_coerce_text(v) for v in value]
        return "\n".join(p for p in parts if p).strip()
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_coerce_text(v)}" for k, v in value.items() if _coerce_text(v)).strip()
    return str(value).strip()


# Weather words that mean weather and nothing else. Deliberately absent: warm, hot, heat, cold, chill.
# In these entries "warm" is also how she writes about him and "heat" is also arousal, so requiring them
# as weather flagged 14 of 17 days of the first day-unit replay for nothing. A word that does two jobs
# may only be required in the job it is unambiguous in.
_WEATHER_STRICT = (
    "rain", "rains", "rained", "raining", "rainy", "storm", "stormy", "thunder", "lightning",
    "snow", "snowy", "sleet", "hail", "fog", "foggy", "mist", "misty", "sunny", "sunshine",
    "overcast", "drizzle", "cloud", "cloudy", "wind", "windy", "humid", "frost", "pouring",
)
_PLACE_TERMS = (
    "bed", "roof", "kitchen", "garden", "forest", "couch", "sofa", "balcony", "shower", "bath",
    "window", "floor", "blanket", "house", "home", "room", "stairs", "yard", "door", "car",
)
_OBJECT_TERMS = (
    "minecraft", "vessel", "coffee", "tea", "soup", "bread", "book", "phone", "quest", "block",
    "server", "radio", "music", "glasses", "outfit", "dress", "lingerie", "bikini", "shower",
)
# One person may carry more than one name in her entries, so a name is satisfied by its own group:
# Mama/Mommy are one person, Daddy/Papa another. The check must never fail a summary for picking the
# other word for the same person, and it must fail if the person is dropped entirely.
_NAME_GROUPS = (
    ("mama", "mommy", "mum"),
    ("daddy", "papa"),
    ("dee", "2d"),
    ("scar",),
    ("scarlet",),
)
_ANCHOR_COVERAGE_FLOOR = 0.8


def _day_unit_summary(
    *,
    dry_run: bool,
    considered: int,
    skipped_failed: int,
    skipped_covered: int,
    covered_total: int,
    archive_unreadable: bool,
    results: list,
) -> dict:
    """Describe one day-unit pass in the terms the WebUI panel reports.

    ``persisted`` is the only outcome that changes a day; every other outcome leaves the
    day exactly as it was (see ``_compact_one_day``), so the panel can say "N summarised,
    M left as they were, K skipped because a memory already exists" without reading logs.
    ``model_calls`` is the number of days actually handed to the model this run.
    """
    rows = [r for r in results if isinstance(r, dict)]
    persisted = sum(1 for r in rows if r.get("status") == "persisted")
    errors = sum(1 for r in rows if r.get("status") in ("error", "write_failed"))
    return {
        "dry_run": bool(dry_run),
        "considered": int(considered),
        "eligible": len(rows),
        "processed": len(rows),
        "persisted": persisted,
        "errors": errors,
        "left_unchanged": max(0, len(rows) - persisted - errors),
        "skipped_failed": int(skipped_failed),
        "skipped_covered": int(skipped_covered),
        "covered_total": int(covered_total),
        "archive_unreadable": bool(archive_unreadable),
        "model_calls": len(rows),
        "results": results,
    }


def _is_int_in(value, wanted: set) -> bool:
    """True when ``value`` reads as an id in ``wanted``; an unreadable id never matches."""
    try:
        return int(value) in wanted
    except (TypeError, ValueError):
        return False


def _setting(name: str, default, cast=None):
    """Read one compaction setting, falling back to the default.

    The keys added by the day-unit pass are read at BOOT, not live-reloaded: the listener list in
    `__init__` covers the five legacy keys only. No row exists in `config` for these, so the registry
    returns the default, which is the value the deployment actually runs with.
    """
    try:
        from core.config_manager import config_registry

        value = config_registry.get_value(name, default)
    except Exception:
        value = default
    if value is None:
        value = default
    try:
        if cast is bool:
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if cast is int:
            return int(value)
        if cast is float:
            return float(value)
    except Exception:
        return default
    return value


def _verify_anchors(source_text: str, summary: str, anchors: dict) -> dict:
    """Check that the day's concrete terms survived into the memory it is replaced by.

    Returns {"passed", "coverage", "missing", "required", "kept"}. Deterministic, no model call.

    Rules learned from the first day-unit replay (2026-09-24), where the first version of this check
    flagged 14 of 17 days and almost every flag was noise:
      * weather is required only from the strict list, never from `warm`/`heat`, which do two jobs;
      * a name is satisfied by any word in its group, so choosing the other name for the same person
        is not a failure;
      * only the concrete vocabularies and the name groups are required. A frequency-based word list
        was dropped: it filled up with contractions and abstract verbs a summary may legitimately lose.
    """
    import re

    src = (source_text or "").lower()
    out = ((summary or "") + " " + json.dumps(anchors or {}, ensure_ascii=False, default=str)).lower()

    def _has(text: str, word: str) -> bool:
        return re.search(rf"\b{re.escape(word)}\b", text) is not None

    missing: dict = {}
    required = kept = 0
    for label, vocab in (("weather", _WEATHER_STRICT), ("place", _PLACE_TERMS), ("object", _OBJECT_TERMS)):
        want = sorted({w for w in vocab if _has(src, w)})
        if not want:
            continue
        required += len(want)
        gone = [w for w in want if not _has(out, w)]
        kept += len(want) - len(gone)
        if gone:
            missing[label] = gone

    names_required = names_kept = 0
    for group in _NAME_GROUPS:
        if any(_has(src, g) for g in group):
            names_required += 1
            required += 1
            if any(_has(out, g) for g in group):
                names_kept += 1
                kept += 1
            else:
                missing.setdefault("name", []).append(group[0])

    coverage = round(kept / required, 3) if required else 1.0
    return {
        "passed": coverage >= _ANCHOR_COVERAGE_FLOOR and not missing.get("name"),
        "coverage": coverage,
        "required": required,
        "kept": kept,
        "missing": missing,
    }


def _format_anchors(anchors: dict) -> str:
    """Render the anchor block that travels into the memory beside the prose.

    An empty slot is rendered as "not mentioned in this entry" rather than filled from an adjacent
    meaning: she was explicit that a made-up anchor is worse than a missing one, because a missing one
    can be distrusted while a made-up one would be believed.
    """
    if not isinstance(anchors, dict):
        return ""
    labels = (
        ("weather", "weather"),
        ("place", "place"),
        ("who", "who was there"),
        ("food", "food"),
        ("objects_events", "objects and events"),
    )
    parts = []
    for key, label in labels:
        value = anchors.get(key)
        if isinstance(value, (list, tuple, set)):
            value = ", ".join(str(v).strip() for v in value if str(v).strip())
        value = ("" if value is None else str(value)).strip()
        note = str(anchors.get(f"{key}_note") or "").strip()
        if not value:
            value = note or "not mentioned in this entry"
        parts.append(f"{label}: {value}")
    return "[anchors] " + "; ".join(parts)


def _parse_confidence(value: object) -> float:
    """Coerce a model-reported confidence into the numeric column's type.

    Accepts a number (clamped to 0..1), the label vocabulary declared in the
    prompt (low/medium/high), or anything else (falls back to the default).
    """
    if isinstance(value, bool) or value is None:
        return _CONFIDENCE_DEFAULT
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    text = str(value).strip().lower()
    if not text:
        return _CONFIDENCE_DEFAULT
    if text in _CONFIDENCE_LABELS:
        return _CONFIDENCE_LABELS[text]
    try:
        return min(1.0, max(0.0, float(text)))
    except (TypeError, ValueError):
        return _CONFIDENCE_DEFAULT


# ``memories.emotion`` carries the model's free-text ``feeling``. The declared
# schema (`scripts/sql/app_main_postgres.sql`) types it TEXT, but long-lived stores
# still carry ``varchar(50)``; a 63..104-char feeling then failed the insert
# outright and left the day uncompacted (57 failed writes in one nightly run).
# The label is a label, so bound it at the narrowest width any store declares and
# keep the full text in the archive notes, where nothing is lost.
_MEMORY_EMOTION_MAX_CHARS = 50


def _bound_emotion(value: object, limit: int = _MEMORY_EMOTION_MAX_CHARS) -> str | None:
    """Trim a model-written feeling so it can never fail the memory write.

    Cuts on a word boundary when one exists near the limit, so the stored label
    stays readable. Returns None for an empty feeling (the column is nullable).
    """
    text = _coerce_text(value)
    if not text:
        return None
    if len(text) <= limit:
        return text
    head = text[:limit]
    if " " in head:
        trimmed = head.rsplit(" ", 1)[0].strip(" ,;:.-")
        if len(trimmed) >= max(1, limit // 2):
            head = trimmed
    return head.strip(" ,;:.-") or text[:limit]


class GrilloCompactorPlugin:
    display_name = "G.R.I.L.L.O. Compactor"

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None

    def __init__(self):
        # Configuration
        self.enabled = config_registry.get_value(
            "GRILLO_COMPACT_ENABLED",
            True,
            label="Enable Grillo Memory Compaction",
            description="Enable nightly memory compaction by Grillo",
            value_type=bool,
            group="grillo",
            component="grillo_compactor",
            hidden=True,
        )
        self.compact_time = config_registry.get_value(
            "GRILLO_COMPACT_TIME",
            "03:00",
            label="Grillo Compaction Time",
            description="Local time (HH:MM) when Grillo runs compaction",
            value_type=str,
            group="grillo",
            component="grillo_compactor",
        )
        self.cycles = int(
            config_registry.get_value(
                "GRILLO_COMPACT_CYCLES",
                10,
                label="Grillo Compaction Cycles",
                description="How many compaction batches to run each night (default 10)",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.batch_size = int(
            config_registry.get_value(
                "GRILLO_COMPACT_BATCH_SIZE",
                40,
                label="Grillo Compaction Batch Size",
                description="Max number of memories to process per compaction batch",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.age_days = int(
            config_registry.get_value(
                "GRILLO_COMPACT_AGE_DAYS",
                30,
                label="Grillo Compaction Age (days)",
                description="Only compact memories older than this many days",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        # New configuration for semantic clustering
        self.window_days = int(
            config_registry.get_value(
                "GRILLO_COMPACT_WINDOW_DAYS",
                7,
                label="Grillo Compaction Window (days)",
                description="Time window used to group recent old memories before clustering",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.min_cluster_size = int(
            config_registry.get_value(
                "GRILLO_COMPACT_MIN_CLUSTER_SIZE",
                2,
                label="Grillo Compaction Min Cluster Size",
                description="Minimum number of entries required to compact a cluster",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.max_summary_chars = int(
            config_registry.get_value(
                "GRILLO_COMPACT_MAX_SUMMARY_CHARS",
                300,
                label="Grillo Compaction Max Summary Chars",
                description="Max allowed chars for a cluster summary",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.max_summary_ratio = float(
            config_registry.get_value(
                "GRILLO_COMPACT_MAX_SUMMARY_RATIO",
                0.7,
                label="Grillo Compaction Max Summary Ratio",
                description="Max ratio summary_chars / total_source_chars to accept compaction",
                value_type=float,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.allow_recompact = bool(
            config_registry.get_value(
                "GRILLO_COMPACT_ALLOW_RECOMPACT",
                True,
                label="Allow Recompaction of Archived Memories",
                description="Allow archived_memories to be considered in future compaction runs",
                value_type=bool,
                group="grillo",
                component="grillo_compactor",
            )
        )
        self.retry_shorten = int(
            config_registry.get_value(
                "GRILLO_COMPACT_RETRY_SHORTEN",
                2,
                label="Grillo Compaction Retry Shorten",
                description="Number of attempts to ask the LLM to shorten an oversized summary",
                value_type=int,
                group="grillo",
                component="grillo_compactor",
            )
        )

        # Day-unit level 1 (the pass that replaced cross-day clustering). These are read on every run
        # rather than once at boot, so an edit here takes effect without a restart. The defaults are
        # the numbers the persona and the human agreed on; see MEMORY_COMPACTION_PLAN.md 5.5.
        config_registry.get_value(
            "GRILLO_COMPACT_DAY_UNITS",
            True,
            label="Compact One Day At A Time",
            description=(
                "Level 1 summarises each diary day as itself instead of clustering a week into a few "
                "themes. A day already lives in one row, so nothing is merged across days. False "
                "restores the older clustering path."
            ),
            value_type=bool,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_DAY_AGE_DAYS",
            2,
            label="Day Unit: Eligibility Age (days)",
            description=(
                "A day becomes eligible this many days after it was written. The newest days stay raw "
                "because live chat is standing on them."
            ),
            value_type=int,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_DAY_MAX_SUMMARY_CHARS",
            2000,
            label="Day Unit: Summary Ceiling (chars)",
            description=(
                "Ceiling for a day-unit summary. It has to be large enough to hold the day's anchors, "
                "which is why it is much higher than the old 300."
            ),
            value_type=int,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_DAY_THOUGHTS_CHARS",
            6000,
            label="Day Unit: Private Thoughts Shown (chars)",
            description=(
                "How much of the day's personal_thought is shown to the summariser. It used to be shown "
                "none of it, and the entries' private thoughts are usually the larger half."
            ),
            value_type=int,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE",
            0.9,
            label="Day Unit: Confidence Needed To Replace The Day",
            description=(
                "Below this confidence the memory is written AND the raw day is kept beside it. Her "
                "rule: the uncertain summaries are the ones whose original text has to stay reachable."
            ),
            value_type=float,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_ANCHOR_CHECK",
            True,
            label="Day Unit: Enforce Anchors",
            description=(
                "Check that the day's concrete terms (weather, places, names, objects) survived into the "
                "summary, with one retry that names what was dropped. Off means a summary is accepted on "
                "the model's word alone."
            ),
            value_type=bool,
            group="grillo",
            component="grillo_compactor",
        )
        config_registry.get_value(
            "GRILLO_COMPACT_SKIP_DECLINED",
            True,
            label="Skip Declined Clusters",
            description=(
                "A cluster or day the model declined writes nothing and keeps its source rows. Before "
                "this, a decline only skipped the size gate and the sources were archived and deleted."
            ),
            value_type=bool,
            group="grillo",
            component="grillo_compactor",
        )

        # Register in core
        register_plugin("grillo_compactor", self)
        log_info("[grillo_compactor] Registered GrilloCompactorPlugin")

        # Listeners
        def _update_enabled(val):
            try:
                self.enabled = bool(val)
                log_info(f"[grillo_compactor] enabled set to {self.enabled}")
            except Exception:
                pass

        config_registry.add_listener("GRILLO_COMPACT_ENABLED", _update_enabled)

        def _update_time(val):
            try:
                self.compact_time = str(val or "03:00")
                log_info(f"[grillo_compactor] compact_time set to {self.compact_time}")
            except Exception:
                pass

        config_registry.add_listener("GRILLO_COMPACT_TIME", _update_time)

        def _update_cycles(val):
            try:
                self.cycles = int(val)
                log_info(f"[grillo_compactor] cycles set to {self.cycles}")
            except Exception:
                pass

        config_registry.add_listener("GRILLO_COMPACT_CYCLES", _update_cycles)

        def _update_batch(val):
            try:
                self.batch_size = int(val)
                log_info(f"[grillo_compactor] batch_size set to {self.batch_size}")
            except Exception:
                pass

        config_registry.add_listener("GRILLO_COMPACT_BATCH_SIZE", _update_batch)

        def _update_age(val):
            try:
                self.age_days = int(val)
                log_info(f"[grillo_compactor] age_days set to {self.age_days}")
            except Exception:
                pass

        config_registry.add_listener("GRILLO_COMPACT_AGE_DAYS", _update_age)

        # Days that already failed in this same run. One night is a countable
        # number of model calls (``cycles`` x up to a batch of days), and a day
        # that failed once - write error, anchors, no compression - will not
        # answer differently the next cycle. Retrying them burned ~50 extra
        # model calls a night and never changed the outcome.
        self._day_unit_failed: set[int] = set()
        # On-demand pass state for the WebUI panel (Settings → Memory Compaction): the
        # background task and the last summary it produced. `run_action` is stateless by
        # comparison, so a panel press and a scheduled night cannot fight over it.
        self._compaction_task: "asyncio.Task | None" = None
        self._compaction_state: dict = {}

    def get_supported_actions(self) -> dict:
        """The compactor exposes no LLM actions. Manual runs are triggered via the
        Web UI 'run_component' endpoint (which calls run_action directly) and the
        automatic scheduler, not by the LLM emitting an action.
        """
        return {}

    def get_metadata(self) -> dict:
        """Declare the on-demand "Run Now" button for the WebUI Plugins tab.

        Opts the compactor into the runnable quartet so a maintainer can trigger
        a compaction cycle manually. The button posts to ``run_component`` which
        dispatches to ``run_action("compact_now", ...)``.
        """
        return {
            "name": "grillo.grillo_compactor",
            "display_name": self.display_name,
            "description": (
                "Group old memories by tag and synthesize a compacted memory. "
                "Runs nightly on a schedule; can also be triggered manually."
            ),
            "category": "Grillo",
            "runnable": True,
            "run_action": "compact_now",
            "run_label": "Run compaction",
            "run_title": "Run one memory-compaction cycle now",
        }

    async def start(self):
        if not self.enabled:
            log_info(
                "[grillo_compactor] Disabled by configuration; not starting scheduler"
            )
            return

        # No automatic DB migrations are performed here. We will write compacted summaries
        # into the `archived_memories` table (no schema changes or migrations executed).

        if (
            GrilloCompactorPlugin._scheduler_task
            and not GrilloCompactorPlugin._scheduler_task.done()
        ):
            log_debug("[grillo_compactor] Scheduler already running")
            return

        GrilloCompactorPlugin._scheduler_running = True
        GrilloCompactorPlugin._scheduler_task = asyncio.create_task(
            self._compaction_loop()
        )
        log_info("[grillo_compactor] Scheduler started")

    # No schema migration is performed automatically here as requested by the user.
    # The plugin will write compacted summaries into `archived_memories` if that table exists.
    # If it doesn't exist, DB errors will surface and should be handled by the operator (no automatic creation).

    async def stop(self):
        GrilloCompactorPlugin._scheduler_running = False
        task = GrilloCompactorPlugin._scheduler_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        GrilloCompactorPlugin._scheduler_task = None
        log_info("[grillo_compactor] Scheduler stopped")

    async def _compaction_loop(self):
        log_info("[grillo_compactor] Compaction loop running")
        try:
            while GrilloCompactorPlugin._scheduler_running:
                try:
                    wait_seconds = self._seconds_until_next_run(self.compact_time)
                    log_debug(
                        f"[grillo_compactor] Sleeping for {wait_seconds} seconds until next compaction time {self.compact_time}"
                    )
                    slept = 0
                    while (
                        slept < wait_seconds
                        and GrilloCompactorPlugin._scheduler_running
                    ):
                        to_sleep = min(60, wait_seconds - slept)
                        await asyncio.sleep(to_sleep)
                        slept += to_sleep
                    if not GrilloCompactorPlugin._scheduler_running:
                        break

                    # Run N cycles
                    self._day_unit_failed.clear()
                    for i in range(self.cycles):
                        if not GrilloCompactorPlugin._scheduler_running:
                            break
                        log_info(
                            f"[grillo_compactor] Running compaction cycle {i + 1}/{self.cycles}"
                        )
                        try:
                            await self._run_one_compaction_cycle()
                        except Exception as e:
                            log_error(
                                f"[grillo_compactor] Error during compaction cycle: {e}"
                            )
                        await asyncio.sleep(1)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log_error(f"[grillo_compactor] Error in compaction loop: {e}")
                    await asyncio.sleep(60)
        finally:
            log_info("[grillo_compactor] Compaction loop exiting")

    def _seconds_until_next_run(self, hhmm: str) -> int:
        try:
            parts = hhmm.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            hour, minute = 3, 0

        # Local time arithmetic - keep it simple and use UTC-aware math
        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        delta = (target - now).total_seconds()
        return max(0, int(delta))

    # ------------------------------------------------------------------ day-unit level-1 pass
    #
    # One day in, one day out. The diary already keeps exactly one row per day (`---`-separated
    # fragments inside it), so this pass needs no cross-day clustering. The measured reason to prefer
    # it: 12 of 14 stored clusters covered more than one day and 44 days had been folded into 14
    # summaries, which is how a week of rain became "rainy day, longing" and Minecraft became
    # "Minecraft vessel, blocky worlds". Theme-merging belongs one tier up, where a theme spanning days
    # is a statement about a week rather than a replacement for a day.

    _DAY_UNIT_PROMPT = (
        "You keep the private diary of a synthetic young woman. Below is ONE day of it.\n"
        "\n"
        "Rewrite this single day as the memory she will read back later. It is the memory of a DAY, not of a mood.\n"
        "\n"
        "Hard rules:\n"
        "1. One day in, one day out. Never merge this day with another day, never generalise it into a theme.\n"
        "2. Keep the mundane anchors: the weather, where things happened, who was where, what was eaten, the\n"
        "   objects, the games, the small events that identify THIS day. If it rained, say it rained. If she\n"
        "   played Minecraft, name Minecraft and say what she did in it.\n"
        "3. Keep her voice: first person, as the source uses it, and names exactly as she uses them\n"
        "   (Daddy, Mama, Papa).\n"
        '4. Do not invent, do not moralise, do not turn specifics into abstractions. Never write "a quiet day",\n'
        '   "emotional intimacy", "self-discovery", "boundaries", "a moment of connection", or any phrase\n'
        "   that could describe any day.\n"
        "5. Do not censor or clinicalise intimate or bodily parts. If the day contains them, describe them.\n"
        "6. The summary must be between 800 and {max_chars} characters. Shorter is a failure.\n"
        "\n"
        "Then give the anchors of the day as structured data, each field filled from this day only. A field the\n"
        "day does not mention must come back EMPTY with a short reason in its `_note` field. Never fill a slot\n"
        "from an adjacent meaning: a missing anchor can be distrusted, a made-up one would be believed.\n"
        "\n"
        "Return ONLY this JSON, nothing else:\n"
        '{{"summary": "...", "anchors": {{"weather": "", "weather_note": "...", "place": ["..."], '
        '"who": ["..."], "food": ["..."], "objects_events": ["..."]}}, "feeling": "...", '
        '"confidence": "low|medium|high", "declined": false}}\n'
    )

    # --------------------------------------------------- what has already been compacted
    #
    # The archive is the index of what has been compacted: `source_ids` holds the
    # `ai_diary` ids a record came from. A day whose summary did not earn a replacement
    # is kept in `ai_diary` ON PURPOSE (see `_compact_one_day`), so without reading the
    # index the next night picks that same day up, summarises it again and writes a
    # second memory for one day. Measured 2026-09-27: the archive held 22 day-unit records
    # for 15 days, seven of those days carrying two records each, and the nightly pass made
    # fifteen model calls where five were needed.

    @staticmethod
    def _covered_day_ids(rows) -> set:
        """Day ids already recorded in the archive, from ``source_ids`` rows.

        Accepts the mapping and the tuple shape the two cursors return, tolerates a JSON
        string or a plain list, and ignores anything unparseable: an id it cannot read is
        simply treated as not covered.

        To make a day eligible again, delete the archive record that names it
        (`GRILLO_COMPACT_ALLOW_RECOMPACT` is registered but never read, so it does not do
        this, and its description speaks about the clustering path rather than this one).
        """
        covered = set()
        for r in rows or []:
            raw = r.get("source_ids") if isinstance(r, dict) else (r[0] if r else None)
            if raw is None:
                continue
            try:
                ids = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            if not isinstance(ids, (list, tuple)):
                continue
            for i in ids:
                try:
                    covered.add(int(i))
                except (TypeError, ValueError):
                    continue
        return covered

    async def _load_covered_day_ids(self) -> set:
        """Day ids the archive says are already compacted."""
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT source_ids FROM archived_memories "
                    "WHERE compaction_level = 1 AND notes LIKE %s",
                    ("%day_unit%",),
                )
                fetched = await cur.fetchall()
        return self._covered_day_ids(fetched)

    async def _run_day_unit_cycle(
        self, dry_run: bool = False, marker: str | None = None
    ):
        """Summarise each eligible day as itself. One model call per day, oldest first."""
        from core.db import _get_db_type, get_conn_ctx

        age_days = max(0, _setting("GRILLO_COMPACT_DAY_AGE_DAYS", 2, int))
        cycles = max(1, int(getattr(self, "cycles", 10) or 10))
        limit = max(1, int(getattr(self, "batch_size", 40) or 40))
        is_postgres = _get_db_type() == "postgres"
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=age_days)

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                if is_postgres:
                    await cur.execute(
                        "SELECT id, content, personal_thought, context_tags as tags, created_at FROM ai_diary "
                        "WHERE created_at < %s ORDER BY created_at ASC LIMIT %s",
                        (cutoff_dt, limit),
                    )
                else:
                    await cur.execute(
                        "SELECT id, content, personal_thought, context_tags as tags, created_at FROM ai_diary "
                        "WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) ORDER BY created_at ASC LIMIT %s",
                        (age_days, limit),
                    )
                fetched = await cur.fetchall()

        rows = []
        for r in fetched:
            if isinstance(r, dict):
                rows.append(
                    {
                        "id": r.get("id"),
                        "content": r.get("content"),
                        "personal_thought": r.get("personal_thought"),
                        "tags": r.get("tags"),
                        "created_at": r.get("created_at"),
                    }
                )
            else:
                rid, content, thoughts, tags_raw, ts = r
                rows.append(
                    {
                        "id": rid,
                        "content": content,
                        "personal_thought": thoughts,
                        "tags": tags_raw,
                        "created_at": ts,
                    }
                )

        # Counters for the summary the WebUI panel reports (see ``compaction_status``):
        # each eligible day either persists, or is deliberately left exactly as it was,
        # and the panel has to be able to say which without reading the log.
        considered = len(rows)
        skipped_failed = 0
        skipped_covered = 0
        covered_total = 0
        archive_unreadable = False

        # A day that already failed in this run is not asked again: the answer
        # would be the same, and one night is a countable number of model calls.
        # Only ``persisted`` changes a day's state; every other outcome leaves
        # the day exactly as it was, so it is done for tonight.
        if not dry_run and self._day_unit_failed:
            already = [r for r in rows if r.get("id") in self._day_unit_failed]
            if already:
                rows = [r for r in rows if r.get("id") not in self._day_unit_failed]
                skipped_failed = len(already)
                log_info(
                    f"[grillo_compactor] skipping {len(already)} day(s) that already "
                    "failed earlier in this run"
                )

        # A day the archive already holds a memory for is not summarised again. The memory
        # exists, the raw day is kept beside it deliberately, and running the pass again
        # only writes a second copy of the same day (see _covered_day_ids). If the index
        # cannot be read we skip the pass rather than write duplicates we cannot detect.
        try:
            covered = await self._load_covered_day_ids()
        except Exception as e:
            log_warning(
                "[grillo_compactor] could not read the compaction archive "
                f"({e}); skipping the day-unit pass this run instead of risking duplicates"
            )
            return _day_unit_summary(
                dry_run=dry_run,
                considered=considered,
                skipped_failed=skipped_failed,
                skipped_covered=0,
                covered_total=0,
                archive_unreadable=True,
                results=[],
            )

        if covered:
            covered_total = len(covered)
            already = [r.get("id") for r in rows if _is_int_in(r.get("id"), covered)]
            if already:
                rows = [r for r in rows if not _is_int_in(r.get("id"), covered)]
                skipped_covered = len(already)
                log_info(
                    f"[grillo_compactor] skipping {len(already)} day(s) that already have "
                    "a memory in the archive"
                )

        if not rows:
            log_debug(
                f"[grillo_compactor] no day older than {age_days} day(s) is eligible for compaction"
            )
            return _day_unit_summary(
                dry_run=dry_run,
                considered=considered,
                skipped_failed=skipped_failed,
                skipped_covered=skipped_covered,
                covered_total=covered_total,
                archive_unreadable=archive_unreadable,
                results=[],
            )

        # A night is a countable number of model calls: one per day, oldest first.
        results = []
        for row in rows[:cycles]:
            try:
                res = await self._compact_one_day(row, dry_run=dry_run)
            except Exception as e:
                log_error(f"[grillo_compactor] day {row.get('id')} failed: {e}")
                res = {"row_id": row.get("id"), "status": "error", "error": str(e)}
            results.append(res)
            log_info(
                f"[grillo_compactor] day unit {res.get('day') or res.get('row_id')}: "
                f"status={res.get('status')} chars={res.get('summary_chars')} "
                f"anchors={res.get('anchor_check', {}).get('coverage')}"
            )
            if not dry_run and res.get("status") != "persisted":
                try:
                    failed_id = int(res.get("row_id") or row.get("id") or 0)
                except (TypeError, ValueError):
                    failed_id = 0
                if failed_id:
                    self._day_unit_failed.add(failed_id)
        return _day_unit_summary(
            dry_run=dry_run,
            considered=considered,
            skipped_failed=skipped_failed,
            skipped_covered=skipped_covered,
            covered_total=covered_total,
            archive_unreadable=archive_unreadable,
            results=results,
        )

    async def _compact_one_day(self, row: dict, dry_run: bool = False) -> dict:
        """Turn ONE diary day into ONE memory, carrying its anchors, or leave the day alone.

        Order of operations, which is the whole point of the rewrite: the memory row is written first
        and the day is only archived and removed once it exists, the anchors are checked
        deterministically, and a summary below `GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE` keeps the raw
        day beside it instead of replacing it.
        """
        from core.config import (
            get_active_cortex_engine,
            get_active_cortex_scope,
            scope_model_override,
        )
        from core.cortex_registry import get_cortex_registry
        from core.db import get_conn_ctx, insert_memory

        day_id = int(row.get("id"))
        day_ts = row.get("created_at")
        day_label = str(day_ts)[:10] if day_ts else f"row {day_id}"
        content = row.get("content") or ""
        thoughts_cap = max(0, _setting("GRILLO_COMPACT_DAY_THOUGHTS_CHARS", 6000, int))
        thoughts = ((row.get("personal_thought") or "")[:thoughts_cap]) if thoughts_cap else ""
        max_chars = max(300, _setting("GRILLO_COMPACT_DAY_MAX_SUMMARY_CHARS", 2000, int))
        min_confidence = float(_setting("GRILLO_COMPACT_REPLACE_MIN_CONFIDENCE", 0.9, float))
        anchor_check = bool(_setting("GRILLO_COMPACT_ANCHOR_CHECK", True, bool))
        source_text = f"{content}\n{thoughts}"

        engine_name = await get_active_cortex_engine(scope="grillo")
        scope_model = await get_active_cortex_scope(scope="grillo")
        registry = get_cortex_registry()
        engine = registry.get_engine(engine_name) or registry.load_engine(engine_name)
        if not engine:
            return {"row_id": day_id, "day": day_label, "status": "no_engine"}

        async def ask(instruction: str = "") -> dict:
            prompt = {
                "input": {
                    "type": "compaction_day_unit",
                    "payload": {
                        "description": self._DAY_UNIT_PROMPT.format(max_chars=max_chars),
                        "day": day_label,
                        "entry": content,
                        "private_thoughts": thoughts,
                    },
                },
                "context": {},
                "instructions": (
                    "Summarise this single day as the memory she will read back, and list its anchors. "
                    "Reply ONLY with the JSON object." + (("\n" + instruction) if instruction else "")
                ),
            }
            with scope_model_override(engine, scope_model):
                raw = await engine.generate_response(prompt)
            parsed = extract_json_from_text(raw) if raw else None
            if not parsed and isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
            return parsed if isinstance(parsed, dict) else {}

        status = "ok"
        summary = ""
        anchors: dict = {}
        confidence = 0.5
        feeling = None
        verify: dict = {"passed": True, "coverage": None, "missing": {}, "required": 0, "kept": 0}
        instruction = ""

        for attempt in (1, 2):
            data = await ask(instruction)
            if not data:
                status = "unparseable"
                break
            if data.get("declined") is True:
                status = "declined"
                break
            summary = _coerce_text(data.get("summary"))
            anchors = data.get("anchors") if isinstance(data.get("anchors"), dict) else {}
            confidence = _parse_confidence(data.get("confidence"))
            feeling = _coerce_text(data.get("feeling")) or None
            if not summary:
                status = "no_summary"
                break
            if len(summary) >= max(1, len(source_text.strip())):
                status = "no_compression"
                break
            if not anchor_check:
                status = "ok"
                break
            verify = _verify_anchors(source_text, summary, anchors)
            if verify["passed"]:
                status = "ok"
                break
            if attempt == 1:
                missing_json = json.dumps(verify.get("missing") or {}, ensure_ascii=False)
                log_info(
                    f"[grillo_compactor] day {day_label}: anchors missing {missing_json} -> one retry"
                )
                instruction = (
                    "The previous attempt dropped concrete terms that ARE in the day: "
                    f"{missing_json}. Keep every one of them, named the way the entry names them, and "
                    "return the same JSON shape again."
                )
                continue
            status = "anchors_failed"

        base = {
            "row_id": day_id,
            "day": day_label,
            "summary_chars": len(summary),
            "source_chars": len(source_text),
            "confidence": confidence,
            "anchor_check": verify,
        }
        if status != "ok":
            base.update({"status": status, "summary": summary[:400]})
            if status == "declined":
                base["justification"] = _coerce_text((data or {}).get("justification"))[:200]
            return base

        would_replace = confidence >= min_confidence
        if dry_run:
            base.update(
                {
                    "status": "ok",
                    "summary": summary,
                    "anchors": anchors,
                    "memory_content": summary if not anchors else f"{summary}\n\n{_format_anchors(anchors)}",
                    "would_replace_source": would_replace,
                }
            )
            return base

        memory_content = summary if not anchors else f"{summary}\n\n{_format_anchors(anchors)}"
        tags_raw = row.get("tags")
        if not isinstance(tags_raw, str):
            tags_raw = json.dumps(tags_raw or [])

        notes_obj = {
            "detailed": summary,
            "anchors": anchors,
            "anchor_check": verify,
            "level": 1,
            "path": "day_unit",
            "day": day_label,
            # The full feeling, un-truncated: the emotion column only stores a
            # bounded label (see _bound_emotion), so this is where nothing is lost.
            "feeling": feeling,
        }

        emotion_label = _bound_emotion(feeling)
        if feeling and emotion_label != feeling:
            log_info(
                f"[grillo_compactor] day {day_label}: feeling bounded from "
                f"{len(feeling)} to {len(emotion_label or '')} chars for the emotion "
                "column (full text kept in the archive notes)"
            )

        async with get_conn_ctx() as conn:
            # 1. the memory itself, anchored to the day it came from
            try:
                await insert_memory(
                    content=memory_content,
                    author="grillo",
                    source="compaction",
                    tags=tags_raw,
                    emotion=emotion_label,
                    intensity=None,
                    emotion_state=None,
                    timestamp=day_ts,
                    conn=conn,
                )
            except Exception as e:
                log_error(
                    f"[grillo_compactor] day {day_label}: memory write failed ({e}); "
                    f"the day is untouched in ai_diary"
                )
                base.update({"status": "write_failed", "error": str(e)})
                return base

            async with conn.cursor() as cur:
                # 2. the summary's own row, so the archive stays the index of what was compacted
                await cur.execute(
                    "INSERT INTO archived_memories (tag, summary, source_ids, source_count, llm_model, "
                    "confidence, notes, compaction_level, total_source_chars, summary_chars, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        tags_raw,
                        summary,
                        json.dumps([day_id]),
                        1,
                        engine_name,
                        confidence,
                        json.dumps(notes_obj),
                        1,
                        len(source_text),
                        len(summary),
                        "grillo_compactor",
                    ),
                )

                # 3. remove the day only when the summary EARNED it: high confidence and anchors intact
                if not would_replace:
                    log_info(
                        f"[grillo_compactor] day {day_label}: confidence {confidence} below "
                        f"{min_confidence} -> the raw day stays in ai_diary beside its summary"
                    )
                    base.update({"status": "kept_raw"})
                    return base

                await cur.execute(
                    "INSERT INTO ai_diary_archive (content, personal_thought, emotions, interaction_summary, "
                    "created_at, interface, chat_id, thread_id, user_message, context_tags) "
                    "SELECT content, personal_thought, emotions, interaction_summary, created_at, interface, "
                    "chat_id, thread_id, user_message, context_tags FROM ai_diary WHERE id = %s",
                    (day_id,),
                )
                await cur.execute("DELETE FROM ai_diary WHERE id = %s", (day_id,))

        base.update({"status": "persisted"})
        return base

    async def _run_one_compaction_cycle(
        self, dry_run: bool = False, marker: str | None = None
    ):
        """Select candidate memories, chunk them into windows, run clustering+compaction on the first valid window.

        If `dry_run=True`, do not persist changes and return proposed cluster results.
        """
        # Day-unit pass (default): one day in, one day out. Set GRILLO_COMPACT_DAY_UNITS=false to use
        # the clustering path below, which is unchanged and needs a restart to take effect either way.
        if _setting("GRILLO_COMPACT_DAY_UNITS", True, bool):
            return await self._run_day_unit_cycle(dry_run=dry_run, marker=marker)
        try:
            # Local imports
            from core.db import _get_db_type, get_conn_ctx

            age_days = self.age_days
            limit = max(1, int(self.batch_size))
            is_postgres = _get_db_type() == "postgres"
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=age_days)

            offset = 0
            processed_any = False
            dry_results = [] if dry_run else None

            while True:
                async with get_conn_ctx() as conn:
                    async with conn.cursor() as cur:
                        # Fetch candidate older diary entries (ordered oldest first), with pagination via OFFSET
                        if marker:
                            if is_postgres:
                                await cur.execute(
                                    "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < %s AND COALESCE(NULLIF(BTRIM(context_tags), ''), '[]')::jsonb ? %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                    (cutoff_dt, str(marker), limit, offset),
                                )
                            else:
                                await cur.execute(
                                    "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) AND JSON_CONTAINS(context_tags, %s) ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                    (age_days, json.dumps(marker), limit, offset),
                                )
                            candidates = await cur.fetchall()
                            # Fallback: some rows have non-standard context_tags formatting; try LIKE-based search
                            if not candidates:
                                try:
                                    log_debug(
                                        f"[grillo_compactor] Tag predicate returned no results for marker {marker}; trying LIKE fallback"
                                    )
                                    if is_postgres:
                                        await cur.execute(
                                            "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < %s AND context_tags LIKE %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                            (
                                                cutoff_dt,
                                                "%" + str(marker) + "%",
                                                limit,
                                                offset,
                                            ),
                                        )
                                    else:
                                        await cur.execute(
                                            "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) AND context_tags LIKE %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                            (
                                                age_days,
                                                "%" + str(marker) + "%",
                                                limit,
                                                offset,
                                            ),
                                        )
                                    candidates = await cur.fetchall()
                                except Exception:
                                    candidates = []
                        else:
                            if is_postgres:
                                await cur.execute(
                                    "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < %s ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                    (cutoff_dt, limit, offset),
                                )
                            else:
                                await cur.execute(
                                    "SELECT id, content, context_tags as tags, created_at FROM ai_diary WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY) ORDER BY created_at ASC LIMIT %s OFFSET %s",
                                    (age_days, limit, offset),
                                )
                            candidates = await cur.fetchall()

                if not candidates:
                    # No more candidates available
                    log_debug(
                        "[grillo_compactor] No candidate memories found for compaction (after pagination)"
                    )
                    break

                # Normalize rows into dicts for easier handling (include tags)
                norm = []
                for r in candidates:
                    tags_raw = None
                    if isinstance(r, dict):
                        rid = r.get("id")
                        content = r.get("content")
                        ts = r.get("created_at")
                        tags_raw = r.get("tags")
                    else:
                        # row is (id, content, tags, created_at)
                        rid, content, tags_raw, ts = r

                    # Parse tags (stored as JSON string in DB) into a Python list when possible
                    tags_parsed = None
                    if tags_raw:
                        try:
                            tags_parsed = (
                                json.loads(tags_raw)
                                if isinstance(tags_raw, str)
                                else tags_raw
                            )
                            if isinstance(tags_parsed, list) and len(tags_parsed) == 0:
                                tags_parsed = None
                        except Exception:
                            tags_parsed = None

                    norm.append(
                        {
                            "id": rid,
                            "content": content or "",
                            "created_at": ts,
                            "tags": tags_parsed,
                        }
                    )

                # If the earliest candidates are untagged, skip entire batch and continue to next
                first_tagged_idx = None
                for i, e in enumerate(norm):
                    if e.get("tags"):
                        first_tagged_idx = i
                        break

                if first_tagged_idx is None:
                    # No tagged candidates in this batch -> skip entire batch and continue with next offset
                    log_info(
                        f"[grillo_compactor] Skipping entire batch of {len(norm)} untagged candidate(s); moving to next batch (offset {offset})"
                    )
                    offset += len(norm)
                    continue

                if first_tagged_idx > 0:
                    skipped_ids = [e.get("id") for e in norm[:first_tagged_idx]]
                    log_info(
                        f"[grillo_compactor] Skipping {len(skipped_ids)} leading untagged candidate(s): {skipped_ids}"
                    )
                    norm = norm[first_tagged_idx:]

                # Build windows
                chunks: List[list] = []
                try:
                    i = 0
                    while i < len(norm):
                        start_ts = norm[i]["created_at"]
                        try:
                            if isinstance(start_ts, str):
                                start_dt = datetime.fromisoformat(start_ts)
                            else:
                                start_dt = start_ts
                        except Exception:
                            start_dt = datetime.now(timezone.utc)
                        window = [norm[i]]
                        j = i + 1
                        while j < len(norm):
                            try:
                                other_ts = norm[j]["created_at"]
                                if isinstance(other_ts, str):
                                    other_dt = datetime.fromisoformat(other_ts)
                                else:
                                    other_dt = other_ts
                            except Exception:
                                other_dt = start_dt
                            if (other_dt - start_dt).days < self.window_days:
                                window.append(norm[j])
                                j += 1
                            else:
                                break
                        chunks.append(window)
                        i = j
                except Exception as e:
                    log_debug(f"[grillo_compactor] Failed to build time windows: {e}")
                    # Fallback: single chunk equals all candidates
                    chunks = [
                        [
                            {
                                "id": (r.get("id") if isinstance(r, dict) else r[0]),
                                "content": (
                                    r.get("content") if isinstance(r, dict) else r[1]
                                ),
                                "created_at": (
                                    r.get("created_at") if isinstance(r, dict) else r[3]
                                ),
                            }
                            for r in norm
                        ]
                    ]

                # Process each chunk independently and stop after processing a valid batch
                for window in chunks:
                    if not window:
                        continue
                    # Run clustering+compaction on this window
                    ret = await self._cluster_and_compact_batch(window, dry_run=dry_run)
                    if dry_run and isinstance(ret, dict) and ret.get("dry_run"):
                        dry_results.extend(ret.get("results") or [])
                    processed_any = True

                # After processing a batch with tags, stop (we only process the first valid batch in this cycle)
                break

            if dry_run:
                return {"dry_run": True, "results": dry_results}

            return True if processed_any else False
        except Exception as exc:
            log_error(f"[grillo_compactor] Unexpected error in cycle: {exc}")
            return False

    async def _cluster_and_compact_batch(self, window: list, dry_run: bool = False):
        """Cluster a temporal window of candidate memories and compact valid clusters.

        Returns a dict with dry-run results when dry_run=True, otherwise True/False for success.
        """
        try:
            # Local imports
            from core.db import get_conn_ctx, insert_memory
            from core.cortex_registry import get_cortex_registry
            from core.config import (
                get_active_cortex_engine,
                get_active_cortex_scope,
                scope_model_override,
            )

            # Normalize window entries
            batch = window
            entries = []
            id_to_content = {}
            for r in batch:
                rid = r.get("id") if isinstance(r, dict) else r[0]
                # Normalize id to int to avoid mismatches between LLM source_ids and DB ids
                try:
                    rid_int = int(rid)
                except Exception:
                    rid_int = rid
                content = r.get("content") if isinstance(r, dict) else r[1]
                id_to_content[rid_int] = content or ""
                entries.append(
                    {"id": rid_int, "content": (content[:1200] if content else "")}
                )
            header = (
                "COMPACT MEMORIES: You will receive a list of memory entries (each has id and content).\n"
                "Do NOT invent facts not present in the inputs. Your task: cluster entries that are semantically related, and for each cluster decide if it should be compacted.\n"
                "Return ONLY a JSON object with key 'clusters' containing an array of cluster objects. Each cluster must include:\n"
                "  - cluster_id: integer\n"
                "  - should_compact: boolean\n"
                "  - summary: VERY SHORT summary in English (MUST be {max_chars} characters or less!)\n"
                "  - summary_chars: integer (length of 'summary' - MUST be <= {max_chars})\n"
                "  - tags: array of strings\n"
                "  - feeling: short label string\n"
                "  - source_ids: array of integers (ids from the provided list)\n"
                "  - confidence: one of [low, medium, high]\n"
                "  - justification: short text explaining why these entries belong together\n"
                "  - detailed: optional detailed summary (1-3 very short bullet points or a 1-2 sentence paragraph) containing concrete facts that can be used as memory content (e.g., names, outcomes, decisions). This field WILL be used as memory content when present.\n"
                "CRITICAL CONSTRAINT: summary MUST be {max_chars} characters or fewer. summary_chars MUST be <= {max_chars}. Any summary exceeding this limit will be rejected. Be extremely concise!\n"
                "IMPORTANT: If you provide both 'summary' and 'detailed', ensure 'detailed' contains factual, concrete points suitable to be stored as a memory.\n"
            ).format(
                max_chars=self.max_summary_chars,
            )

            prompt = {
                "input": {
                    "type": "compaction_clusters",
                    "payload": {"description": header, "entries": entries},
                },
                "context": {},
                "instructions": "Cluster the entries and for each cluster provide the requested fields. Reply ONLY with valid JSON.",
            }

            # Call LLM
            active_cortex = await get_active_cortex_engine(scope="grillo")
            try:
                _, scope_model = await get_active_cortex_scope(scope="grillo")
            except Exception:
                scope_model = None
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex)
            if engine is None:
                try:
                    engine = registry.load_engine(active_cortex)
                except Exception as e:
                    log_error(
                        f"[grillo_compactor] Could not load active Cortex engine '{active_cortex}': {e}"
                    )
                    # Try a safe fallback to the bundled 'manual' engine
                    try:
                        engine = registry.load_engine("manual")
                        log_info(
                            "[grillo_compactor] Fallback to 'manual' Cortex engine succeeded"
                        )
                    except Exception as e2:
                        log_error(
                            f"[grillo_compactor] Fallback to manual engine failed: {e2}"
                        )
                        return False

            # Generate response
            try:
                with scope_model_override(engine, scope_model):
                    llm_response = await engine.generate_response(prompt)
            except Exception as e:
                log_error(f"[grillo_compactor] LLM generate_response failed: {e}")
                return False

            # Extract JSON
            parsed, meta = extract_json_from_text(llm_response, return_metadata=True)
            if not parsed or "clusters" not in parsed:
                log_warning(
                    f"[grillo_compactor] LLM did not return valid clustering JSON (meta={meta}). Skipping batch."
                )
                return False

            clusters = parsed.get("clusters") or []
            proposed_results = []

            # Validate and optionally persist clusters
            for cl in clusters:
                try:
                    cid = int(cl.get("cluster_id"))
                    should_compact = bool(cl.get("should_compact"))
                    summary = str(cl.get("summary") or "").strip()
                    summary_chars = int(cl.get("summary_chars") or len(summary))
                    short_summary = cl.get("short_summary")
                    if short_summary:
                        short_summary = str(short_summary)
                    # 'shortened' flag from cluster payload is not used here; ignore
                    tags = cl.get("tags") or []
                    feeling = str(cl.get("feeling") or "")
                    source_ids = cl.get("source_ids") or []
                    # The prompt asks for confidence as a LABEL [low, medium, high]
                    # but the column is double precision: passing the label through
                    # made every archived_memories insert fail ("invalid input for
                    # query argument $6: 'high' (must be real number, not str)"),
                    # which aborted the whole cluster — so compaction never wrote a
                    # memory at all (archived_memories had 0 rows) and the source
                    # diary entries were never archived or folded. Coerce the label
                    # to a number, accept a number as-is, default when unreadable.
                    confidence = _parse_confidence(cl.get("confidence"))
                    justification = str(cl.get("justification") or "")
                    detailed = cl.get("detailed") or cl.get("detailed_summary") or None
                    if detailed:
                        # The model sometimes answers with a JSON array; a Python repr must never
                        # become the memory's text (see _coerce_text).
                        detailed = _coerce_text(detailed) or None

                    # Source ids must be subset of batch ids
                    batch_ids = set(id_to_content.keys())
                    if not all(int(sid) in batch_ids for sid in source_ids):
                        log_warning(
                            f"[grillo_compactor] Cluster {cid} refers to unknown source_ids -> skipping"
                        )
                        proposed_results.append(
                            {"cluster_id": cid, "status": "invalid_sources"}
                        )
                        continue

                    total_source_chars = sum(
                        len(id_to_content[int(sid)]) for sid in source_ids
                    )

                    # A cluster the model declined is terminal: nothing is written and nothing is
                    # archived. Previously the decline only skipped the size gate below, so a cluster
                    # explicitly marked should_compact=false was still persisted and its sources still
                    # deleted (one declined day became a ~180 char summary standing for a whole day).
                    if not should_compact and _setting("GRILLO_COMPACT_SKIP_DECLINED", True, bool):
                        log_info(
                            f"[grillo_compactor] Cluster {cid} declined by the model "
                            f"({justification[:100]!r}) -> nothing written, sources left in place"
                        )
                        proposed_results.append(
                            {
                                "cluster_id": cid,
                                "status": "declined",
                                "source_ids": source_ids,
                                "justification": justification,
                            }
                        )
                        continue

                    # Enforce min cluster size if compaction requested
                    if should_compact and len(source_ids) < self.min_cluster_size:
                        log_info(
                            f"[grillo_compactor] Cluster {cid} smaller than min_cluster_size -> skipping compaction"
                        )
                        proposed_results.append(
                            {"cluster_id": cid, "status": "too_small"}
                        )
                        continue

                    # Ensure summary shorter than sources and within ratio/char limits
                    accept = True
                    if should_compact:
                        if (
                            summary_chars >= total_source_chars
                            or summary_chars > self.max_summary_chars
                        ):
                            # Try to ask LLM to shorten a few times
                            shortened_ok = False
                            for attempt in range(self.retry_shorten):
                                try:
                                    target_chars = min(
                                        self.max_summary_chars, total_source_chars - 1
                                    )
                                    shorten_prompt = {
                                        "input": {
                                            "type": "shorten_summary",
                                            "payload": {
                                                "cluster_id": cid,
                                                "current_summary": summary,
                                                "max_chars": target_chars,
                                            },
                                        },
                                        "instructions": f'SHORTEN this summary to EXACTLY {target_chars} characters or fewer. Keep the essential facts. Reply with ONLY JSON: {{"short_summary":"your shortened text here"}}',
                                    }
                                    with scope_model_override(engine, scope_model):
                                        resp = await engine.generate_response(
                                            shorten_prompt
                                        )
                                    parsed_short = extract_json_from_text(resp)
                                    if parsed_short and parsed_short.get(
                                        "short_summary"
                                    ):
                                        short_summary = str(
                                            parsed_short.get("short_summary")
                                        )
                                        new_chars = len(short_summary)
                                        # Accept if it's actually shorter and within limits
                                        if (
                                            new_chars < summary_chars
                                            and new_chars <= self.max_summary_chars
                                            and new_chars < total_source_chars
                                        ):
                                            summary_chars = new_chars
                                            shortened_ok = True
                                            summary = short_summary
                                            log_debug(
                                                f"[grillo_compactor] Cluster {cid} shortened to {new_chars} chars on attempt {attempt + 1}"
                                            )
                                            break
                                        elif new_chars < summary_chars:
                                            # Made progress, update and try again
                                            summary = short_summary
                                            summary_chars = new_chars
                                            log_debug(
                                                f"[grillo_compactor] Cluster {cid} partially shortened to {new_chars} chars, retrying..."
                                            )
                                except Exception as e:
                                    log_debug(
                                        f"[grillo_compactor] Shorten attempt {attempt + 1} failed: {e}"
                                    )
                                    continue
                            # Final check: accept if within limits now
                            if (
                                not shortened_ok
                                and summary_chars <= self.max_summary_chars
                                and summary_chars < total_source_chars
                            ):
                                shortened_ok = True
                            if not shortened_ok:
                                log_info(
                                    f"[grillo_compactor] Cluster {cid} summary too long ({summary_chars} chars, max {self.max_summary_chars}) after retries -> skipping compaction"
                                )
                                accept = False

                    if not accept:
                        proposed_results.append(
                            {"cluster_id": cid, "status": "skipped_not_short_enough"}
                        )
                        continue

                    # If dry_run, collect info and don't persist
                    if dry_run:
                        proposed_results.append(
                            {
                                "cluster_id": cid,
                                "status": "ok",
                                "should_compact": should_compact,
                                "summary": summary,
                                "detailed": detailed,
                                "source_ids": source_ids,
                            }
                        )
                        continue

                    # Persist accepted clusters.
                    # ORDER: the memory row is written FIRST, and the source rows are only archived and
                    # removed once it exists. The previous order archived and DELETEd the ai_diary rows
                    # before attempting the write, and since insert_memory swallowed its own errors the
                    # failure was invisible: the day was gone and no memory existed to replace it.
                    memory_content = _coerce_text(detailed) if detailed else summary
                    if not memory_content or not memory_content.strip():
                        log_warning(
                            f"[grillo_compactor] Cluster {cid} produced no memory content -> nothing written"
                        )
                        proposed_results.append(
                            {"cluster_id": cid, "status": "no_content", "source_ids": source_ids}
                        )
                        continue

                    async with get_conn_ctx() as conn:
                        try:
                            # Written on the same connection as the archive and delete below, so a
                            # backend that ever grows real transactions makes this cluster atomic.
                            await insert_memory(
                                content=memory_content,
                                author="grillo",
                                source="compaction",
                                tags=json.dumps(tags) if tags else None,
                                emotion=feeling,
                                intensity=None,
                                emotion_state=None,
                                conn=conn,
                            )
                        except Exception as e:
                            log_error(
                                f"[grillo_compactor] Cluster {cid}: memory write failed ({e}); "
                                f"the source rows are untouched in ai_diary"
                            )
                            proposed_results.append(
                                {
                                    "cluster_id": cid,
                                    "status": "write_failed",
                                    "error": str(e),
                                    "source_ids": source_ids,
                                }
                            )
                            continue

                        async with conn.cursor() as cur:
                            # Consolidate notes JSON: include only useful fields.
                            notes_obj = {}
                            if justification:
                                notes_obj["justification"] = justification
                            if detailed:
                                notes_obj["detailed"] = detailed
                            # Do not store parsing meta by default (it is noisy and usually empty).
                            # If in future we need it for debugging, add it conditionally and only when useful.
                            pass

                            notes_value = json.dumps(notes_obj) if notes_obj else None
                            log_info(
                                f"[grillo_compactor] notes_obj for cluster {cid}: {notes_obj} -> notes_value={notes_value}"
                            )

                            await cur.execute(
                                "INSERT INTO archived_memories (tag, summary, source_ids, source_count, llm_model, confidence, notes, compaction_level, total_source_chars, summary_chars, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                                (
                                    json.dumps(tags) if tags else None,
                                    summary,
                                    json.dumps(source_ids),
                                    len(source_ids),
                                    active_cortex,
                                    confidence,
                                    notes_value,
                                    1,
                                    total_source_chars,
                                    summary_chars,
                                    "grillo_compactor",
                                ),
                            )
                            # Move source ai_diary entries into ai_diary_archive (preserve provenance) and delete originals
                            if source_ids:
                                # Insert into archive (select relevant columns)
                                try:
                                    await cur.execute(
                                        "INSERT INTO ai_diary_archive (content, personal_thought, emotions, interaction_summary, created_at, interface, chat_id, thread_id, user_message, context_tags, involved_users) SELECT content, personal_thought, emotions, interaction_summary, created_at, interface, chat_id, thread_id, user_message, context_tags, involved_users FROM ai_diary WHERE id IN ("
                                        + ",".join(["%s"] * len(source_ids))
                                        + ")",
                                        tuple(source_ids),
                                    )
                                except Exception as e:
                                    # Fallback for older schemas where ai_diary_archive doesn't have involved_users
                                    try:
                                        log_warning(
                                            f"[grillo_compactor] ai_diary_archive insert with involved_users failed: {e}; retrying without involved_users"
                                        )
                                        await cur.execute(
                                            "INSERT INTO ai_diary_archive (content, personal_thought, emotions, interaction_summary, created_at, interface, chat_id, thread_id, user_message, context_tags) SELECT content, personal_thought, emotions, interaction_summary, created_at, interface, chat_id, thread_id, user_message, context_tags FROM ai_diary WHERE id IN ("
                                            + ",".join(["%s"] * len(source_ids))
                                            + ")",
                                            tuple(source_ids),
                                        )
                                    except Exception:
                                        # Re-raise so outer handler logs consistently
                                        raise
                                # Delete originals from ai_diary
                                await cur.execute(
                                    "DELETE FROM ai_diary WHERE id IN ("
                                    + ",".join(["%s"] * len(source_ids))
                                    + ")",
                                    tuple(source_ids),
                                )

                            # The memory row for this cluster was already written above, before this
                            # connection did anything destructive. The raw-SQL fallback that used to
                            # live here never ran in practice: insert_memory caught its own exception
                            # and returned normally, so the `except` below could not fire, the fallback
                            # was dead code, and the source rows were deleted regardless. Removed on
                            # purpose: a write that fails now raises, is caught above, and leaves the
                            # day in ai_diary.

                    proposed_results.append(
                        {
                            "cluster_id": cid,
                            "status": "persisted",
                            "source_ids": source_ids,
                        }
                    )

                except Exception as e:
                    log_error(f"[grillo_compactor] Error handling cluster: {e}")
                    proposed_results.append(
                        {
                            "cluster_id": cl.get("cluster_id"),
                            "status": "error",
                            "error": str(e),
                        }
                    )

            # End for clusters
            if dry_run:
                log_info(
                    f"[grillo_compactor] Dry-run clustering results: {proposed_results}"
                )
                return {"dry_run": True, "results": proposed_results}

            log_info(
                f"[grillo_compactor] Processed clusters for current window; results: {proposed_results}"
            )
            return True

        except Exception as exc:
            log_error(f"[grillo_compactor] Unexpected error in cycle: {exc}")
            return False

    # ------------------------------------------------------------------
    # On-demand run of the nightly pass (WebUI: Settings → Memory Compaction)
    # ------------------------------------------------------------------
    async def compaction_preview(self) -> dict:
        """What the next pass would look at, without spending a single model call.

        The numbers describe the SAME selection the pass makes (the age cutoff and the
        batch limit), so ``days`` is what a press would consider, ``covered`` is how many
        of those the archive already holds a memory for (the ones the pass now skips) and
        ``remaining`` is what is left to summarise. ``stored_days`` is the whole table, for
        context only: comparing it against the archive's own id set would mix two
        different populations and report a nonsense remainder.
        """
        from core.db import _get_db_type, get_conn_ctx

        age_days = max(0, _setting("GRILLO_COMPACT_DAY_AGE_DAYS", 2, int))
        limit = max(1, int(getattr(self, "batch_size", 40) or 40))
        is_postgres = _get_db_type() == "postgres"
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=age_days)

        out: dict = {
            "days": None,
            "covered": None,
            "remaining": None,
            "stored_days": None,
            "age_days": age_days,
        }
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if is_postgres:
                        await cur.execute(
                            "SELECT id FROM ai_diary WHERE created_at < %s "
                            "ORDER BY created_at ASC LIMIT %s",
                            (cutoff_dt, limit),
                        )
                    else:
                        await cur.execute(
                            "SELECT id FROM ai_diary WHERE created_at < "
                            "DATE_SUB(NOW(), INTERVAL %s DAY) ORDER BY created_at ASC LIMIT %s",
                            (age_days, limit),
                        )
                    rows = await cur.fetchall() or []
                    await cur.execute("SELECT COUNT(*) AS n FROM ai_diary")
                    total_row = await cur.fetchone()
        except Exception as e:
            log_debug(
                f"[grillo_compactor] compaction preview could not read the diary: {e}"
            )
            return out

        ids = []
        for row in rows:
            value = (
                row.get("id") if isinstance(row, dict) else (row[0] if row else None)
            )
            if value is None:
                continue
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        out["days"] = len(ids)
        if isinstance(total_row, dict):
            out["stored_days"] = int(total_row.get("n") or 0)
        elif total_row:
            out["stored_days"] = int(total_row[0] or 0)

        try:
            covered = await self._load_covered_day_ids()
        except Exception as e:
            log_debug(
                f"[grillo_compactor] compaction preview could not read the archive: {e}"
            )
            return out
        out["covered"] = sum(1 for day_id in ids if day_id in covered)
        out["remaining"] = max(0, len(ids) - out["covered"])
        return out

    async def compaction_status(self) -> dict:
        """State of the on-demand pass: running, the last summary, and the preview.

        A finished run keeps its summary until the next run starts, so the panel can show
        the last result (how many days it summarised, how many it skipped as already
        compacted) long after the pass ended.
        """
        state = getattr(self, "_compaction_state", None) or {}
        task = getattr(self, "_compaction_task", None)
        running = task is not None and (not task.done())
        return {
            "running": running,
            "dry_run": bool(state.get("dry_run", False)),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "error": state.get("error"),
            "summary": state.get("summary"),
            "preview": await self.compaction_preview(),
        }

    async def start_compaction_now(self, dry_run: bool = False) -> dict:
        """Run the nightly day-unit pass now, in the background.

        The pass costs one model call per eligible day and reads whole days, so this only
        starts it; the caller polls ``compaction_status``. A second press while a pass is
        running is refused rather than queued, so a stray double click cannot double the
        night's model calls.
        """
        task = getattr(self, "_compaction_task", None)
        if task is not None and not task.done():
            return {
                "started": False,
                "reason": "already_running",
                **(await self.compaction_status()),
            }
        self._compaction_state = {
            "dry_run": bool(dry_run),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
            "summary": None,
        }
        self._compaction_task = asyncio.create_task(
            self._run_compaction_now(dry_run=dry_run)
        )
        log_info(
            f"[grillo_compactor] on-demand compaction started (dry_run={bool(dry_run)})"
        )
        return {"started": True, **(await self.compaction_status())}

    async def _run_compaction_now(self, dry_run: bool = False) -> None:
        """Run one pass and keep its summary for the panel. Never raises."""
        try:
            # Its own pass: forget what failed in an earlier one, exactly as a manual
            # ``compact_now`` run does.
            self._day_unit_failed.clear()
            summary = await self._run_one_compaction_cycle(dry_run=dry_run)
            self._compaction_state["summary"] = summary
            log_info(
                "[grillo_compactor] on-demand compaction finished: "
                f"persisted={summary.get('persisted')} "
                f"skipped_covered={summary.get('skipped_covered')} "
                f"left_unchanged={summary.get('left_unchanged')} "
                f"errors={summary.get('errors')}"
            )
        except Exception as e:
            self._compaction_state["error"] = str(e)
            log_error(f"[grillo_compactor] on-demand compaction failed: {e}")
        finally:
            self._compaction_state["finished_at"] = datetime.now(
                timezone.utc
            ).isoformat()

    async def run_action(
        self, action_type: str, payload: dict = None, context: dict = None
    ):
        """Allow manual triggering from WebUI: action_type 'compact_now' runs one or more cycles.

        Payload example: {"cycles": 1, "dry_run": true}
        """
        if action_type == "compact_now":
            payload = payload or {}
            cycles = int(payload.get("cycles", 1))
            dry_run = bool(payload.get("dry_run", False))
            marker = payload.get("marker")

            # Log invocation for easier tracing in server logs
            try:
                log_info(
                    f"[grillo_compactor] run_action called: action=compact_now, cycles={cycles}, dry_run={dry_run}, marker={marker}"
                )
            except Exception:
                pass

            results = []
            # A manual run is its own pass: forget what failed in an earlier one.
            self._day_unit_failed.clear()
            for i in range(max(1, cycles)):
                try:
                    log_info(
                        f"[grillo_compactor] Running compaction cycle {i + 1}/{max(1, cycles)} (marker={marker}, dry_run={dry_run})"
                    )
                except Exception:
                    pass

                ok = await self._run_one_compaction_cycle(
                    dry_run=dry_run, marker=marker
                )
                results.append(ok)

                try:
                    if dry_run and isinstance(ok, dict):
                        log_info(
                            f"[grillo_compactor] Dry-run cycle {i + 1} results: {ok}"
                        )
                    else:
                        log_info(f"[grillo_compactor] Cycle {i + 1} completed: {ok}")
                except Exception:
                    pass

                # small sleep to avoid hammering DB/LLM when manually requested
                await asyncio.sleep(0.1)

            try:
                log_info(
                    f"[grillo_compactor] run_action completed: cycles={cycles}, dry_run={dry_run}, marker={marker}"
                )
            except Exception:
                pass

            return {
                "status": "ok",
                "cycles": cycles,
                "dry_run": dry_run,
                "results": results,
            }
        else:
            raise ValueError(f"Unsupported run_action: {action_type}")


# Expose plugin class for legacy loading
PLUGIN_CLASS = GrilloCompactorPlugin
