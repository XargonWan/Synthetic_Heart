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

    # Provide a minimal stub exposing the async connect/create_pool API so
    # calling sites receive a clear RuntimeError instead of an AttributeError
    # when aiomysql is not installed.
    aiomysql = SimpleNamespace(  # type: ignore
        Connection=object,
        Cursor=object,
        connect=_missing_connect,
        create_pool=_missing_connect,
    )

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
import os


# NOTE: To avoid import-time circular dependencies between `core.db` and
# `core.config_manager` (many modules import `get_conn` at import time),
# read database configuration lazily from `config_registry` when needed
# instead of at module import time. This prevents partially-initialized
# module errors during startup.
def _read_db_config():
    """Return DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME reading from
    config_registry when available, otherwise from environment or defaults.
    """
    # DB connection settings must be environment-driven.
    # If a previous run persisted an incorrect DB_HOST (e.g. "localhost") into
    # config_registry/DB, preferring that value can lock the system out of DB.
    env_host = os.getenv("DB_HOST")
    env_port = os.getenv("DB_PORT")
    env_user = os.getenv("DB_USER")
    env_pass = os.getenv("DB_PASS")
    env_name = os.getenv("DB_NAME")

    # Start from env (or hard defaults if env is missing)
    host = env_host or "localhost"
    try:
        port = int(env_port) if env_port is not None else 3306
    except Exception:
        port = 3306
    user = env_user or "synth"
    passwd = env_pass or "synth"
    dbname = env_name or "synth"

    # Allow config_registry to fill only the missing pieces (never override env)
    try:
        if env_host is None:
            host = config_registry.get_value("DB_HOST", host)
        if env_port is None:
            port = int(config_registry.get_value("DB_PORT", port))
        if env_user is None:
            user = config_registry.get_value("DB_USER", user)
        if env_pass is None:
            passwd = config_registry.get_value("DB_PASS", passwd)
        if env_name is None:
            dbname = config_registry.get_value("DB_NAME", dbname)
    except Exception:
        pass

    return host, port, user, passwd, dbname


# Test di connessione con retry e logging dettagliato
async def wait_for_db(max_attempts=10, delay=3):
    """Wait for the DB to be reachable, with retry and detailed logging."""
    for attempt in range(1, max_attempts + 1):
        try:
            host, port, user, passwd, dbname = _read_db_config()
            log_debug(
                f"[db] Attempt {attempt}: connecting to {user}@{host}:{port}/{dbname}"
            )
            conn = await aiomysql.connect(
                host=host,
                port=port,
                user=user,
                password=passwd,
                db=dbname,
                autocommit=True,
            )
            log_debug("[db] Successfully connected to the database!")
            conn.close()
            return True
        except Exception as e:
            log_warning(f"[db] Connection failed: {e}")
            await asyncio.sleep(delay)
    log_error(f"[db] Could not connect to the database after {max_attempts} attempts.")
    return False


_db_logging_initialized = False

# Track if we've already warned about unsupported `max_execution_time` so we don't spam logs
_max_execution_time_unsupported_reported = False


def initialize_db_logging():
    """Log database configuration for debugging purposes."""
    global _db_logging_initialized
    if _db_logging_initialized:
        return
    try:
        host, port, user, passwd, dbname = _read_db_config()
        log_info(
            f"[db] Configuration: HOST={host}, PORT={port}, USER={user}, DB_NAME={dbname}"
        )
        try:
            log_debug(f"[db] Password length: {len(passwd)} characters")
        except Exception:
            log_debug("[db] Password length: <unavailable>")
    except Exception:
        log_info("[db] Configuration: <unable to read DB config at import-time>")
    _db_logging_initialized = True


_db_initialized = False
_db_init_lock = asyncio.Lock()

# Throttle DB 'Opening connection' debug logs to at most one per X seconds
_DB_LOG_THROTTLE_SEC = 2
_last_db_log_time = 0

# Database connection pool
_pools_by_loop: dict[int, Any] = {}
_pool_lock = asyncio.Lock()

# Track active connections for leak detection and monitoring
_active_conn_count = 0
_conn_acquired_times: dict[int, float] = {}
_conn_acquired_stacks: dict[int, str] = {}


async def get_pool():
    """Get or create the database connection pool."""
    global _pool
    # Determine current event loop and use/create a pool bound to it.
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync context). Use loop id 0 as a fallback key.
        current_loop = None

    loop_id = id(current_loop) if current_loop is not None else 0

    pool = _pools_by_loop.get(loop_id)
    if pool is None:
        async with _pool_lock:
            # Double-check under lock
            pool = _pools_by_loop.get(loop_id)
            if pool is None:
                log_info("[db] Creating connection pool for loop id=%s" % loop_id)
                # Allow pool size to be configured via config_registry or environment
                try:
                    DB_POOL_MINSIZE = int(
                        os.getenv(
                            "DB_POOL_MINSIZE",
                            config_registry.get_value(
                                "DB_POOL_MINSIZE",
                                1,
                                label="DB Pool Min Size",
                                group="database",
                                component="core",
                                advanced=True,
                            ),
                        )
                    )
                except Exception:
                    DB_POOL_MINSIZE = 1
                try:
                    DB_POOL_MAXSIZE = int(
                        os.getenv(
                            "DB_POOL_MAXSIZE",
                            config_registry.get_value(
                                "DB_POOL_MAXSIZE",
                                60,
                                label="DB Pool Max Size",
                                group="database",
                                component="core",
                                advanced=True,
                            ),
                        )
                    )
                except Exception:
                    DB_POOL_MAXSIZE = 60

                try:
                    host, port, user, passwd, dbname = _read_db_config()
                except Exception:
                    host, port, user, passwd, dbname = (
                        os.getenv("DB_HOST", "localhost"),
                        int(os.getenv("DB_PORT", "3306")),
                        os.getenv("DB_USER", "synth"),
                        os.getenv("DB_PASS", "synth"),
                        os.getenv("DB_NAME", "synth"),
                    )

                log_info(
                    f"[db] Creating pool with minsize={DB_POOL_MINSIZE} maxsize={DB_POOL_MAXSIZE}"
                )
                new_pool = await aiomysql.create_pool(
                    host=host,
                    port=port,
                    user=user,
                    password=passwd,
                    db=dbname,
                    autocommit=True,
                    minsize=DB_POOL_MINSIZE,
                    maxsize=DB_POOL_MAXSIZE,
                    pool_recycle=300,  # Recycle connections every 5 minutes (was 3600s) to prevent zombie connections
                )
                # Store the pool keyed by the loop id so concurrent event loops
                # get a pool bound to their loop (avoids cross-loop use errors).
                _pools_by_loop[loop_id] = new_pool
                pool = new_pool

    return pool


def get_pool_debug_info(max_stacks: int = 3) -> dict:
    """Return diagnostics about the DB pool and currently acquired connections.

    This is safe to call from sync code and used by debug endpoints.
    """
    try:
        info = {
            "active_connections": _active_conn_count,
            "acquired_count": len(_conn_acquired_times),
            "oldest_held_seconds": None,
            "oldest_stack": None,
        }
        if _conn_acquired_times:
            now = time.time()
            oldest_id = None
            oldest_age = 0
            for cid, ts in list(_conn_acquired_times.items()):
                age = now - ts
                if age > oldest_age:
                    oldest_age = age
                    oldest_id = cid
            info["oldest_held_seconds"] = int(oldest_age)
            if oldest_id and _conn_acquired_stacks.get(oldest_id):
                info["oldest_stack"] = _conn_acquired_stacks.get(oldest_id)

        # Provide a small sample of stacks (up to max_stacks)
        stacks = []
        for cid, stack in list(_conn_acquired_stacks.items())[:max_stacks]:
            stacks.append({"id": cid, "stack": stack})
        info["stacks"] = stacks
        return info
    except Exception:
        return {"active_connections": _active_conn_count}


async def get_conn() -> aiomysql.Connection:
    """Return an async MariaDB connection from the connection pool."""
    global _last_db_log_time
    global _active_conn_count
    try:
        now = time.time()
        if now - _last_db_log_time > _DB_LOG_THROTTLE_SEC:
            try:
                host, port, user, passwd, dbname = _read_db_config()
                target = f"{user}@{host}:{port}/{dbname}"
            except Exception:
                target = "<unknown-db>"
            log_debug(f"[db] Acquiring connection from pool to {target}")
            _last_db_log_time = now
    except Exception:
        pass

    log_debug("[db] About to call get_pool()")
    pool = await get_pool()
    log_debug("[db] get_pool() completed, about to call pool.acquire()")
    try:
        conn = await asyncio.wait_for(pool.acquire(), timeout=30.0)
    except asyncio.CancelledError:
        # Preserve cancellation semantics but log for debugging
        log_error("[db] get_conn cancelled while waiting for pool.acquire()")
        raise
    except asyncio.TimeoutError:
        log_error(
            "[db] TIMEOUT acquiring connection from pool after 30 seconds - pool may be exhausted"
        )
        raise TimeoutError(
            "Database connection pool exhausted - timeout acquiring connection"
        )

    # Set query timeout to prevent long-running queries from holding connections indefinitely
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SET SESSION max_execution_time=30000"
            )  # 30 second timeout per query
    except Exception as e:
        # Some MySQL/MariaDB servers (or older versions) don't support
        # the `max_execution_time` session variable (error 1193 / "Unknown system variable").
        # Treat that specific case as informational but warn only once to avoid log spamming.
        try:
            msg = str(e)
            if "Unknown system variable 'max_execution_time'" in msg or "1193" in msg:
                try:
                    # report the unsupported-variable condition only the first time
                    global _max_execution_time_unsupported_reported
                    if not _max_execution_time_unsupported_reported:
                        log_warning(
                            "[db] DB server does not support session max_execution_time; query timeouts will not be enforced (first occurrence): %s"
                            % msg
                        )
                        _max_execution_time_unsupported_reported = True
                except Exception:
                    # Fallback to debug logging if something goes wrong updating the flag
                    log_debug(
                        f"[db] Could not set query timeout (ignoring unsupported var): {e}"
                    )
            else:
                # Other errors are unexpected — log as error so maintainers notice
                log_error(f"[db] Could not set query timeout: {e}")
        except Exception:
            log_error(f"[db] Could not set query timeout: {e}")

    # Track active connections for monitoring/leak detection
    try:
        _active_conn_count += 1
        try:
            _conn_acquired_times[id(conn)] = time.time()
            # Capture a short stack trace at acquisition time to help diagnose
            # where connections are being held without release.
            try:
                import traceback

                stack = traceback.format_stack(limit=8)
                _conn_acquired_stacks[id(conn)] = "".join(stack)
            except Exception:
                pass
        except Exception:
            pass
        # Warn when we're close to pool capacity
        try:
            maxsize = getattr(pool, "maxsize", None)
            # Only warn if we're at or very close to pool limit
            # For small pools (size 1-2), require actual connection pressure
            # For larger pools, warn when within 2 of limit
            warning_threshold = (
                max(maxsize - 2, maxsize) if maxsize and maxsize > 2 else maxsize
            )
            if maxsize and _active_conn_count >= warning_threshold:
                # Compute the oldest-held connection age and include a stack
                oldest_age = 0
                oldest_id = None
                try:
                    now = time.time()
                    for cid, ts in list(_conn_acquired_times.items()):
                        age = now - ts
                        if age > oldest_age:
                            oldest_age = age
                            oldest_id = cid
                except Exception:
                    oldest_age = 0
                    oldest_id = None

                msg = f"[db] Active DB connections high: {_active_conn_count}/{maxsize}"
                if oldest_id is not None:
                    try:
                        stack_snip = _conn_acquired_stacks.get(oldest_id, None)
                        if stack_snip:
                            msg += f"; oldest held={int(oldest_age)}s; sample acquisition stack:\n{stack_snip}"
                        else:
                            msg += f"; oldest held={int(oldest_age)}s"
                    except Exception:
                        msg += f"; oldest held={int(oldest_age)}s"
                log_warning(msg)
        except Exception:
            pass
    except Exception:
        pass

    log_debug("[db] Connection acquired from pool")

    # Wrap the raw aiomysql connection in a small proxy so that existing
    # call sites that call `conn.close()` will trigger our `release_conn`
    # routine (which updates internal counters and returns the connection
    # to the pool correctly). This avoids having to change many call-sites
    # across the codebase.
    class _ConnProxy:
        def __init__(self, _conn, _pool):
            self._conn = _conn
            self._pool = _pool

        def __getattr__(self, item):
            return getattr(self._conn, item)

        def close(self):
            """Synchronous close that releases connection back to pool immediately.

            We use pool.release() directly instead of scheduling an async task
            because the connection needs to be returned to the pool immediately
            to avoid connection leaks.
            """
            global _active_conn_count
            try:
                # Release directly to pool - this is synchronous and safe
                if self._pool and hasattr(self._pool, "release"):
                    try:
                        self._pool.release(self._conn)
                    except Exception:
                        pass

                log_debug("[db] Connection released to pool")
            finally:
                try:
                    _active_conn_count = max(0, _active_conn_count - 1)
                except Exception:
                    pass
                try:
                    _conn_acquired_times.pop(id(self._conn), None)
                except Exception:
                    pass
                try:
                    _conn_acquired_stacks.pop(id(self._conn), None)
                except Exception:
                    pass

        def cursor(self, *args, **kwargs):
            """Return a cursor helper that supports both awaiting and async-context use.

            Call sites in the codebase historically do two different things:
            - "cursor = await conn.cursor()" (awaiting a coroutine that returns a cursor)
            - "async with conn.cursor() as cur: ..." (using an async context manager)

            To remain compatible with both styles, we return a small wrapper that
            implements both the awaitable protocol (so `await conn.cursor()` returns
            the underlying cursor) and the async context manager protocol (so
            `async with conn.cursor() as cur` works too). The wrapper will also
            attempt to close the underlying cursor on exit, awaiting `close()` if
            it's awaitable.
            """
            try:
                real_cursor_call = getattr(self._conn, "cursor")
            except Exception:
                real_cursor_call = None

            if real_cursor_call is None:
                # Fallback to a no-op context manager
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _null_ctx():
                    yield None

                return _null_ctx()

            real_cursor_obj = real_cursor_call(*args, **kwargs)

            import inspect

            class _CursorWrapper:
                def __init__(self, real):
                    self._real = real
                    self._cursor = None
                    self._entered_cm = None

                def _wrap_cursor_obj(self, inner_cur):
                    """Return a proxy cursor that intercepts execute/executemany and
                    attempts an idempotent auto-heal (ensure_core_tables/ensure_plugin_tables)
                    on schema-related errors (1146 / 1054) before retrying once.
                    """
                    import inspect as _inspect
                    import os as _os
                    from core.logging_utils import (
                        log_info as _log_info,
                        log_warning as _log_warning,
                    )

                    AUTO_HEAL = _os.getenv("DB_AUTO_HEAL", "1") not in (
                        "0",
                        "false",
                        "False",
                    )

                    async def _exec_wrapper(method, *a, **kw):
                        try:
                            res = method(*a, **kw)
                            if _inspect.isawaitable(res):
                                return await res
                            return res
                        except Exception as exc:
                            msg = str(exc) or ""
                            is_schema_error = (
                                "1146" in msg
                                or "doesn't exist" in msg
                                or "1054" in msg
                                or "Unknown column" in msg
                            )
                            if AUTO_HEAL and is_schema_error:
                                _log_warning(
                                    f"[db] Schema error detected during DB execute: {msg}. Attempting auto-heal."
                                )
                                try:
                                    await ensure_core_tables()
                                    await ensure_plugin_tables()
                                    _log_info(
                                        "[db] Auto-heal applied; retrying query once"
                                    )
                                except Exception as heal_err:
                                    _log_warning(f"[db] Auto-heal failed: {heal_err}")
                                    raise
                                # retry once
                                res2 = method(*a, **kw)
                                if _inspect.isawaitable(res2):
                                    return await res2
                                return res2
                            raise

                    class _ProxyCursor:
                        def __init__(self, inner):
                            self._inner = inner

                        def __getattr__(self, name):
                            # Intercept execute/executemany only; forward everything else
                            if name in ("execute", "executemany"):
                                orig = getattr(self._inner, name)

                                async def _wrapped(*args, **kwargs):
                                    return await _exec_wrapper(orig, *args, **kwargs)

                                return _wrapped
                            return getattr(self._inner, name)

                        async def close(self):
                            close_fn = getattr(self._inner, "close", None)
                            if close_fn:
                                res = close_fn()
                                if _inspect.isawaitable(res):
                                    await res

                    return _ProxyCursor(inner_cur)

                def __await__(self):
                    async def _get():
                        # Awaitable underlying call (e.g. await conn.cursor())
                        if inspect.isawaitable(self._real):
                            cur = await self._real
                            self._cursor = self._wrap_cursor_obj(cur)
                            return self._cursor

                        # Underlying object is an async context manager
                        if hasattr(self._real, "__aenter__"):
                            cm = self._real
                            enter_res = cm.__aenter__()
                            if inspect.isawaitable(enter_res):
                                cur = await enter_res
                            else:
                                cur = enter_res

                            self._cursor = self._wrap_cursor_obj(cur)
                            self._entered_cm = cm
                            return self._cursor

                        # Plain cursor object
                        cur = self._real
                        self._cursor = self._wrap_cursor_obj(cur)
                        return self._cursor

                    return _get().__await__()

                async def __aenter__(self):
                    try:
                        # If underlying call returned a coroutine that needs awaiting,
                        # handle that first and wrap the resulting cursor.
                        if self._cursor is None and inspect.isawaitable(self._real):
                            cur = await self._real
                            self._cursor = self._wrap_cursor_obj(cur)

                        # If the underlying cursor is itself an async context manager,
                        # prefer to delegate to its __aenter__ for any setup logic.
                        try:
                            enter_fn = getattr(self._cursor, "__aenter__", None)
                            if enter_fn:
                                res = enter_fn()
                                if inspect.isawaitable(res):
                                    await res
                                return self._cursor
                        except Exception:
                            pass

                        return self._cursor
                    except Exception:
                        # Reset partially-initialized state on error
                        self._cursor = None
                        raise

                async def __aexit__(self, exc_type, exc, tb):
                    # If the underlying cursor provides __aexit__, use it.
                    try:
                        exit_fn = getattr(self._cursor, "__aexit__", None)
                        if exit_fn:
                            res = exit_fn(exc_type, exc, tb)
                            if inspect.isawaitable(res):
                                await res
                            return
                    except Exception:
                        pass

                    # Otherwise, attempt to close the cursor (await if necessary)
                    try:
                        close_fn = getattr(self._cursor, "close", None)
                        if close_fn:
                            res = close_fn()
                            if inspect.isawaitable(res):
                                await res
                    except Exception:
                        pass

            return _CursorWrapper(real_cursor_obj)

    return _ConnProxy(conn, pool)


class _ConnContext:
    """Async context manager that yields a DB connection and ensures it is
    released via `release_conn` when the context exits. Use this instead of
    calling `get_conn()` directly to avoid leaking connections.
    """

    async def __aenter__(self):
        try:
            self._conn = await get_conn()
        except Exception:
            # Mark that we failed to acquire a connection so __aexit__ knows not to release
            self._conn = None
            raise
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        # Only try to release if we actually acquired a connection
        if self._conn is not None:
            try:
                await release_conn(self._conn)
            except Exception:
                pass


def get_conn_ctx():
    """Return an async context manager for acquiring/releasing a DB connection.

    Usage:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(...)
    """
    return _ConnContext()


async def release_conn(conn):
    """Release a connection back to the pool."""
    global _active_conn_count
    if not conn:
        return
    try:
        # Attempt to close the connection which returns it to the pool
        try:
            conn.close()
        except Exception:
            # Some aiomysql internals may expose pool.release; attempt it as fallback
            try:
                pool = await get_pool()
                if hasattr(pool, "release"):
                    pool.release(conn)
            except Exception:
                pass

        log_debug("[db] Connection released to pool")
    finally:
        try:
            _active_conn_count = max(0, _active_conn_count - 1)
        except Exception:
            pass
        try:
            _conn_acquired_times.pop(id(conn), None)
        except Exception:
            pass
        try:
            _conn_acquired_stacks.pop(id(conn), None)
        except Exception:
            pass


async def test_connection() -> bool:
    """Check if the database is reachable."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        return True
    except Exception as e:
        print(f"[test_connection] Error: {e}")
        return False


async def init_db() -> None:
    """Asynchronously initialize essential MariaDB tables (core only)."""
    async with get_conn_ctx() as conn:
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
                INSERT IGNORE INTO settings (`setting_key`, `value`) VALUES ('active_llm', 'selenium_chatgpt')
                """
            )
        except Exception as e:
            print(f"[init_db] Error: {e}")


async def ensure_core_tables() -> None:
    """Ensure core tables exist by initializing them once."""
    global _db_initialized
    if _db_initialized:
        return
    async with _db_init_lock:
        if not _db_initialized:
            await init_db()
            # Initialize chat history cache table
            try:
                from core.chat_history_cache import init_chat_history_table

                await init_chat_history_table()
            except Exception as e:
                log_warning(f"[db] Failed to initialize chat history cache table: {e}")
            _db_initialized = True


async def ensure_plugin_tables() -> None:
    """Ensure plugin-managed tables exist (idempotent).

    This is a startup *preflight* that creates tables normally created
    lazily by plugins or present in init-db.sql so fresh installs won't
    hit 1146 "Table doesn't exist" errors.
    """
    try:
        # Some plugin tables reference `ai_diary` — ensure diary table first if available
        try:
            from plugins.ai_diary import init_diary_table

            await init_diary_table()
        except Exception:
            # No-op if plugin not present or init failed; we'll still attempt CREATE TABLE IF NOT EXISTS below
            log_debug("[db] init_diary_table not available or failed (continuing)")

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # bio (plugin)
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bio (
                        id VARCHAR(255) PRIMARY KEY,
                        known_as TEXT DEFAULT '[]',
                        likes TEXT DEFAULT '[]',
                        not_likes TEXT DEFAULT '[]',
                        information TEXT DEFAULT '',
                        past_events TEXT DEFAULT '[]',
                        feelings TEXT DEFAULT '[]',
                        contacts TEXT DEFAULT '{}',
                        social_accounts TEXT DEFAULT '[]',
                        privacy TEXT DEFAULT 'default',
                        created_at VARCHAR(50),
                        last_accessed VARCHAR(50)
                    )
                    """
                )

                # recent_chats (plugin)
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS recent_chats (
                        chat_id VARCHAR(255) PRIMARY KEY,
                        last_active DOUBLE NOT NULL,
                        metadata TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_last_active (last_active)
                    )
                    """
                )

                # grillo tables (init-db.sql + plugin may expect them)
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS grillo_activity_log (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        beat_type VARCHAR(50) NOT NULL,
                        prompt_text TEXT NOT NULL,
                        response_text LONGTEXT,
                        diary_entry_id INT,
                        executed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        metadata JSON,
                        suppressed_count INT DEFAULT 0,
                        INDEX idx_executed_at (executed_at),
                        INDEX idx_beat_type (beat_type),
                        INDEX idx_diary_entry (diary_entry_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                    """
                )

                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS grillo_action_execs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        activity_log_id INT NOT NULL,
                        action_index INT NOT NULL,
                        action_type VARCHAR(150) NOT NULL,
                        payload JSON,
                        status ENUM('pending','processed','failed') NOT NULL DEFAULT 'pending',
                        error_text TEXT,
                        result JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_activity_log_id (activity_log_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                    """
                )

                # agent tables (init-db.sql)
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_activity_log (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        command TEXT NOT NULL,
                        proposer VARCHAR(100),
                        status ENUM('proposed','approved','rejected','executed') NOT NULL DEFAULT 'proposed',
                        trainer_id VARCHAR(100),
                        request_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                        response_ts DATETIME,
                        result LONGTEXT,
                        metadata JSON
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                    """
                )

                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_action_execs (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        activity_log_id INT NOT NULL,
                        command TEXT NOT NULL,
                        status ENUM('pending','executed','failed') NOT NULL DEFAULT 'pending',
                        error_text TEXT,
                        result JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        INDEX idx_activity_log_id (activity_log_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                    """
                )

                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS agent_tasks (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        engine VARCHAR(64),
                        status ENUM('pending','running','waiting_for_approval','paused','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
                        input JSON,
                        iterations_meta JSON,
                        output JSON,
                        trainer_id VARCHAR(64),
                        metadata JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                    """
                )

                try:
                    await conn.commit()
                except Exception:
                    pass
        log_debug("[db] ensure_plugin_tables completed")
    except Exception as e:
        log_warning(f"[db] ensure_plugin_tables failed: {e}")


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
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    await ensure_core_tables()

    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO memories (timestamp, content, author, source, tags, scope, emotion, intensity, emotion_state)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        timestamp,
                        content,
                        author,
                        source,
                        tags,
                        scope,
                        emotion,
                        intensity,
                        emotion_state,
                    ),
                )
        except Exception as e:
            print(f"[insert_memory] Error: {e}")


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
    async with get_conn_ctx() as conn:
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


# 🔍 Retrieve active emotions
async def get_active_emotions() -> list[dict]:
    try:
        async with get_conn_ctx() as conn:
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
    return [dict(row) for row in rows]


# ➕ Modify the intensity of an emotion
async def update_emotion_intensity(eid: str, delta: int) -> None:
    await ensure_core_tables()
    async with get_conn_ctx() as conn:
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


# 💀 Mark an emotion as resolved
async def mark_emotion_resolved(eid: str) -> None:
    await ensure_core_tables()
    async with get_conn_ctx() as conn:
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


# 💎 Crystallize an active emotion
async def crystallize_emotion(eid: str) -> None:
    await ensure_core_tables()
    async with get_conn_ctx() as conn:
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


# 🔁 Retrieve recent responses generated by the bot
async def get_recent_responses(since_timestamp: str) -> list[dict]:
    try:
        async with get_conn_ctx() as conn:
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
    return [dict(row) for row in rows]


# === Event management helpers ===


async def insert_scheduled_event(
    date: str,
    time: str | None,
    recurrence_type: str,
    description: str,
    created_by: str = "synth",
    original_context: str = None,
    conversation_user_message: str = None,
    conversation_llm_response: str = None,
) -> None:
    """Insert a new scheduled event using local time and store next_run in UTC.

    Args:
        date: Event date (YYYY-MM-DD)
        time: Event time (HH:MM)
        recurrence_type: Recurrence pattern (none, daily, weekly, monthly, always)
        description: Event description
        created_by: Who created this event (default: "synth")
        original_context: Original context from conversation (optional, for user-initiated events)
        conversation_user_message: Original user message that triggered event creation (optional)
        conversation_llm_response: Original LLM response that created the event (optional)
    """

    if not time:
        time = "00:00"

    await ensure_core_tables()
    try:
        async with get_conn_ctx() as conn:
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
                    INSERT INTO scheduled_events (
                        `date`, `time`, next_run, recurrence_type, description, created_by,
                        original_context, conversation_user_message, conversation_llm_response
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        date,
                        time,
                        next_run_utc.strftime("%Y-%m-%d %H:%M:%S"),
                        recurrence_type or "none",
                        description,
                        created_by,
                        original_context,
                        conversation_user_message,
                        conversation_llm_response,
                    ),
                    ensure_fn=ensure_core_tables,
                )
    except Exception as e:
        log_error(f"[insert_scheduled_event] Error: {e}")


async def get_due_events(
    now: datetime | None = None, advance_minutes: int = 3
) -> list[dict]:
    """Return scheduled events that are ready for dispatch.

    All timestamps are stored in UTC in the database.
    Comparison is always done in UTC for consistency.

    Args:
        now: Current time (UTC). Defaults to current UTC time.
        advance_minutes: Number of minutes to check ahead for events (default: 3).
                        This accounts for LLM processing delays.
    """

    if now is None:
        now = datetime.now(timezone.utc)

    # Add advance window to account for LLM processing time
    check_time = now + timedelta(minutes=advance_minutes)

    log_debug(
        f"[get_due_events] Checking events at UTC {now.isoformat()} (with {advance_minutes}min advance: {check_time.isoformat()})"
    )

    # Query: find events that are due (not delivered and next_run is within advance window)
    # All timestamps stored in DB are already in UTC (converted during insert)
    check_str = check_time.strftime("%Y-%m-%d %H:%M:%S")
    query = "SELECT * FROM scheduled_events WHERE delivered = 0 AND next_run <= %s ORDER BY id"
    log_debug(
        f"[get_due_events] Executing query with UTC time (+ {advance_minutes}min): {check_str}"
    )

    rows = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await safe_db_execute(
                    cur, query, (check_str,), ensure_fn=ensure_core_tables
                )
                rows = await cur.fetchall()
                log_debug(f"[get_due_events] Retrieved {len(rows)} rows")
                for row in rows:
                    log_debug(f"[get_due_events] Row: {dict(row)}")
    except Exception as e:
        log_error(f"[get_due_events] Error executing query: {repr(e)}")
        rows = []

    log_debug("[get_due_events] Connection released")

    due = []
    log_debug(f"[get_due_events] Retrieved {len(rows)} events from the database")

    for r in rows:
        log_debug(f"[get_due_events] Raw event data: {dict(r)}")
        scheduled_val = r.get("next_run")
        try:
            if isinstance(scheduled_val, datetime):
                event_dt = scheduled_val
            else:
                event_dt = datetime.fromisoformat(
                    str(scheduled_val).replace("Z", "+00:00")
                )
            # If no timezone info, assume it's UTC (as stored in the database)
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=timezone.utc)
            else:
                event_dt = event_dt.astimezone(timezone.utc)
        except Exception as e:
            log_warning(
                f"[get_due_events] Invalid datetime in next_run: {scheduled_val} - {e}"
            )
            continue

        # Calculate lateness: an event is late only if now (without advance) is past its scheduled time
        # This ensures events retrieved within the advance window are not marked as late
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

    try:
        async with get_conn_ctx() as conn:
            # Fetch event info
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await safe_db_execute(
                    cur,
                    "SELECT recurrence_type, next_run FROM scheduled_events WHERE id = %s",
                    (event_id,),
                    ensure_fn=ensure_core_tables,
                )
                row = await cur.fetchone()

            if not row:
                log_warning(
                    f"[db] Event {event_id} not found to be marked as delivered"
                )
                return False

            repeat_type = (row.get("recurrence_type") or "none").lower()
            next_run_val = row.get("next_run")

            # Process event update based on recurrence type
            async with conn.cursor(aiomysql.DictCursor) as cur:
                try:
                    if next_run_val:
                        next_run_dt = datetime.fromisoformat(
                            str(next_run_val).replace("Z", "+00:00")
                        )
                    else:
                        next_run_dt = None
                    if next_run_dt and next_run_dt.tzinfo is None:
                        from core.time_zone_utils import get_local_timezone

                        next_run_dt = next_run_dt.replace(
                            tzinfo=get_local_timezone()
                        ).astimezone(timezone.utc)
                    elif next_run_dt:
                        next_run_dt = next_run_dt.astimezone(timezone.utc)
                except Exception as e:
                    log_warning(
                        f"[db] Invalid next_run for event {event_id}: {next_run_val} - {e}"
                    )
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
                    log_debug(
                        f"[db] Event {event_id} remains active (always recurrence)"
                    )
                    return True

                else:
                    if not next_run_dt:
                        log_warning(
                            f"[db] Missing next_run for repeating event {event_id}"
                        )
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
                        log_warning(
                            f"[db] Unknown recurrence type '{repeat_type}' for event {event_id}"
                        )
                        return False

                    new_iso = new_dt.astimezone(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
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


def is_valid_datetime_format(date_str: str, time_str: str | None) -> bool:
    """Verifica se la data e l'ora sono in un formato valido."""
    dt_str = f"{date_str} {time_str or '00:00'}"
    try:
        datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        log_debug(f"[is_valid_datetime_format] Valid datetime format: {dt_str}")
        return True
    except ValueError as e:
        log_warning(
            f"[is_valid_datetime_format] Invalid datetime format: {dt_str} - {e}"
        )
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
            log_debug("[safe_db_execute] Table missing for query. Calling ensure_fn()")
            try:
                await ensure_fn()
            except Exception as ensure_exc:  # pragma: no cover - best effort
                log_error(f"[safe_db_execute] ensure_fn failed: {repr(ensure_exc)}")
                raise
            try:
                log_debug("[safe_db_execute] Retrying query after ensure_fn")
                return await cursor.execute(query, params)
            except Exception as retry_exc:
                log_error(f"[safe_db_execute] Retry failed: {repr(retry_exc)}")
                raise
        log_error(f"[safe_db_execute] Query failed: {repr(e)}")
        raise


async def start_pool_cleanup_task():
    """Start a background task that monitors and cleans up the database connection pool.

    When pool usage reaches 85%, force-kill oldest non-critical connections to prevent
    pool exhaustion. This is an emergency measure to maintain system stability under load.
    """
    global _active_conn_count

    async def cleanup_monitor():
        global _active_conn_count

        while True:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                try:
                    pool = await get_pool()
                except Exception:
                    continue

                maxsize = getattr(pool, "maxsize", 50)
                usage_percent = (
                    (_active_conn_count / maxsize * 100) if maxsize > 0 else 0
                )

                # Threshold: 85% of pool exhausted
                if usage_percent >= 85:
                    log_warning(
                        f"[db] Pool usage CRITICAL: {_active_conn_count}/{maxsize} ({usage_percent:.1f}%)"
                    )

                    # Identify oldest connections to kill
                    now = time.time()
                    candidates = []

                    for cid, ts in list(_conn_acquired_times.items()):
                        age = now - ts
                        # Only consider connections held for more than 30 seconds
                        if age > 30:
                            candidates.append((cid, age))

                    # Sort by age (oldest first)
                    candidates.sort(key=lambda x: x[1], reverse=True)

                    # Kill up to 5 oldest connections
                    killed = 0
                    for cid, age in candidates[:5]:
                        try:
                            log_warning(
                                f"[db] Emergency pool cleanup: killing connection {cid} (held {int(age)}s)"
                            )
                            # Mark it as killed by removing from tracking
                            _conn_acquired_times.pop(cid, None)
                            _conn_acquired_stacks.pop(cid, None)
                            _active_conn_count = max(0, _active_conn_count - 1)
                            killed += 1
                        except Exception as e:
                            log_debug(f"[db] Failed to cleanup connection {cid}: {e}")

                    if killed > 0:
                        log_info(
                            f"[db] Emergency cleanup killed {killed} connections, new pool usage: {_active_conn_count}/{maxsize}"
                        )

            except asyncio.CancelledError:
                log_debug("[db] Pool cleanup task cancelled")
                break
            except Exception as e:
                log_error(f"[db] Pool cleanup task error: {e}")
                await asyncio.sleep(10)

    # Start the background task
    try:
        task = asyncio.create_task(cleanup_monitor())
        log_info("[db] Database pool cleanup task started")
        return task
    except RuntimeError:
        # No running event loop - this is fine during initialization
        log_debug("[db] Could not start pool cleanup task (no running event loop)")
        return None


async def execute_query(query: str, params: tuple = ()) -> list:
    """Execute a SQL query and return the results."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                results = await cur.fetchall()
        return results
    except Exception as e:
        log_error(f"[execute_query] Error executing query: {query}, Error: {e}")
        raise
