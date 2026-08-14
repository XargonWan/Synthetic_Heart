Database Connection Management
===============================

Overview
--------

The Synthetic Heart project now uses a **shared database handler in** ``core/db.py``
for all runtime database access. The default runtime backend is PostgreSQL,
while a preserved legacy MariaDB source may still be consulted only during the
first-boot migration flow.

Connection Pool Configuration
------------------------------

The connection pool is configured through environment variables in `.env-dev`:

.. code-block:: bash

    DB_POOL_MINSIZE=1          # Minimum connections kept alive
    DB_POOL_MAXSIZE=5          # Maximum concurrent connections (reduced from 8 to prevent bio_manager timeouts)
    DB_CONNECTION_TIMEOUT=10   # Timeout for acquiring a connection (seconds)

**Runtime Database**: PostgreSQL is the primary runtime backend. The shared
handler can also manage named Postgres pools for subsystems such as SOUL, so
runtime code does not own driver calls directly.

**Bio Manager Timeout Fix**: The pool size was reduced from 8 to 5 connections to prevent ``TimeoutError`` in the bio_manager plugin. The bio_manager performs synchronous database operations from async contexts, and a smaller pool size ensures connections are always available without blocking.

Single Event Loop Architecture
-------------------------------

**Critical Fix Applied**: The application now uses a **single event loop** for all async operations, including database initialization.

Previously, the application created multiple event loops:
1. One for database initialization (via ``asyncio.run(initialize_database())``)
2. One for the main application (via ``asyncio.run(start_application())``)

Each event loop maintained its own connection pool, leading to:
- Pool 1: 8 connections
- Pool 2: 8 connections
- Total: 16 connections (exceeding limits)

**Solution**: Moved database initialization into the main async function, eliminating the second event loop. Now there is **only one pool with 5 maximum connections**.

Connection Acquisition Pattern
-------------------------------

All database access must follow the async context manager pattern:

.. code-block:: python

    from core.db import get_conn_ctx
    
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, params)
            results = await cursor.fetchall()

**Key Points**:
- Use ``get_conn_ctx()`` context manager for automatic cleanup
- Connections are **automatically released** to the pool when the context exits
- This pattern prevents connection leaks
- Runtime code must not call ``aiomysql.connect()``, ``asyncpg.connect()``, or
    ``asyncpg.create_pool()`` directly; those flows belong in ``core/db.py``.

Named Postgres Pools
--------------------

Some subsystems need a dedicated Postgres target while still respecting the
shared DB ownership rule. For that case, use the named-pool helpers in
``core/db.py`` rather than creating driver pools locally.

This is how the SOUL repository now acquires its Postgres pool.

Connection Release Mechanism
-----------------------------

The ``_ConnProxy`` wrapper in ``core/db.py`` handles automatic connection release:

.. code-block:: python

    class _ConnProxy:
        """Wraps a raw connection to ensure proper cleanup."""
        
        def __init__(self, raw_conn, pool):
            self._raw_conn = raw_conn
            self._pool = pool
        
        async def __aenter__(self):
            return self
        
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            """Synchronously release connection back to pool."""
            if self._pool:
                self._pool.release(self._raw_conn)  # Synchronous release!

**Critical Detail**: Connection release is **synchronous** (not async). This ensures connections are immediately available for reuse rather than waiting for an async task to complete.

Common Mistakes
---------------

❌ **Mistake 1**: Using sync connection fetching without await

.. code-block:: python

    # WRONG - Returns a coroutine, never awaited!
    conn = get_conn()
    
    # This leaves the connection "checked out" forever, causing pool exhaustion.

✅ **Correct Pattern**:

.. code-block:: python

    # RIGHT - Use context manager
    async with get_conn_ctx() as conn:
        # ... use connection ...
    # Automatically released when exiting context

❌ **Mistake 2**: Holding connections for too long

.. code-block:: python

    # WRONG - Connection held for entire LLM response
    async with get_conn_ctx() as conn:
        # ... fetch user data ...
        response = await llm.generate(prompt)  # Long operation!
        # Connection blocked while waiting for LLM

✅ **Correct Pattern**:

.. code-block:: python

    # RIGHT - Fetch data, release connection, then process
    async with get_conn_ctx() as conn:
        user_data = await fetch_user_data(conn)
    
    # Connection released here
    response = await llm.generate(prompt)  # No connection held

❌ **Mistake 3**: ConfigVar used as string directly

.. code-block:: python

    # WRONG - ConfigVar is an object, not a string
    fallback_msg = FAILED_MESSAGE_TEXT
    await send_message(chat_id, text=fallback_msg)  # Fails with len() error

✅ **Correct Pattern**:

.. code-block:: python

    # RIGHT - Extract value from ConfigVar
    fallback_msg = FAILED_MESSAGE_TEXT
    if hasattr(fallback_msg, 'get_value'):
        fallback_msg = fallback_msg.get_value()
    fallback_msg = str(fallback_msg)
    await send_message(chat_id, text=fallback_msg)

Monitoring Connection Pool
---------------------------

Check connection pool status in logs:

.. code-block:: bash

    # Watch for pool creation messages
    docker logs synth-dev 2>&1 | grep "Creating pool"
    
    # Should show only ONE pool creation:
    # [INFO] [db.py:151] [db] Creating pool with minsize=1 maxsize=8

Watch for connection acquisition/release:

.. code-block:: bash

    # Monitor connections in real-time
    docker logs synth-dev 2>&1 | tail -f | grep -E "Connection acquired|Connection released"

Detecting Connection Leaks
---------------------------

**Symptoms of Connection Leak**:
- Repeated "TIMEOUT acquiring connection" errors
- Errors like "(1040, 'Too many connections')" from database
- Pool unable to handle concurrent requests
- Connection exhaustion within seconds/minutes

**Investigation Steps**:

1. Check if connection is being properly released:

.. code-block:: bash

    docker logs synth-dev 2>&1 | grep "Connection released" | wc -l
    docker logs synth-dev 2>&1 | grep "Connection acquired" | wc -l
    
    # These numbers should be roughly equal

2. Check for "coroutine was never awaited" warnings:

.. code-block:: bash

    docker logs synth-dev 2>&1 | grep -i "coroutine"

3. Look for stuck connections:

.. code-block:: bash

    docker exec synth-db mariadb -u synth -p'PASSWORD' -e "SHOW PROCESSLIST;"

Performance Tuning
------------------

**Pool Size Considerations**:

- **Too Small** (MAXSIZE=1-2): Bottleneck, requests blocked waiting for connection
- **Too Large** (MAXSIZE=50+): Uses too many database resources
- **Optimal** (MAXSIZE=4-8): Balances concurrency with resource usage

Formula: ``MAXSIZE = min(database_max_connections / 20, 8)``

For MariaDB with 151 max connections:
``MAXSIZE = min(151 / 20, 8) = min(7.55, 8) = 7 or 8``

Testing Connection Pool
-----------------------

Quick test to verify pool is working:

.. code-block:: bash

    python3 -c "
    import asyncio
    from core.db import get_conn_ctx
    
    async def test():
        for i in range(3):
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute('SELECT 1')
                    result = await cursor.fetchone()
                    print(f'Query {i}: {result}')
    
    asyncio.run(test())
    "

Expected output:
- 3 successful queries
- No connection errors
- Connection acquire/release logs show proper cleanup

Backup & read-only query access
-------------------------------

The runtime database can be backed up and queried through both the WebUI/HTTP
API and the ``synth-db`` MCP server.

**HTTP API** (see :doc:`api_endpoints` for full parameters):

- ``POST /api/database/backup`` — full runtime backup; returns the generated
  filename. The WebUI Settings tab triggers this and then downloads the archive
  to the browser via ``GET /api/database/backup/download?filename=<name>``.
- ``POST /api/database/backup/table`` — backup a **list** of tables
  (``{"tables": ["a", "b"]}``). Table names are sanitised to identifier
  characters; any invalid name rejects the whole request.
- ``POST /api/database/query`` — run a **read-only** ``SELECT``/``WITH`` query.
  Non-read-only statements (INSERT/UPDATE/DELETE/DDL/multiple statements) are
  rejected with ``400``. ``limit`` is clamped to ``1..1000`` (default ``200``).

Both backup handlers write to ``SYNTH_BACKUPS_DIR`` and the download handler
confines the requested filename to that directory (path traversal → ``403``).

**synth-db MCP server** (``mcp_servers/synth_db.py``):

- ``run_select("SELECT ...")`` — read-only query, row cap ``1..200``.
- ``backup_database(confirm=False, target=None)`` — full backup; dry-run unless
  ``confirm=True``.
- ``backup_table(tables=[...], confirm=False, target=None)`` — backup a list of
  tables. Table names are sanitised; an invalid name rejects the request. Dry-run
  unless ``confirm=True``.

References
----------

- **aiomysql Documentation**: https://aiomysql.readthedocs.io/
- **AsyncIO Context Managers**: https://docs.python.org/3/library/contextlib.html#async-context-managers

Recent Fixes
------------

**Bio Manager Timeout Fix (November 2025)**:

The bio_manager plugin was experiencing ``TimeoutError`` when retrieving user profiles during prompt injection. Root causes and solutions:

**Issues Fixed**:
- **Database Pool Contention**: Pool size reduced from 8 to 5 connections to prevent exhaustion
- **Table Initialization Deadlock**: Added caching to prevent repeated ``_ensure_table()`` calls
- **Async/Sync Mixing**: Converted ``get_static_injection()`` to async to prevent ``run_coroutine_threadsafe()`` timeouts

**Technical Details**:
- **Before**: Sync ``get_static_injection()`` → sync ``get_bio_light()`` → ``_run()`` → ``run_coroutine_threadsafe()`` → 30s timeout
- **After**: Async ``get_static_injection()`` → ``await _get_bio_light_async()`` → direct async DB operations

**Configuration Changes**:
.. code-block:: bash

    # .env-dev
    DB_POOL_MAXSIZE=5  # Reduced from 8

**Code Changes**:
- ``plugins/bio_manager.py``: Added ``_get_bio_light_async()``, ``_update_last_accessed_async()``
- ``plugins/bio_manager.py``: Converted ``get_static_injection()`` to ``async def``
- **MariaDB Connection Pooling**: https://mariadb.com/kb/en/

**Agent Lane action deadlock (August 2026)**:

The same ``run_coroutine_threadsafe()`` deadlock resurfaced on the *action* path: the Agent Lane
(``core/agent_tool_executor.py`` → ``core/action_parser.run_action``) calls ``BioPlugin.execute_action``
directly on the event-loop thread. The old sync ``execute_action`` went through ``_run()``, which
scheduled its coroutine on the very loop it was blocking, deadlocking until the 30s ``TimeoutError``
("Error in _run: " with an empty message, repeated every 30s — e.g. the ``bio_full_request`` tool
with ``targets: "grillo,karada,scarlett,scarlet"``).

- **Root cause**: ``_run()`` used ``run_coroutine_threadsafe(coro, loop).result(timeout=30)`` when the
  loop is running. From the loop thread itself, the scheduled coroutine can never run while the loop
  thread blocks in ``.result()``.
- **Fix**: ``execute_action`` is now ``async def`` and uses loop-safe async helpers
  (``_get_bio_light_async``, ``_get_bio_full_async``, ``_update_bio_fields_async``,
  ``_ensure_user_exists_async``, ``_resolve_target_async``); ``action_parser`` already awaited coroutine
  results from ``execute_action``, so no caller change was needed. ``_run()`` additionally gained a
  defensive branch: when invoked from the loop thread it delegates to a worker thread instead of
  deadlocking, and its error log now includes the exception type (a bare ``TimeoutError`` has an empty
  ``str()``).
- **Bonus fix**: ``bio_full_request`` now normalizes a comma-separated string ``targets`` into a list
  (LLMs often emit a string, which previously iterated character-by-character).
- **Tests**: ``tests/test_bio_manager_async.py`` proves the action path never touches the ``_run``
  bridge and runs inside a live event loop without deadlocking.
