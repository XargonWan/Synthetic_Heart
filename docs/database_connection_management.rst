Database Connection Management
===============================

Overview
--------

The Synthetic Heart project uses an **aiomysql connection pool** for managing asynchronous database connections. The pool is carefully sized to avoid exhausting database resources while maintaining performance.

Connection Pool Configuration
------------------------------

The connection pool is configured through environment variables in `.env-dev`:

.. code-block:: bash

    DB_POOL_MINSIZE=1          # Minimum connections kept alive
    DB_POOL_MAXSIZE=8          # Maximum concurrent connections
    DB_CONNECTION_TIMEOUT=10   # Timeout for acquiring a connection (seconds)

**Database Limit**: MariaDB/MySQL has a system-wide connection limit (default 151 on standard deployments). The pool is sized to stay well below this limit.

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

**Solution**: Moved database initialization into the main async function, eliminating the second event loop. Now there is **only one pool with 8 maximum connections**.

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

References
----------

- **aiomysql Documentation**: https://aiomysql.readthedocs.io/
- **AsyncIO Context Managers**: https://docs.python.org/3/library/contextlib.html#async-context-managers
- **MariaDB Connection Pooling**: https://mariadb.com/kb/en/
