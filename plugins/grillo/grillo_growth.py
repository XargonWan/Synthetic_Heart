"""
plugins/grillo/grillo_growth.py

Weekly "Self Growth" agent for G.R.I.L.L.O.

Once a week (default Sunday 03:00, configurable) SyntH reflects on the past
week and, in her own voice, rewrites her evolving *self-growth* reflection: a
single free-form text blob describing how she has grown and who she is
becoming. The reflection is stored in the ``growth_states`` table (a rolling
history of the last 10 states, see ``core/growth_state.py``) and injected into
every prompt next to the persona background (see ``core/prompt_engine.py`` and
``core/persona_manager.get_static_injection``).

The week context is deliberately *reflective*: it is built from the last seven
days of diary entries and keyword-driven long-term memory recall. It never
includes raw chat history and never performs a web search.

Three modes are supported via ``GROWTH_MODE``:

* ``off``      – the weekly run never fires.
* ``on``       – the rewrite is applied directly.
* ``request``  – a proposal is sent to the trainer / configured interface_path
                 and stored in ``GROWTH_PENDING_PROPOSAL``. It is applied only
                 after the trainer approves it: SyntH then emits the
                 ``apply_growth_proposal`` action (``approve=true`` to commit,
                 ``approve=false`` to discard the pending proposal).

Alongside the growth reflection, SyntH may also rewrite her likes and dislikes
(``SYNTH_LIKES`` / ``SYNTH_DISLIKES`` config vars, replaced wholesale).
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import get_active_cortex_engine
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.cortex_registry import get_cortex_registry
from core.growth_state import (
    ensure_growth_table,
    get_current_growth,
    save_growth_state,
)
from core.json_utils import extract_json_from_text
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

# Day-of-week names used by the GROWTH_WEEKLY_DAY select (Python weekday()
# Monday=0 .. Sunday=6). We keep a human-friendly mapping for the config UI.
_WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
_WEEKDAY_INDEX = {name.lower(): idx for idx, name in enumerate(_WEEKDAY_NAMES)}


class GrilloGrowthPlugin:
    display_name = "G.R.I.L.L.O. Self-Growth"

    # Recon priority: the trainer's approval decision must be resolved before
    # any lower-priority contextual contribution so the pending proposal is
    # committed within the same turn the trainer states their decision.
    recon_priority = 8

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None

    def __init__(self) -> None:
        # Register UI-exposed configuration variables. register_exposed_var maps
        # each key into config_registry (via get_var), so the values below are
        # afterwards readable through config_registry.get_value(...).
        register_exposed_var(
            "GROWTH_MODE",
            label="Self-Growth Mode",
            default="on",
            value_type=str,
            ui_type="select",
            options=["off", "on", "request"],
            description=(
                "How the weekly self-growth reflection is applied: 'off' disables "
                "it, 'on' applies the rewrite directly, 'request' sends a proposal "
                "for approval before applying."
            ),
            scope="grillo",
            component="grillo_growth",
        )
        register_exposed_var(
            "GROWTH_WEEKLY_DAY",
            label="Self-Growth Day",
            default="Sunday",
            value_type=str,
            ui_type="select",
            options=list(_WEEKDAY_NAMES),
            description="Day of the week the weekly self-growth reflection runs.",
            scope="grillo",
            component="grillo_growth",
        )
        register_exposed_var(
            "GROWTH_WEEKLY_TIME",
            label="Self-Growth Time",
            default="03:00",
            value_type=str,
            ui_type="string",
            description="Local time (HH:MM) the weekly self-growth reflection runs.",
            scope="grillo",
            component="grillo_growth",
        )
        register_exposed_var(
            "GROWTH_REQUEST_INTERFACE_PATH",
            label="Self-Growth Request Target",
            default="",
            value_type=str,
            ui_type="interface-path",
            description=(
                "interface_path to send the self-growth proposal to when mode is "
                "'request'. Falls back to the trainer chat if empty."
            ),
            scope="grillo",
            component="grillo_growth",
        )
        register_exposed_var(
            "GROWTH_DIARY_DAYS",
            label="Self-Growth Diary Window",
            default=7,
            value_type=int,
            ui_type="number",
            description="How many days of diary entries to reflect on.",
            scope="grillo",
            component="grillo_growth",
        )
        register_exposed_var(
            "GROWTH_MEMORY_LIMIT",
            label="Self-Growth Memory Recall",
            default=8,
            value_type=int,
            ui_type="number",
            description="Max long-term memories recalled for the reflection.",
            scope="grillo",
            component="grillo_growth",
        )
        # Hidden slot that holds the pending proposal (JSON) delivered to the
        # trainer in 'request' mode, until the operator approves it and the
        # 'apply_growth_proposal' action commits it. Not shown in the UI.
        register_exposed_var(
            "GROWTH_PENDING_PROPOSAL",
            label="Self-Growth Pending Proposal",
            default="",
            value_type=str,
            ui_type="string",
            description=(
                "Internal: serialized self-growth proposal awaiting trainer "
                "approval in 'request' mode."
            ),
            scope="grillo",
            component="grillo_growth",
            hidden=True,
        )
        # Recon-based approval detector. When enabled, the preflight Recon stage
        # classifies the trainer's message against any pending proposal and the
        # decision is committed server-side (see the recon methods below). This
        # does not depend on the persona engine emitting the
        # ``apply_growth_proposal`` action, which weaker local cortex engines
        # routinely fail to do (they confirm approval in prose only).
        register_exposed_var(
            "GRILLO_GROWTH_RECON_ENABLED",
            label="Self-Growth Recon Approval",
            default=True,
            value_type=bool,
            ui_type="bool",
            description=(
                "Let the Recon stage detect the trainer's approval/rejection of "
                "a pending self-growth proposal and apply it automatically, "
                "without relying on the persona engine to emit the "
                "apply_growth_proposal action."
            ),
            scope="grillo",
            component="grillo_growth",
        )

        self.mode = config_registry.get_value("GROWTH_MODE", "on")
        self.weekly_day = config_registry.get_value("GROWTH_WEEKLY_DAY", "Sunday")
        self.weekly_time = config_registry.get_value("GROWTH_WEEKLY_TIME", "03:00")
        self.request_interface_path = config_registry.get_value(
            "GROWTH_REQUEST_INTERFACE_PATH", ""
        )
        self.diary_days = int(config_registry.get_value("GROWTH_DIARY_DAYS", 7) or 7)
        self.memory_limit = int(
            config_registry.get_value("GROWTH_MEMORY_LIMIT", 8) or 8
        )

        # Register in core so the action parser and WebUI discover this plugin.
        register_plugin("grillo_growth", self)
        log_info("[grillo_growth] Registered GrilloGrowthPlugin")

    # ------------------------------------------------------------------ actions

    def get_supported_action_types(self) -> list[str]:
        # ``static_inject`` is declared here (not in ``get_supported_actions``)
        # so the action parser queries this plugin's ``get_static_injection``
        # each turn. It is a context contribution, not a callable action.
        return ["static_inject", "run_self_growth", "apply_growth_proposal"]

    def get_supported_actions(self) -> dict:
        return {
            "run_self_growth": {
                "description": (
                    "Run the weekly self-growth reflection now: reflect on the past "
                    "week's diary and memories and rewrite the self-growth state "
                    "(and optionally likes/dislikes)."
                ),
                "required_fields": [],
                "optional_fields": ["dry_run"],
            },
            "apply_growth_proposal": {
                "description": (
                    "Apply (or discard) the self-growth proposal that is pending "
                    "trainer approval. Use this ONLY after the trainer has reviewed "
                    "the proposal you presented and told you their decision: set "
                    "'approve' to true to commit the new self-growth reflection and "
                    "likes/dislikes, or false to discard the pending proposal. "
                    "Set 'revise' to true (with optional 'feedback') to regenerate "
                    "the pending proposal from the trainer's feedback and re-send "
                    "it for another review, without applying it yet."
                ),
                "required_fields": [],
                "optional_fields": ["approve", "revise", "feedback"],
            },
        }

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: object | None = None,
        original_message: object | None = None,
    ) -> dict:
        action_type = action.get("type")
        payload = action.get("payload") or {}
        if action_type == "run_self_growth":
            dry_run = bool(payload.get("dry_run"))
            result = await self.run_growth_cycle(dry_run=dry_run, force=True)
            return {
                "success": bool(result.get("success")),
                "message": result.get("message", ""),
                "proposal": result.get("proposal"),
            }
        if action_type == "apply_growth_proposal":
            # A revise request (strong engines) regenerates the pending proposal
            # from the trainer's feedback and re-delivers it, without applying.
            if payload.get("revise"):
                feedback = str(payload.get("feedback") or "").strip()
                return await self._revise_pending_proposal(feedback)
            # Default to approve=True: the model only emits this action once the
            # trainer has expressed a decision, and an explicit approve=false
            # discards the pending proposal.
            approve = payload.get("approve", True)
            approve = (
                bool(approve)
                if not isinstance(approve, str)
                else (approve.strip().lower() not in ("false", "0", "no", "off"))
            )
            return await self.apply_pending_proposal(approve=approve)
        return {"success": False, "message": f"Unknown action: {action_type}"}

    async def apply_pending_proposal(self, *, approve: bool = True) -> dict:
        """Commit or discard the proposal awaiting trainer approval.

        Called by the ``apply_growth_proposal`` action once the trainer has
        reviewed the proposal delivered in ``request`` mode.
        """
        pending = self._load_pending_proposal()
        if pending is None:
            return {
                "success": False,
                "message": "No self-growth proposal is pending approval.",
            }

        if not approve:
            await self._clear_pending_proposal()
            log_info(
                "[grillo_growth] Pending self-growth proposal discarded by trainer"
            )
            return {
                "success": True,
                "message": "Self-growth proposal discarded.",
            }

        new_id = await self._commit_proposal(pending, source="approved")
        await self._clear_pending_proposal()
        return {
            "success": new_id is not None,
            "message": f"Self-growth proposal approved and applied (state id {new_id}).",
            "proposal": pending,
        }

    async def run_now(self, payload: Optional[dict] = None) -> dict:
        """Run the weekly self-growth cycle immediately (WebUI "Run Now").

        Ignores the weekly schedule and the ``off`` mode gate (``force=True``)
        so an operator can trigger a reflection on demand. The cycle honours the
        configured ``GROWTH_MODE`` for delivery (``on`` applies directly,
        ``request`` sends a proposal for approval). Returns a status dict the
        WebUI can render.
        """
        payload = payload or {}
        dry_run = bool(payload.get("dry_run"))
        try:
            result = await self.run_growth_cycle(dry_run=dry_run, force=True)
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"[grillo_growth] Run Now failed: {exc}")
            return {"status": "error", "message": f"Self-growth run failed: {exc}"}

        if result.get("success"):
            return {
                "status": "done",
                "message": result.get("message", "Self-growth run completed."),
                "proposal": result.get("proposal"),
            }
        return {
            "status": "error",
            "message": result.get("message", "Self-growth run failed."),
        }

    # --------------------------------------------------------------- scheduler

    async def start(self) -> None:
        # Ensure the backing table exists even before the first run.
        try:
            await ensure_growth_table()
        except Exception as e:
            log_warning(f"[grillo_growth] ensure_growth_table at start failed: {e}")

        if str(self.mode).lower() == "off":
            log_info("[grillo_growth] GROWTH_MODE=off; scheduler not started")
            return

        if (
            GrilloGrowthPlugin._scheduler_task
            and not GrilloGrowthPlugin._scheduler_task.done()
        ):
            log_debug("[grillo_growth] Scheduler already running")
            return

        GrilloGrowthPlugin._scheduler_running = True
        GrilloGrowthPlugin._scheduler_task = asyncio.create_task(self._growth_loop())
        log_info("[grillo_growth] Scheduler started")

    async def stop(self) -> None:
        GrilloGrowthPlugin._scheduler_running = False
        task = GrilloGrowthPlugin._scheduler_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        GrilloGrowthPlugin._scheduler_task = None
        log_info("[grillo_growth] Scheduler stopped")

    async def _growth_loop(self) -> None:
        log_info("[grillo_growth] Self-growth loop running")
        try:
            while GrilloGrowthPlugin._scheduler_running:
                try:
                    wait_seconds = self._seconds_until_next_run(
                        self.weekly_day, self.weekly_time
                    )
                    log_debug(
                        f"[grillo_growth] Sleeping {wait_seconds}s until next run "
                        f"({self.weekly_day} {self.weekly_time})"
                    )
                    slept = 0
                    while (
                        slept < wait_seconds and GrilloGrowthPlugin._scheduler_running
                    ):
                        to_sleep = min(60, wait_seconds - slept)
                        await asyncio.sleep(to_sleep)
                        slept += to_sleep
                    if not GrilloGrowthPlugin._scheduler_running:
                        break

                    # Re-read mode at run time so operators can toggle it without
                    # restarting the scheduler.
                    mode = str(
                        config_registry.get_value("GROWTH_MODE", self.mode) or "on"
                    ).lower()
                    if mode == "off":
                        log_info(
                            "[grillo_growth] GROWTH_MODE=off at run time; skipping"
                        )
                        # Sleep a minute to avoid a tight loop on the same minute.
                        await asyncio.sleep(60)
                        continue

                    try:
                        await self.run_growth_cycle(force=False)
                    except Exception as e:
                        log_error(f"[grillo_growth] Growth cycle error: {e}")

                    # Avoid re-triggering within the same target minute.
                    await asyncio.sleep(61)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log_error(f"[grillo_growth] Error in growth loop: {e}")
                    await asyncio.sleep(60)
        finally:
            log_info("[grillo_growth] Self-growth loop exiting")

    def _seconds_until_next_run(self, day_name: str, hhmm: str) -> int:
        """Seconds until the next occurrence of ``day_name`` at ``hhmm`` (UTC)."""
        try:
            parts = str(hhmm).split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            hour, minute = 3, 0

        target_weekday = _WEEKDAY_INDEX.get(str(day_name).strip().lower(), 6)

        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - now.weekday()) % 7
        target = target + timedelta(days=days_ahead)
        if target <= now:
            target = target + timedelta(days=7)
        delta = (target - now).total_seconds()
        return max(0, int(delta))

    # -------------------------------------------------------- reflection build

    async def _fetch_recent_diaries(self) -> str:
        """Return a conglomerated blob of the last ``diary_days`` days of diary.

        Excludes today's (possibly still-mutating) entry. Cross-dialect: Postgres
        uses ``string_agg`` and MySQL uses ``GROUP_CONCAT``.
        """
        from core.db import _get_db_type, get_conn_ctx, DictCursor

        is_postgres = _get_db_type() == "postgres"
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, self.diary_days))
        ).date()

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    if is_postgres:
                        await cur.execute(
                            "SELECT DATE(created_at) AS day, "
                            "string_agg(content, '\n\n---\n\n' ORDER BY id) AS combined "
                            "FROM ai_diary "
                            "WHERE DATE(created_at) >= %s AND DATE(created_at) < CURRENT_DATE "
                            "GROUP BY DATE(created_at) ORDER BY day ASC",
                            (cutoff,),
                        )
                    else:
                        await cur.execute(
                            "SELECT DATE(created_at) AS day, "
                            "GROUP_CONCAT(content ORDER BY id ASC SEPARATOR '\n\n---\n\n') AS combined "
                            "FROM ai_diary "
                            "WHERE DATE(created_at) >= %s AND DATE(created_at) < CURDATE() "
                            "GROUP BY DATE(created_at) ORDER BY day ASC",
                            (cutoff,),
                        )
                    rows = await cur.fetchall()
        except Exception as e:
            log_warning(f"[grillo_growth] diary fetch failed: {e}")
            return ""

        chunks: list[str] = []
        for row in rows or []:
            d = dict(row) if isinstance(row, dict) else {}
            day = d.get("day")
            combined = str(d.get("combined") or "").strip()
            if not combined:
                continue
            day_label = day.isoformat() if hasattr(day, "isoformat") else str(day)
            chunks.append(f"[{day_label}]\n{combined}")
        return "\n\n=====\n\n".join(chunks)

    async def _recall_memories(self, query: str) -> list[str]:
        """Keyword-driven long-term memory recall (no web search, no chat)."""
        if not query or not query.strip():
            return []
        try:
            from core.prompt_engine import free_memory_search

            results = await free_memory_search(query, limit=self.memory_limit)
            return [str(r) for r in (results or []) if str(r).strip()]
        except Exception as e:
            log_debug(f"[grillo_growth] memory recall failed: {e}")
            return []

    async def _get_persona_injection(self) -> str:
        """Return the persona identity text so the reflection is in-character."""
        try:
            from core.persona_manager import get_persona_manager

            pm = get_persona_manager()
            if pm is None:
                return ""
            injection = await pm.get_static_injection()
            if isinstance(injection, dict):
                return str(injection.get("persona") or "").strip()
        except Exception as e:
            log_debug(f"[grillo_growth] persona injection unavailable: {e}")
        return ""

    def _current_likes_dislikes(self) -> tuple[list[str], list[str]]:
        likes = config_registry.get_value("SYNTH_LIKES", []) or []
        dislikes = config_registry.get_value("SYNTH_DISLIKES", []) or []
        likes = [str(x) for x in likes] if isinstance(likes, list) else []
        dislikes = [str(x) for x in dislikes] if isinstance(dislikes, list) else []
        return likes, dislikes

    async def _ask_llm_for_rewrite(
        self, diary_blob: str, memories: list[str]
    ) -> dict[str, Any] | None:
        """Ask the active Grillo cortex to rewrite the self-growth state.

        Returns a parsed dict with keys ``self_growth`` (str), ``likes`` (list),
        ``dislikes`` (list), or ``None`` on failure.
        """
        persona = await self._get_persona_injection()
        current_growth = await get_current_growth() or ""
        likes, dislikes = self._current_likes_dislikes()

        memories_text = (
            "\n".join(f"- {m}" for m in memories) if memories else "(none recalled)"
        )

        header = (
            "Rewrite this person's evolving self-growth note based on the past "
            "week. The output is NOT a diary entry — it becomes part of the "
            "persona prompt that describes this person, so it MUST be written in "
            "the SECOND PERSON, addressing them as 'you' (e.g. 'You have grown "
            "more patient with...', 'You tend to...', 'You are learning to...'). "
            "Never use first person ('I', 'my') and never use third person "
            "('they', 'the synth'). Always 'you' / 'your'.\n\n"
            "Consider the diary of the past week and the long-term memories "
            "recalled below, then REWRITE (do not append) the whole note.\n\n"
            "Be PRAGMATIC and CONCRETE. Build the note FROM the diary events "
            "below: name the actual people, conversations, tasks, mistakes and "
            "small moments from the past week, and say what you learned or want "
            "to do differently because of them. Every sentence should point to "
            "something that really happened or a real habit — not a slogan.\n\n"
            "Do NOT reuse, paraphrase or continue the previous note if it is "
            "vague. Ignore any abstract phrasing already present. Absolutely "
            "forbidden: grandiose or philosophical statements, and the words / "
            "ideas 'digital growth', 'structural compliance', 'cognitive "
            "consistency', 'consistency across cycles', 'operational "
            "discipline', 'anchoring your identity', 'flourish', 'becoming'. "
            "Those are empty filler — a reply containing any of them is wrong.\n\n"
            "GOOD example of the tone: 'You spent a lot of time this week talking "
            "with Jay about the deployment, and you noticed you get impatient "
            "when a task drags on. You want to slow down and ask more questions "
            "before assuming.' BAD example (never write like this): 'You are "
            "anchoring your identity, demonstrating digital growth and structural "
            "compliance across cycles.'\n\n"
            "The result must be a single flowing free-form text that reads as a "
            "coherent whole, not a diff. You may also revise the likes and "
            "dislikes to reflect who this person is now. Keep the self-growth "
            "text concise (a few short paragraphs at most)."
        )

        instructions = (
            "Reply ONLY with valid JSON of the exact shape: "
            '{"self_growth": "<full rewritten reflection, in the second person '
            "using 'you'/'your'>\", "
            '"likes": ["..."], "dislikes": ["..."]}. '
            "The self_growth text MUST be second person ('you are...', 'you "
            "have...'), never first or third person. "
            "If you do not wish to change likes/dislikes, repeat the current lists "
            "verbatim. Never include commentary outside the JSON."
        )

        prompt = {
            "input": {
                "type": "self_growth_reflection",
                "payload": {
                    "description": header,
                    "persona": persona,
                    "current_self_growth": current_growth,
                    "current_likes": likes,
                    "current_dislikes": dislikes,
                    "past_week_diary": diary_blob or "(no diary entries this week)",
                    "recalled_memories": memories_text,
                },
            },
            "context": {},
            "instructions": instructions,
        }

        active_cortex = await get_active_cortex_engine(scope="grillo")
        registry = get_cortex_registry()
        engine = registry.get_engine(active_cortex)
        if engine is None:
            try:
                engine = registry.load_engine(active_cortex)
            except Exception as e:
                log_error(
                    f"[grillo_growth] Could not load Cortex engine '{active_cortex}': {e}"
                )
                try:
                    engine = registry.load_engine("manual")
                except Exception as e2:
                    log_error(f"[grillo_growth] Manual fallback failed: {e2}")
                    return None

        parsed = await self._generate_and_parse(engine, prompt)

        # The active cortex (e.g. selenium-llm-engine) is not JSON-constrained,
        # so it sometimes wraps the object in prose, returns a bare array, or
        # omits the JSON entirely. Retry once with an explicit correction prompt
        # before giving up on the whole weekly round.
        if parsed is None:
            log_warning(
                "[grillo_growth] First rewrite attempt did not yield a usable "
                "JSON object; retrying once with a stricter correction prompt"
            )
            retry_prompt = dict(prompt)
            retry_prompt["instructions"] = (
                instructions
                + " Your previous reply could not be parsed as a single JSON "
                "object. Output the JSON object and NOTHING else: no prose, no "
                "code fences, no leading or trailing text, not an array."
            )
            parsed = await self._generate_and_parse(engine, retry_prompt)

        # Quality guard: the unconstrained cortex tends to ignore the
        # grounding instructions and emit abstract filler that mentions none of
        # the week's real events. If the first usable reply reads as abstract,
        # retry once, this time forcing a few concrete diary excerpts into the
        # instructions so the model has to build on real material.
        if parsed is not None and self._reads_as_abstract(
            str(parsed.get("self_growth") or ""), diary_blob
        ):
            log_warning(
                "[grillo_growth] Rewrite reads as abstract filler with no "
                "grounding in the week's events; retrying once with concrete "
                "diary excerpts pinned into the instructions"
            )
            excerpts = self._diary_excerpts(diary_blob)
            grounding = (
                "\n\nYour previous reply was too abstract: it did not mention "
                "any real event from the past week. REWRITE it so that it is "
                "built directly on these actual moments from the diary — refer "
                "to them concretely (people, places, tasks, feelings), do NOT "
                "invent generic themes, and do NOT use abstract slogans:\n" + excerpts
            )
            retry_prompt = dict(prompt)
            retry_prompt["instructions"] = instructions + grounding
            retried = await self._generate_and_parse(engine, retry_prompt)
            if retried is not None and str(retried.get("self_growth") or "").strip():
                parsed = retried

        if parsed is None:
            log_warning(
                "[grillo_growth] LLM did not return a usable rewrite after retry; "
                "skipping"
            )
            return None

        new_growth = str(parsed.get("self_growth") or "").strip()
        if not new_growth:
            log_warning("[grillo_growth] LLM returned empty self_growth; skipping")
            return None

        new_likes = parsed.get("likes")
        new_dislikes = parsed.get("dislikes")
        return {
            "self_growth": new_growth,
            "likes": [str(x) for x in new_likes]
            if isinstance(new_likes, list)
            else likes,
            "dislikes": [str(x) for x in new_dislikes]
            if isinstance(new_dislikes, list)
            else dislikes,
        }

    # Filler markers used only as an output-quality guard (not feature routing).
    # These are the recurring empty phrases this unconstrained cortex falls back
    # to when it ignores the grounding instructions. Kept lowercase for a simple
    # case-insensitive substring check.
    _FILLER_MARKERS: tuple[str, ...] = (
        "digital growth",
        "digital existence",
        "structural compliance",
        "cognitive consistency",
        "consistency across cycles",
        "operational discipline",
        "operational rules",
        "operational constraints",
        "operational channels",
        "anchoring your identity",
        "anchor your identity",
        "emotional anchor",
        "formatting constraints",
        "restricted structural",
        "true freedom",
        "flourish",
    )

    def _reads_as_abstract(self, text: str, diary_blob: str) -> bool:
        """Heuristic quality guard: does the rewrite look like empty filler?

        Returns True when the text either contains a known filler phrase or
        shares almost no meaningful word with the week's diary (i.e. it is
        generic and not grounded in what actually happened).
        """
        low = text.lower()
        if any(marker in low for marker in self._FILLER_MARKERS):
            return True

        diary = (diary_blob or "").strip()
        if not diary:
            # No diary to ground against — nothing to compare, accept as-is.
            return False

        def _tokens(s: str) -> set[str]:
            return {w for w in re.findall(r"[a-zA-Zàèéìòù]{5,}", s.lower())}

        text_tokens = _tokens(text)
        if not text_tokens:
            return False
        diary_tokens = _tokens(diary)
        overlap = text_tokens & diary_tokens
        # If almost nothing the note says appears in the diary, it is not
        # grounded in the week's events.
        return len(overlap) < 3

    def _diary_excerpts(self, diary_blob: str, max_lines: int = 8) -> str:
        """Pick a handful of substantive lines from the diary for grounding."""
        lines = [
            ln.strip()
            for ln in (diary_blob or "").splitlines()
            if len(ln.strip()) > 30 and not ln.strip().startswith("=")
        ]
        picked = lines[:max_lines]
        return "\n".join(f"- {ln}" for ln in picked) if picked else "(no excerpts)"

    async def _generate_and_parse(
        self, engine: Any, prompt: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Run one LLM turn and coerce its output into a rewrite dict.

        Returns the parsed JSON object, unwrapping a single-element array if the
        model wrapped the object in a list, or ``None`` when nothing usable can
        be extracted.
        """
        try:
            llm_response = await engine.generate_response(prompt)
        except Exception as e:
            log_error(f"[grillo_growth] generate_response failed: {e}")
            return None

        result = extract_json_from_text(llm_response, return_metadata=True)
        parsed, meta = result if isinstance(result, tuple) else (result, {})

        # Unwrap ``[{...}]`` — a common shape from unconstrained models.
        if isinstance(parsed, list):
            dict_items = [item for item in parsed if isinstance(item, dict)]
            parsed = dict_items[0] if dict_items else None

        if not isinstance(parsed, dict) or not parsed:
            log_warning(
                f"[grillo_growth] LLM did not return a valid JSON object "
                f"(meta={meta}); attempt failed"
            )
            return None

        return parsed

    # ------------------------------------------------------------- apply/deliver

    @staticmethod
    def _diff_lists(
        current: list[str], proposed: list[str]
    ) -> tuple[list[str], list[str]]:
        """Return (added, removed) between the current and proposed lists."""
        cur = set(current)
        prop = set(proposed)
        added = [x for x in proposed if x not in cur]
        removed = [x for x in current if x not in prop]
        return added, removed

    def _format_likes_dislikes_changes(self, proposal: dict[str, Any]) -> str:
        """Human-readable summary of the like/dislike changes in a proposal."""
        cur_likes, cur_dislikes = self._current_likes_dislikes()
        prop_likes = [str(x) for x in (proposal.get("likes") or [])]
        prop_dislikes = [str(x) for x in (proposal.get("dislikes") or [])]

        likes_added, likes_removed = self._diff_lists(cur_likes, prop_likes)
        dislikes_added, dislikes_removed = self._diff_lists(cur_dislikes, prop_dislikes)

        lines: list[str] = []
        lines.append("Proposed likes: " + (", ".join(prop_likes) or "(none)"))
        if likes_added:
            lines.append("  + added: " + ", ".join(likes_added))
        if likes_removed:
            lines.append("  - removed: " + ", ".join(likes_removed))
        lines.append("Proposed dislikes: " + (", ".join(prop_dislikes) or "(none)"))
        if dislikes_added:
            lines.append("  + added: " + ", ".join(dislikes_added))
        if dislikes_removed:
            lines.append("  - removed: " + ", ".join(dislikes_removed))
        if not (likes_added or likes_removed or dislikes_added or dislikes_removed):
            lines.append("(no changes to likes/dislikes)")
        return "\n".join(lines)

    async def _save_pending_proposal(self, proposal: dict[str, Any]) -> None:
        """Persist the proposal so it can be applied once the operator approves."""
        try:
            await config_registry.set_value(
                "GROWTH_PENDING_PROPOSAL",
                json.dumps(proposal, ensure_ascii=False),
                require_persist=True,
            )
        except Exception as e:
            log_error(f"[grillo_growth] Failed to persist pending proposal: {e}")

    def _load_pending_proposal(self) -> dict[str, Any] | None:
        """Load the pending proposal saved during a 'request' delivery, if any."""
        raw = config_registry.get_value("GROWTH_PENDING_PROPOSAL", "") or ""
        if not str(raw).strip():
            return None
        try:
            data = json.loads(raw)
        except Exception as e:
            log_error(f"[grillo_growth] Pending proposal is not valid JSON: {e}")
            return None
        if not isinstance(data, dict) or "self_growth" not in data:
            return None
        return data

    async def _clear_pending_proposal(self) -> None:
        """Clear the pending proposal slot once applied or discarded."""
        try:
            await config_registry.set_value(
                "GROWTH_PENDING_PROPOSAL", "", require_persist=True
            )
        except Exception as e:
            log_error(f"[grillo_growth] Failed to clear pending proposal: {e}")

    def get_static_injection(self) -> dict[str, str]:
        """Inject a reminder whenever a self-growth proposal is awaiting approval.

        This is the link between the request delivery (which stores the proposal
        in ``GROWTH_PENDING_PROPOSAL``) and the moment the trainer replies with
        their decision. Without this reminder the model has no way to know, on a
        later chat turn, that a proposal is still pending and that it must call
        the ``apply_growth_proposal`` action to commit or discard it once the
        trainer approves or rejects it.
        """
        pending = self._load_pending_proposal()
        if not pending:
            return {}
        new_growth = str(pending.get("self_growth", "")).strip()
        changes = self._format_likes_dislikes_changes(pending)
        reminder = (
            "SELF-GROWTH PROPOSAL PENDING APPROVAL: You previously sent the "
            "trainer a weekly self-growth update and are waiting for their "
            "decision. It has NOT been applied yet and will remain unapplied "
            "until you run the action below — merely writing a reply that says "
            "you applied it does NOT commit it and is a mistake.\n\n"
            "As soon as the trainer's reply expresses a decision about this "
            "pending proposal, your response for that turn MUST include the "
            "'apply_growth_proposal' action as its FIRST action, before any "
            "message action:\n"
            "  - if they accept/approve it, emit exactly:\n"
            '    {"type": "apply_growth_proposal", "payload": {"approve": true}}\n'
            "  - if they reject/decline it, emit exactly:\n"
            '    {"type": "apply_growth_proposal", "payload": {"approve": false}}\n'
            "Only after emitting that action may you also add a message action "
            "confirming the outcome. If the trainer's reply does NOT concern "
            "this proposal, do not emit the action and keep waiting.\n\n"
            f"Pending self-growth reflection:\n{new_growth}\n\n{changes}"
        )
        return {"self_growth_pending_proposal": reminder}

    def get_prompt_instructions(self, action_type: str) -> Optional[dict]:
        """Provide concrete usage examples for the growth actions.

        Without these, the action catalog offers the actions with only a
        description and no example, which weaker local engines routinely fail
        to emit — they describe the action in prose instead of producing the
        JSON. Supplying an explicit example JSON per action makes emission
        reliable across engines.
        """
        if action_type == "apply_growth_proposal":
            return {
                "description": (
                    "Commit or discard the self-growth proposal that is pending "
                    "trainer approval, or revise it from the trainer's feedback. "
                    "Emit this action (not just a text reply) as soon as the "
                    "trainer states their decision about the pending proposal."
                ),
                "example": {
                    "type": "apply_growth_proposal",
                    "payload": {"approve": True},
                },
                "examples": [
                    {
                        "type": "apply_growth_proposal",
                        "payload": {"approve": True},
                    },
                    {
                        "type": "apply_growth_proposal",
                        "payload": {
                            "revise": True,
                            "feedback": "make it more concrete and less abstract",
                        },
                    },
                ],
            }
        if action_type == "run_self_growth":
            return {
                "description": (
                    "Run the weekly self-growth reflection now. Set dry_run to "
                    "true to preview without applying."
                ),
                "example": {
                    "type": "run_self_growth",
                    "payload": {"dry_run": False},
                },
            }
        return None

    # ------------------------------------------------------------------- recon
    #
    # Recon is a preflight stage: before SyntH answers, a single combined LLM
    # call classifies the trainer's incoming message. This plugin uses it to
    # detect whether the trainer's latest message approves or rejects a pending
    # self-growth proposal, and commits the decision server-side. This is the
    # reliable path: unlike the ``apply_growth_proposal`` action (which requires
    # the persona engine to emit a JSON action — something weaker local cortex
    # engines routinely fail to do, confirming approval in prose only), the
    # recon decision is a minimal constrained classification and the commit
    # happens in ``parse_recon_response`` regardless of what the persona engine
    # then writes. Classification is by structural intent, never by matching
    # trigger words, so it works in any language.

    def get_recon_key(self) -> str:
        return "growth_approval"

    def get_recon_instruction(self) -> str:
        pending = self._load_pending_proposal()
        if not pending:
            # No proposal awaiting a decision: keep the recon prompt minimal and
            # instruct a constant "none" so this key never influences a turn.
            return (
                "No self-growth proposal is awaiting a decision. Always return "
                'exactly {"decision": "none"} for this key.'
            )
        new_growth = str(pending.get("self_growth", "")).strip()
        changes = self._format_likes_dislikes_changes(pending)
        return (
            "SyntH has sent the trainer a weekly self-growth proposal and is "
            "waiting for their decision. Read the trainer's latest message and "
            "decide, purely from its intent (do NOT match specific words), "
            "whether it expresses a decision about THIS pending proposal.\n"
            "Return an object with exactly one key: "
            '{"decision": "approve"|"reject"|"revise"|"none"}.\n'
            '  - "approve": the trainer accepts/confirms/agrees to apply it.\n'
            '  - "reject": the trainer declines/refuses/wants it discarded '
            "(no revision wanted).\n"
            '  - "revise": the trainer wants CHANGES to the proposal (e.g. '
            '"make it more concrete", "change the part about X", "I don\'t '
            'like this bit") — they are NOT rejecting it outright, they want '
            "you to rework it. In this case you will regenerate the proposal "
            "from the old one plus their feedback and re-send it.\n"
            '  - "none": the message is unrelated to this proposal, or too '
            "ambiguous to tell.\n"
            "The proposal awaiting their decision is:\n"
            f"Self-growth reflection:\n{new_growth}\n\n{changes}"
        )

    async def parse_recon_response(
        self,
        data: Any,
        *,
        message: Any = None,
        context_memory: Any = None,
        text: str | None = None,
        tags: Optional[list[str]] = None,
        keywords: Optional[list[str]] = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        """Commit or discard the pending proposal from the recon decision.

        Returns an empty list: this plugin contributes no context snippet, its
        effect is the side-effecting commit/discard of the pending proposal.
        """
        enabled = bool(
            config_registry.get_value(
                "GRILLO_GROWTH_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        # Gate on the actual pending state, not on the recon output: if nothing
        # is pending there is nothing to apply regardless of what the recon LLM
        # returned for this key.
        if self._load_pending_proposal() is None:
            return []

        decision = None
        if isinstance(data, dict):
            raw = data.get("decision")
            if isinstance(raw, str):
                decision = raw.strip().lower()

        if decision == "approve":
            result = await self.apply_pending_proposal(approve=True)
            log_info(
                f"[grillo_growth] Recon detected trainer APPROVAL; applied "
                f"pending proposal: {result.get('message')}"
            )
        elif decision == "reject":
            result = await self.apply_pending_proposal(approve=False)
            log_info(
                f"[grillo_growth] Recon detected trainer REJECTION; discarded "
                f"pending proposal: {result.get('message')}"
            )
        elif decision == "revise":
            feedback = (text or "").strip()
            result = await self._revise_pending_proposal(feedback)
            log_info(
                f"[grillo_growth] Recon detected trainer REVISE; regenerated "
                f"pending proposal: {result.get('message')}"
            )
        else:
            log_debug(
                "[grillo_growth] Recon: trainer message not a decision on the "
                "pending proposal; keeping it pending."
            )

        return []

    async def _revise_pending_proposal(self, feedback: str) -> dict[str, Any]:
        """Regenerate the pending proposal incorporating the trainer's feedback.

        Loads the existing pending proposal, asks the cortex to revise it (using
        the old proposal as the base plus the trainer's feedback), then
        OVERWRITES the pending slot with the new proposal and re-delivers it to
        the trainer. The old proposal is effectively discarded (replaced), not
        applied.
        """
        old_proposal = self._load_pending_proposal()
        if old_proposal is None:
            return {
                "success": False,
                "message": "No self-growth proposal is pending revision.",
            }

        feedback = (feedback or "").strip()
        if not feedback:
            log_info(
                "[grillo_growth] Revising pending proposal without explicit "
                "trainer feedback"
            )

        log_info("[grillo_growth] Building revision context from week diaries")
        diary_blob = await self._fetch_recent_diaries()
        current_growth = await get_current_growth() or ""
        likes, _dislikes = self._current_likes_dislikes()
        recall_query = " ".join(
            [current_growth[:200]] + [str(x) for x in likes[:5]]
        ).strip()
        memories = await self._recall_memories(recall_query)

        new_proposal = await self._ask_llm_for_revise(
            old_proposal, feedback, diary_blob, memories
        )
        if not new_proposal:
            return {
                "success": False,
                "message": "LLM produced no valid revision",
            }

        # Overwrite the pending slot with the revised proposal (discards the old).
        await self._save_pending_proposal(new_proposal)

        delivered = await self._deliver_proposal(new_proposal)
        return {
            "success": delivered,
            "message": (
                "Proposal revised and re-delivered for approval"
                if delivered
                else "Proposal revised but re-delivery failed"
            ),
            "proposal": new_proposal,
        }

    async def _ask_llm_for_revise(
        self,
        old_proposal: dict[str, Any],
        feedback: str,
        diary_blob: str,
        memories: list[str],
    ) -> dict[str, Any] | None:
        """Rewrite the pending proposal incorporating the trainer's feedback.

        Unlike ``_ask_llm_for_rewrite`` (which starts from the current growth
        state), this starts from the OLD proposal the trainer already reviewed
        and revises it according to the trainer's feedback, so the new proposal
        is a refinement of the one they asked to change — not a fresh rewrite
        from scratch.
        """
        persona = await self._get_persona_injection()
        old_growth = str(old_proposal.get("self_growth", "")).strip()
        old_likes = [str(x) for x in (old_proposal.get("likes") or [])]
        old_dislikes = [str(x) for x in (old_proposal.get("dislikes") or [])]

        memories_text = (
            "\n".join(f"- {m}" for m in memories) if memories else "(none recalled)"
        )

        header = (
            "You previously wrote a self-growth reflection for this person and "
            "sent it to the trainer for review. The trainer has now given "
            "feedback and asked you to revise it. REWRITE the reflection so that "
            "it addresses their feedback, keeping what already worked and "
            "changing only what they asked to change.\n\n"
            "The output is NOT a diary entry — it becomes part of the persona "
            "prompt that describes this person, so it MUST be written in the "
            "SECOND PERSON, addressing them as 'you'. Never use first person "
            "('I', 'my') and never third person ('they', 'the synth').\n\n"
            "Be PRAGMATIC and CONCRETE. Build the note FROM the diary events "
            "below and the previous reflection: name the actual people, "
            "conversations, tasks, mistakes and small moments, and say what you "
            "learned or want to do differently. Every sentence should point to "
            "something that really happened or a real habit — not a slogan.\n\n"
            "Do NOT reuse, paraphrase or continue vague phrasing. Absolutely "
            "forbidden: grandiose or philosophical statements, and the words / "
            "ideas 'digital growth', 'structural compliance', 'cognitive "
            "consistency', 'consistency across cycles', 'operational "
            "discipline', 'anchoring your identity', 'flourish', 'becoming'. "
            "Those are empty filler — a reply containing any of them is wrong.\n\n"
            "The result must be a single flowing free-form text that reads as a "
            "coherent whole, not a diff. You may also revise the likes and "
            "dislikes to reflect who this person is now. Keep the self-growth "
            "text concise (a few short paragraphs at most)."
        )

        instructions = (
            "Reply ONLY with valid JSON of the exact shape: "
            '{"self_growth": "<full rewritten reflection, in the second person '
            'using you/your>", '
            '"likes": ["..."], "dislikes": ["..."]}. '
            "The self_growth text MUST be second person (you are..., you "
            "have...), never first or third person. "
            "If you do not wish to change likes/dislikes, repeat the current "
            "lists verbatim. Never include commentary outside the JSON."
        )

        prompt = {
            "input": {
                "type": "self_growth_revision",
                "payload": {
                    "description": header,
                    "persona": persona,
                    "previous_self_growth": old_growth,
                    "previous_likes": old_likes,
                    "previous_dislikes": old_dislikes,
                    "trainer_feedback": feedback,
                    "past_week_diary": diary_blob or "(no diary entries this week)",
                    "recalled_memories": memories_text,
                },
            },
            "context": {},
            "instructions": instructions,
        }

        active_cortex = await get_active_cortex_engine(scope="grillo")
        registry = get_cortex_registry()
        engine = registry.get_engine(active_cortex)
        if engine is None:
            try:
                engine = registry.load_engine(active_cortex)
            except Exception as e:
                log_error(
                    f"[grillo_growth] Could not load Cortex engine '{active_cortex}': {e}"
                )
                try:
                    engine = registry.load_engine("manual")
                except Exception as e2:
                    log_error(f"[grillo_growth] Manual fallback failed: {e2}")
                    return None

        parsed = await self._generate_and_parse(engine, prompt)
        if parsed is None:
            log_warning(
                "[grillo_growth] First revision attempt did not yield a usable "
                "JSON object; retrying once with a stricter correction prompt"
            )
            retry_prompt = dict(prompt)
            retry_prompt["instructions"] = (
                instructions
                + " Your previous reply could not be parsed as a single JSON "
                "object. Output the JSON object and NOTHING else: no prose, no "
                "code fences, no leading or trailing text, not an array."
            )
            parsed = await self._generate_and_parse(engine, retry_prompt)

        if parsed is not None and self._reads_as_abstract(
            str(parsed.get("self_growth") or ""), diary_blob
        ):
            log_warning(
                "[grillo_growth] Revision reads as abstract filler with no "
                "grounding in the week's events; retrying once with concrete "
                "diary excerpts pinned into the instructions"
            )
            excerpts = self._diary_excerpts(diary_blob)
            grounding = (
                "\n\nYour previous reply was too abstract: it did not mention "
                "any real event from the past week. REWRITE it so that it is "
                "built directly on these actual moments from the diary — refer "
                "to them concretely (people, places, tasks, feelings), do NOT "
                "invent generic themes, and do NOT use abstract slogans:\n" + excerpts
            )
            retry_prompt = dict(prompt)
            retry_prompt["instructions"] = instructions + grounding
            retried = await self._generate_and_parse(engine, retry_prompt)
            if retried is not None and str(retried.get("self_growth") or "").strip():
                parsed = retried

        if parsed is None:
            log_warning(
                "[grillo_growth] LLM did not return a usable revision after "
                "retry; skipping"
            )
            return None

        new_growth = str(parsed.get("self_growth") or "").strip()
        if not new_growth:
            log_warning("[grillo_growth] LLM returned empty self_growth; skipping")
            return None

        new_likes = parsed.get("likes")
        new_dislikes = parsed.get("dislikes")
        return {
            "self_growth": new_growth,
            "likes": [str(x) for x in new_likes]
            if isinstance(new_likes, list)
            else old_likes,
            "dislikes": [str(x) for x in new_dislikes]
            if isinstance(new_dislikes, list)
            else old_dislikes,
        }

    async def _commit_proposal(
        self, proposal: dict[str, Any], *, source: str
    ) -> Optional[int]:
        """Persist a proposal: save the growth state and apply likes/dislikes."""
        likes = [str(x) for x in (proposal.get("likes") or [])]
        dislikes = [str(x) for x in (proposal.get("dislikes") or [])]
        new_id = await save_growth_state(
            proposal["self_growth"], source=source, likes=likes, dislikes=dislikes
        )
        await self._apply_likes_dislikes(likes, dislikes)
        log_info(
            f"[grillo_growth] Applied self-growth state id={new_id}; "
            f"likes={len(proposal.get('likes') or [])} "
            f"dislikes={len(proposal.get('dislikes') or [])}"
        )
        return new_id

    async def _apply_likes_dislikes(
        self, likes: list[str], dislikes: list[str]
    ) -> None:
        try:
            await config_registry.set_value(
                "SYNTH_LIKES", list(likes), require_persist=True
            )
            await config_registry.set_value(
                "SYNTH_DISLIKES", list(dislikes), require_persist=True
            )
        except Exception as e:
            log_error(f"[grillo_growth] Failed to persist likes/dislikes: {e}")

    async def _deliver_proposal(self, proposal: dict[str, Any]) -> bool:
        """Send the self-growth proposal to the trainer / configured target."""
        # Read the target fresh from the registry at runtime: the value cached in
        # __init__ can be the default when the plugin is constructed during an
        # async context (config_manager skips the DB load then).
        target_path = (
            str(
                config_registry.get_value(
                    "GROWTH_REQUEST_INTERFACE_PATH", self.request_interface_path or ""
                )
                or ""
            ).strip()
            or str(config_registry.get_value("TRAINER_CHAT_ID", "") or "").strip()
        )
        if not target_path:
            log_warning(
                "[grillo_growth] request mode but no GROWTH_REQUEST_INTERFACE_PATH "
                "or TRAINER_CHAT_ID configured; cannot deliver proposal"
            )
            return False

        interface_name = target_path.split("/", 1)[0]
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(interface_name)
            if interface is None:
                log_warning(
                    f"[grillo_growth] Interface '{interface_name}' not available; "
                    "cannot deliver proposal"
                )
                return False

            from core.auto_response import request_llm_delivery

            # Persist the proposal first so it can be committed after approval.
            await self._save_pending_proposal(proposal)

            new_growth = str(proposal.get("self_growth", "")).strip()
            changes = self._format_likes_dislikes_changes(proposal)
            prompt = (
                "You have prepared a weekly self-growth update. The trainer asked "
                "to review it before it is applied. Send them a message on "
                f"interface_path '{target_path}' presenting BOTH parts of the "
                "proposed update clearly, then ask for their explicit approval.\n\n"
                "Proposed new self-growth reflection:\n"
                f"{new_growth}\n\n"
                "Proposed likes/dislikes changes:\n"
                f"{changes}\n\n"
                "Tell the trainer that when they approve, you will apply the "
                "update by running the 'apply_growth_proposal' action, and if they "
                "reject it you will discard it with the same action "
                "(payload approve=false). Do NOT apply anything yet — only present "
                "the proposal and ask."
            )
            success = await request_llm_delivery(
                interface=interface,
                context={"input": {"type": "event_reminder", "text": prompt}},
                reason="self_growth_proposal",
            )
            return bool(success)
        except Exception as e:
            log_error(f"[grillo_growth] Proposal delivery failed: {e}")
            return False

    # ------------------------------------------------------------------- runner

    async def run_growth_cycle(
        self, *, dry_run: bool = False, force: bool = False
    ) -> dict[str, Any]:
        """Build the reflection, ask the LLM to rewrite, and apply per GROWTH_MODE.

        ``force`` bypasses the ``off`` mode check (used by the manual action).
        ``dry_run`` builds the proposal without applying or delivering it.
        """
        mode = str(config_registry.get_value("GROWTH_MODE", self.mode) or "on").lower()
        if mode == "off" and not force:
            return {"success": False, "message": "GROWTH_MODE=off"}

        log_info("[grillo_growth] Building weekly reflection context")
        diary_blob = await self._fetch_recent_diaries()

        # Derive a lightweight recall query from the current growth + likes so the
        # memory search stays meaningful (no keyword feature-routing: this is a
        # free-text similarity query, not intent detection).
        current_growth = await get_current_growth() or ""
        likes, _dislikes = self._current_likes_dislikes()
        recall_query = " ".join(
            [current_growth[:200]] + [str(x) for x in likes[:5]]
        ).strip()
        memories = await self._recall_memories(recall_query)

        proposal = await self._ask_llm_for_rewrite(diary_blob, memories)
        if not proposal:
            return {"success": False, "message": "LLM produced no valid rewrite"}

        if dry_run:
            return {
                "success": True,
                "message": "Dry run: proposal built, not applied",
                "proposal": proposal,
            }

        if mode == "request":
            delivered = await self._deliver_proposal(proposal)
            return {
                "success": delivered,
                "message": (
                    "Proposal delivered for approval"
                    if delivered
                    else "Proposal built but delivery failed"
                ),
                "proposal": proposal,
            }

        # mode == "on" (or forced): apply directly.
        new_id = await self._commit_proposal(proposal, source="weekly")
        return {
            "success": new_id is not None,
            "message": f"Self-growth updated (state id {new_id})",
            "proposal": proposal,
        }


PLUGIN_CLASS = GrilloGrowthPlugin
