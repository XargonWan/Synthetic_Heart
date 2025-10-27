# core/db.py

from datetime import datetime, timezone, timedelta
import calendar
import asyncio
import time

from types import SimpleNamespace
from typing import Any

# ``aiomysql`` is an optional dependency.  Import it lazily and provide a
# minimal stub when it's not installed so modules depending on ``core.db`` can
# still be imported during tests.
try:  # pragma: no cover - import guard
    import aiomysql  # type: ignore
except Exception:  # pragma: no cover - executed when aiomysql missing
    async def _missing_connect(*args, **kwargs):
        raise RuntimeError("aiomysql is not installed")

    aiomysql = SimpleNamespace(  # type: ignore
        Connection=object,
        Cursor=object,
        connect=_missing_connect,
    )

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Database connection parameters
DB_HOST = config_registry.get_value(
    "DB_HOST",
    "localhost",
    label="Database Host",
    description="Host used to connect to the synth MariaDB instance.",
    group="database",
    component="core",
    advanced=True,
    tags=["bootstrap"],
)
DB_PORT = config_registry.get_value(
    "DB_PORT",
    3306,
    label="Database Port",
    description="Port used to connect to the synth MariaDB instance.",
    value_type=int,
    group="database",
    component="core",
    advanced=True,
    tags=["bootstrap"],
)
DB_USER = config_registry.get_value(
    "DB_USER",
    "synth",
    label="Database User",
    description="Database username used by Synth.",
    group="database",
    component="core",
    advanced=True,
    tags=["bootstrap"],
)
DB_PASS = config_registry.get_value(
    "DB_PASS",
    "synth",
    label="Database Password",
    description="Database password used by the Synth.",
    group="database",
    component="core",
    advanced=True,
    sensitive=True,
    tags=["bootstrap"],
)
DB_NAME = config_registry.get_value(
    "DB_NAME",
    "synth",
    label="Database Name",
    description="Database schema used by the Synth.",
    group="database",
    component="core",
    advanced=True,
    tags=["bootstrap"],
)

# Test di connessione con retry e logging dettagliato
async def wait_for_db(max_attempts=10, delay=3):
    """Wait for the DB to be reachable, with retry and detailed logging."""
    for attempt in range(1, max_attempts + 1):
        try:
            log_info(f"[db] Attempt {attempt}: connecting to {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
            conn = await aiomysql.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASS,
                db=DB_NAME,
                autocommit=True,
            )
            log_info("[db] Successfully connected to the database!")
            conn.close()
            return True
        except Exception as e:
            log_warning(f"[db] Connection failed: {e}")
            await asyncio.sleep(delay)
    log_error(f"[db] Could not connect to the database after {max_attempts} attempts.")
    return False

_db_logging_initialized = False

def initialize_db_logging():
    """Log database configuration for debugging purposes."""
    global _db_logging_initialized
    if _db_logging_initialized:
        return
    log_info(f"[db] Configuration: HOST={DB_HOST}, PORT={DB_PORT}, USER={DB_USER}, DB_NAME={DB_NAME}")
    log_debug(f"[db] Password length: {len(DB_PASS)} characters")
    _db_logging_initialized = True

_db_initialized = False
_db_init_lock = asyncio.Lock()

# Throttle DB 'Opening connection' debug logs to at most one per X seconds
_DB_LOG_THROTTLE_SEC = 2
_last_db_log_time = 0

# Database connection pool
_pool = None
_pool_lock = asyncio.Lock()

async def get_pool():
    """Get or create the database connection pool."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                log_info("[db] Creating connection pool")
                _pool = await aiomysql.create_pool(
                    host=DB_HOST,
                    port=DB_PORT,
                    user=DB_USER,
                    password=DB_PASS,
                    db=DB_NAME,
                    autocommit=True,
                    minsize=1,
                    maxsize=50,  # Increased to resolve deadlock during config loading
                    pool_recycle=3600,  # Recycle connections every hour
                )
    return _pool

async def get_conn() -> aiomysql.Connection:
    """Return an async MariaDB connection from the connection pool."""
    global _last_db_log_time
    try:
        now = time.time()
        if now - _last_db_log_time > _DB_LOG_THROTTLE_SEC:
            log_debug(
                f"[db] Acquiring connection from pool to {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
            )
            _last_db_log_time = now
    except Exception:
        pass
    
    log_debug("[db] About to call get_pool()")
    pool = await get_pool()
    log_debug("[db] get_pool() completed, about to call pool.acquire()")
    try:
        conn = await asyncio.wait_for(pool.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        log_error("[db] TIMEOUT acquiring connection from pool after 10 seconds - pool may be exhausted")
        raise TimeoutError("Database connection pool exhausted - timeout acquiring connection")
    log_debug("[db] Connection acquired from pool")
    return conn

async def release_conn(conn):
    """Release a connection back to the pool."""
    if conn:
        conn.close()  # This automatically returns the connection to the pool
        log_debug("[db] Connection released to pool")

async def test_connection() -> bool:
    """Check if the database is reachable."""
    try:
        conn = await get_conn()
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
        conn.close()
        return True
    except Exception as e:
        print(f"[test_connection] Error: {e}")
        return False

async def init_db() -> None:
    """Asynchronously initialize essential MariaDB tables (core only)."""
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            # settings table for configuration values - core functionality
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    `setting_key` VARCHAR(255) PRIMARY KEY,
                    `value` TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )

            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    `config_key` VARCHAR(255) PRIMARY KEY,
                    `value` TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )

            # Insert default settings if they don't exist
            await cur.execute(
                """
                INSERT IGNORE INTO settings (`setting_key`, `value`) VALUES ('active_llm', 'manual')
                """
            )
    except Exception as e:
        print(f"[init_db] Error: {e}")
    finally:
        conn.close()


async def ensure_core_tables() -> None:
    """Ensure core tables exist by initializing them once."""
    global _db_initialized
    if _db_initialized:
        return
    async with _db_init_lock:
        if not _db_initialized:
            await init_db()
            _db_initialized = True

# 🧠 Insert a new memory into the database
async def insert_memory(
    content: str,
    author: str,
    source: str,
    tags: str,
    scope: str | None = None,
    emotion: str | None = None,
    intensity: int | None = None,
    emotion_state: str | None = None,
    timestamp: str | None = None,
) -> None:
    if not timestamp:
        timestamp = datetime.now(timezone.utc).isoformat()

    await ensure_core_tables()

    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO memories (timestamp, content, author, source, tags, scope, emotion, intensity, emotion_state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (timestamp, content, author, source, tags, scope, emotion, intensity, emotion_state),
            )
    except Exception as e:
        print(f"[insert_memory] Error: {e}")
    finally:
        conn.close()

# 💥 Insert a new emotional event
async def insert_emotion_event(
    eid: str,
    source: str,
    event: str,
    emotion: str,
    intensity: int,
    state: str,
    trigger_condition: str,
    decision_logic: str,
    next_check: str,
) -> None:
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO emotion_diary (id, source, event, emotion, intensity, state, trigger_condition, decision_logic, next_check)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    eid,
                    source,
                    event,
                    emotion,
                    intensity,
                    state,
                    trigger_condition,
                    decision_logic,
                    next_check,
                ),
            )
    except Exception as e:
        print(f"[insert_emotion_event] Error: {e}")
    finally:
        conn.close()

# 🔍 Retrieve active emotions
async def get_active_emotions() -> list[dict]:
    conn = await get_conn()
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT * FROM emotion_diary
                WHERE state = 'active'
                """
            )
            rows = await cur.fetchall()
    except Exception as e:
        print(f"[get_active_emotions] Error: {e}")
        rows = []
    finally:
        conn.close()
    return [dict(row) for row in rows]

# ➕ Modify the intensity of an emotion
async def update_emotion_intensity(eid: str, delta: int) -> None:
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE emotion_diary
                SET intensity = intensity + %s
                WHERE id = %s
                """,
                (delta, eid),
            )
    except Exception as e:
        print(f"[update_emotion_intensity] Error: {e}")
    finally:
        conn.close()

# 💀 Mark an emotion as resolved
async def mark_emotion_resolved(eid: str) -> None:
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE emotion_diary
                SET state = 'resolved'
                WHERE id = %s
                """,
                (eid,),
            )
    except Exception as e:
        print(f"[mark_emotion_resolved] Error: {e}")
    finally:
        conn.close()

# 💎 Crystallize an active emotion
async def crystallize_emotion(eid: str) -> None:
    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE emotion_diary
                SET state = 'crystallized'
                WHERE id = %s
                """,
                (eid,),
            )
    except Exception as e:
        print(f"[crystallize_emotion] Error: {e}")
    finally:
        conn.close()

# 🔁 Retrieve recent responses generated by the bot
async def get_recent_responses(since_timestamp: str) -> list[dict]:
    conn = await get_conn()
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT * FROM memories
                WHERE source = 'synth' AND timestamp >= %s
                ORDER BY timestamp DESC
                """,
                (since_timestamp,),
            )
            rows = await cur.fetchall()
    except Exception as e:
        print(f"[get_recent_responses] Error: {e}")
        rows = []
    finally:
        conn.close()
    return [dict(row) for row in rows]

# === Event management helpers ===

async def insert_scheduled_event(
    date: str,
    time: str | None,
    recurrence_type: str,
    description: str,
    created_by: str = "synth",
) -> None:
    """Insert a new scheduled event using local time and store next_run in UTC."""

    if not time:
        time = "00:00"

    await ensure_core_tables()
    conn = await get_conn()
    try:
        async with conn.cursor() as cur:
            try:
                from core.time_zone_utils import parse_local_to_utc
                next_run_utc = parse_local_to_utc(date, time)
            except Exception as e:
                log_warning(
                    f"[insert_scheduled_event] Invalid date/time: {date} {time} - {e}"
                )
                return

            await safe_db_execute(
                cur,
                """
                INSERT INTO scheduled_events (`date`, `time`, next_run, recurrence_type, description, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    date,
                    time,
                    next_run_utc.isoformat(),
                    recurrence_type or "none",
                    description,
                    created_by,
                ),
                ensure_fn=ensure_core_tables,
            )
    except Exception as e:
        log_error(f"[insert_scheduled_event] Error: {e}")
    finally:
        conn.close()


async def get_due_events(now: datetime | None = None) -> list[dict]:
    """Return scheduled events that are ready for dispatch."""

    if now is None:
        now = datetime.now(timezone.utc)

    log_debug(f"[get_due_events] Checking events at UTC {now.isoformat()}")

    query = "SELECT * FROM scheduled_events WHERE delivered = 0 AND next_run <= %s ORDER BY id"
    log_debug(f"[get_due_events] Executing query: {query}")

    conn = await get_conn()
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await safe_db_execute(cur, query, (now.isoformat(),), ensure_fn=ensure_core_tables)
            rows = await cur.fetchall()
            log_debug(f"[get_due_events] Retrieved {len(rows)} rows")
            for row in rows:
                log_debug(f"[get_due_events] Row: {dict(row)}")
    except Exception as e:
        log_error(f"[get_due_events] Error executing query: {repr(e)}")
        rows = []
    finally:
        conn.close()
        log_debug("[get_due_events] Connection closed")

    due = []
    log_debug(f"[get_due_events] Retrieved {len(rows)} events from the database")

    for r in rows:
        log_debug(f"[get_due_events] Raw event data: {dict(r)}")
        scheduled_val = r.get('next_run')
        try:
            if isinstance(scheduled_val, datetime):
                event_dt = scheduled_val
            else:
                event_dt = datetime.fromisoformat(str(scheduled_val).replace('Z', '+00:00'))
            if event_dt.tzinfo is None:
                from core.time_zone_utils import get_local_timezone
                event_dt = (
                    event_dt.replace(tzinfo=get_local_timezone())
                    .astimezone(timezone.utc)
                )
            else:
                event_dt = event_dt.astimezone(timezone.utc)
        except Exception as e:
            log_warning(f"[get_due_events] Invalid datetime in next_run: {scheduled_val} - {e}")
            continue

        is_late = now > event_dt
        minutes_late = int((now - event_dt).total_seconds() / 60) if is_late else 0

        ev = dict(r)
        from core.time_zone_utils import format_dual_time
        ev.update(
            {
                "is_late": is_late,
                "minutes_late": minutes_late,
                "scheduled_time": format_dual_time(event_dt),
            }
        )
        due.append(ev)
        log_debug(f"[get_due_events] Due event: {ev}")
    log_debug(f"[get_due_events] Total due events: {len(due)}")
    return due


async def mark_event_delivered(event_id: int) -> bool:
    """Update an event after it has been dispatched.

    Returns ``True`` when the update succeeds and ``False`` otherwise.
    """
    await ensure_core_tables()
    conn = await get_conn()

    async with conn.cursor(aiomysql.DictCursor) as cur:
        await safe_db_execute(cur, "SELECT recurrence_type, next_run FROM scheduled_events WHERE id = %s", (event_id,), ensure_fn=ensure_core_tables)
        row = await cur.fetchone()
    if not row:
        log_warning(f"[db] Event {event_id} not found to be marked as delivered")
        conn.close()
        return False
    repeat_type = (row.get("recurrence_type") or "none").lower()
    next_run_val = row.get("next_run")

    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            try:
                if next_run_val:
                    next_run_dt = datetime.fromisoformat(str(next_run_val).replace('Z', '+00:00'))
                else:
                    next_run_dt = None
                if next_run_dt and next_run_dt.tzinfo is None:
                    from core.time_zone_utils import get_local_timezone
                    next_run_dt = (
                        next_run_dt.replace(tzinfo=get_local_timezone())
                        .astimezone(timezone.utc)
                    )
                elif next_run_dt:
                    next_run_dt = next_run_dt.astimezone(timezone.utc)
            except Exception as e:
                log_warning(f"[db] Invalid next_run for event {event_id}: {next_run_val} - {e}")
                next_run_dt = None

            if repeat_type == "none":
                await safe_db_execute(
                    cur,
                    "UPDATE scheduled_events SET delivered = 1 WHERE id = %s",
                    (event_id,),
                    ensure_fn=ensure_core_tables,
                )
                log_info(f"[db] Event {event_id} marked as delivered (one-time)")
                return True

            elif repeat_type == "always":
                # Always recurring events stay active indefinitely
                log_debug(f"[db] Event {event_id} remains active (always recurrence)")
                return True

            else:
                if not next_run_dt:
                    log_warning(f"[db] Missing next_run for repeating event {event_id}")
                    return False

                if repeat_type == "daily":
                    new_dt = next_run_dt + timedelta(days=1)
                elif repeat_type == "weekly":
                    new_dt = next_run_dt + timedelta(days=7)
                elif repeat_type == "monthly":
                    year = next_run_dt.year + (next_run_dt.month // 12)
                    month = next_run_dt.month % 12 + 1
                    day = min(next_run_dt.day, calendar.monthrange(year, month)[1])
                    new_dt = next_run_dt.replace(year=year, month=month, day=day)
                else:
                    log_warning(f"[db] Unknown recurrence type '{repeat_type}' for event {event_id}")
                    return False

                new_iso = new_dt.astimezone(timezone.utc).isoformat()
                await safe_db_execute(
                    cur,
                    "UPDATE scheduled_events SET next_run = %s WHERE id = %s",
                    (new_iso, event_id),
                    ensure_fn=ensure_core_tables,
                )
                log_info(f"[db] Event {event_id} rescheduled to {new_iso}")
                return True
    except Exception as e:
        log_error(f"[mark_event_delivered] Error: {e}")
        return False
    finally:
        conn.close()

def is_valid_datetime_format(date_str: str, time_str: str | None) -> bool:
    """Verifica se la data e l'ora sono in un formato valido."""
    dt_str = f"{date_str} {time_str or '00:00'}"
    try:
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        log_debug(f"[is_valid_datetime_format] Valid datetime format: {dt_str}")
        return True
    except ValueError as e:
        log_warning(f"[is_valid_datetime_format] Invalid datetime format: {dt_str} - {e}")
        return False


async def safe_db_execute(
    cursor: Any,
    query: str,
    params: tuple | list = (),
    ensure_fn=None,
) -> any:
    """Execute a SQL statement and retry once if table missing (error 1146).

    Args:
        cursor: Active aiomysql cursor.
        query: SQL query to execute.
        params: Parameters for the query.
        ensure_fn: Coroutine that creates the missing table if called.

    Returns:
        Result of ``cursor.execute``.
    """
    try:
        log_debug(f"[safe_db_execute] Executing: {query} {params}")
        return await cursor.execute(query, params)
    except aiomysql.Error as e:
        err_code = e.args[0] if e.args else None
        if err_code == 1146 and ensure_fn:
            log_debug(
                f"[safe_db_execute] Table missing for query. Calling ensure_fn()"
            )
            try:
                await ensure_fn()
            except Exception as ensure_exc:  # pragma: no cover - best effort
                log_error(
                    f"[safe_db_execute] ensure_fn failed: {repr(ensure_exc)}"
                )
                raise
            try:
                log_debug("[safe_db_execute] Retrying query after ensure_fn")
                return await cursor.execute(query, params)
            except Exception as retry_exc:
                log_error(
                    f"[safe_db_execute] Retry failed: {repr(retry_exc)}"
                )
                raise
        log_error(f"[safe_db_execute] Query failed: {repr(e)}")
        raise

async def execute_query(query: str, params: tuple = ()) -> list:
    """Execute a SQL query and return the results."""
    try:
        conn = await get_conn()
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            results = await cur.fetchall()
        conn.close()
        return results
    except Exception as e:
        log_error(f"[execute_query] Error executing query: {query}, Error: {e}")
        raise
