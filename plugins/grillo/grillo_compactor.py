"""
plugins/grillo/grillo_compactor.py

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

    def get_supported_actions(self) -> dict:
        """The compactor exposes no LLM actions. Manual runs are triggered via the
        Web UI 'run_component' endpoint (which calls run_action directly) and the
        automatic scheduler, not by the LLM emitting an action.
        """
        return {}

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

    async def _run_one_compaction_cycle(
        self, dry_run: bool = False, marker: str | None = None
    ):
        """Select candidate memories, chunk them into windows, run clustering+compaction on the first valid window.

        If `dry_run=True`, do not persist changes and return proposed cluster results.
        """
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
            from core.config import get_active_cortex_engine

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
                    confidence = str(cl.get("confidence") or "low")
                    justification = str(cl.get("justification") or "")
                    detailed = cl.get("detailed") or cl.get("detailed_summary") or None
                    if detailed:
                        detailed = str(detailed).strip()

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

                    # Persist accepted clusters
                    async with get_conn_ctx() as conn:
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

                            # Insert new compacted memory into `memories` via helper
                            # Store the most useful content in `memories`: prefer 'detailed' if available, otherwise fallback to summary
                            memory_content = detailed if detailed else summary
                            try:
                                await insert_memory(
                                    content=memory_content,
                                    author="grillo",
                                    source="compaction",
                                    tags=json.dumps(tags) if tags else None,
                                    emotion=feeling,
                                    intensity=None,
                                    emotion_state=None,
                                )
                            except Exception as e:
                                log_warning(
                                    f"[grillo_compactor] Failed to insert new compacted memory using helper: {e}"
                                )
                                await cur.execute(
                                    "INSERT INTO memories (created_at, content, author, source, tags, scope, emotion, intensity, emotion_state) VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s)",
                                    (
                                        memory_content,
                                        "grillo",
                                        "compaction",
                                        json.dumps(tags) if tags else None,
                                        None,
                                        feeling,
                                        None,
                                        None,
                                    ),
                                )

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
