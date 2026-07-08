import os
import signal
import sys
import asyncio
from pathlib import Path


def _load_repo_env_defaults() -> None:
    env_path = Path(__file__).resolve().with_name(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_repo_env_defaults()

from core.db import init_db, test_connection, get_conn_ctx, _get_db_type  # noqa: E402

# from core.blocklist import init_blocklist_table  # Now handled by blocklist plugin
from core.logging_utils import (  # noqa: E402
    log_debug,
    log_info,
    log_warning,
    setup_logging,
    log_error,
)

# Import exposed variables EARLY to ensure correct type registrations
# before any circular import chains or dynamic calls can cause premature registration
try:
    import core.variables_engine  # noqa: F401
except Exception as e:
    print(f"[main] Warning: Failed to import variables_engine early: {e}", flush=True)

# Global restart flag
_restart_requested = False
_restart_event = None
# Global flag to preserve dev components state across restarts
_dev_components_enabled = False

# Set by signal_handler, awaited by the main loop to perform an orderly shutdown.
# Deliberately NOT handled by raising SystemExit inside the raw OS signal frame:
# uvicorn (interface/openai_api_server.py) installs its own SIGINT/SIGTERM capture
# around `await server.serve()` and re-raises the signal to whatever handler was
# previously registered once it unwinds, so a synchronous sys.exit() here fires deep
# inside that task's stack. SystemExit is a BaseException, so it isn't caught by
# asyncio's per-task handling and blows straight out of run_forever() instead of
# letting asyncio.run()'s normal _cancel_all_tasks() sweep cancel every background
# task in one clean, orderly pass.
_shutdown_event = None
_main_event_loop = None


def request_restart():
    """Request a graceful restart of the application."""
    global _restart_requested, _restart_event
    log_info("[main] Restart requested")
    _restart_requested = True
    if _restart_event:
        _restart_event.set()


def set_dev_components_enabled(enabled: bool):
    """Set whether dev components should be loaded (preserved across restarts)."""
    global _dev_components_enabled
    _dev_components_enabled = enabled
    log_info(f"[main] Dev components {'ENABLED' if enabled else 'DISABLED'} globally")


def are_dev_components_enabled() -> bool:
    """Check if dev components are enabled."""
    return _dev_components_enabled


def cleanup_components():
    """Clean up all registered components (engines, plugins, interfaces)."""
    try:
        log_debug("[main] Starting component cleanup...")

        # Let the core initializer handle cleanup of all registered components

        # Cleanup Cortex engines
        from core.cortex_registry import get_cortex_registry

        registry = get_cortex_registry()
        for engine_name in registry.get_available_engines():
            try:
                engine_instance = registry.get_engine(engine_name)
                if engine_instance and hasattr(engine_instance, "cleanup"):
                    engine_instance.cleanup()
                    log_debug(f"[main] Cleaned up engine: {engine_name}")
            except Exception as e:
                log_warning(f"[main] Failed to cleanup engine {engine_name}: {e}")

        log_info("[main] Component cleanup completed")

    except Exception as e:
        log_warning(f"[main] Component cleanup failed: {e}")


async def stop_interfaces() -> None:
    """Give each interface a chance to stop its own background tasks in an
    orderly, isolated sequence before asyncio.run()'s blanket task cancellation
    fires. Without this, an interface's own cleanup (e.g. python-telegram-bot's
    app.stop() awaiting its internal fetcher task) races against that same
    blanket sweep independently cancelling the same tasks, producing noisy but
    harmless CancelledError tracebacks. A per-interface timeout keeps one stuck
    interface from stalling the rest of shutdown.
    """
    import inspect

    from core.core_initializer import INTERFACE_REGISTRY

    for name, interface_instance in list(INTERFACE_REGISTRY.items()):
        stop_method = getattr(interface_instance, "stop", None)
        if not callable(stop_method):
            continue
        try:
            result = stop_method()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=10)
            log_debug(f"[main] Stopped interface: {name}")
        except TimeoutError:
            log_warning(f"[main] Interface '{name}' did not stop within 10s")
        except Exception as e:
            log_warning(f"[main] Error stopping interface '{name}': {e}")


def signal_handler(signum, frame):
    """Request a graceful shutdown; never blocks or exits from this raw signal frame.

    See the `_shutdown_event` comment above for why this can't just call
    cleanup_components()/sys.exit() directly.
    """
    log_info(f"[main] Received signal {signum}, requesting graceful shutdown...")

    if _main_event_loop is not None and _shutdown_event is not None:
        _main_event_loop.call_soon_threadsafe(_shutdown_event.set)
    else:
        # Signal arrived before the event loop was up (e.g. during early startup) -
        # nothing async is running yet, so an immediate exit is safe here.
        sys.exit(0)


async def initialize_database():
    """Initialize database with proper async handling."""
    log_info("[main] initialize_database() started")

    # Verifica dei permessi dell'utente del database
    async def check_permissions():
        log_debug("[main] Checking database permissions...")
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    if _get_db_type() == "postgres":
                        await cur.execute(
                            """
                            SELECT
                                current_user AS role_name,
                                current_database() AS database_name,
                                has_database_privilege(current_user, current_database(), 'CONNECT') AS can_connect,
                                has_database_privilege(current_user, current_database(), 'CREATE') AS can_create,
                                has_schema_privilege(current_user, 'public', 'USAGE') AS public_usage,
                                has_schema_privilege(current_user, 'public', 'CREATE') AS public_create
                            """
                        )
                    else:
                        await cur.execute("SHOW GRANTS FOR CURRENT_USER()")
                    grants = await cur.fetchall()
                    log_debug("[main] Database permissions check completed")
                    return grants
        except Exception as e:
            log_error(f"[main] Error checking database permissions: {repr(e)}")
            raise

    try:
        grants = await check_permissions()
        log_info(f"[main] Database user permissions: {grants}")

        log_info("[main] Testing database connection...")
        if not await test_connection():
            log_error("[main] Database connection test failed")
            return False
        log_info("[main] Database connection test passed")

        log_info("[main] Initializing database schema...")
        await init_db()
        log_info("[main] Database schema initialized")

        # Persist bootstrap configurations to DB after initialization
        log_debug("[main] Persisting bootstrap configurations...")
        from core.config_manager import config_registry

        await config_registry.persist_bootstrap_configs()
        log_debug("[main] Bootstrap configurations persisted")

        # NOTE: load_all_from_db() is called later in core_initializer.initialize_all()
        # after all variable registrations are complete. Do not call it here as it would
        # skip loading persona configurations due to incomplete registration.

        # Blocklist table now handled by blocklist plugin
        # log_info("[main] Initializing blocklist table...")
        # await init_blocklist_table()
        # log_info("[main] Blocklist table initialized")

        log_info("[main] Database initialization completed successfully!")
        return True
    except Exception as e:
        log_error(f"[main] Error in initialize_database(): {repr(e)}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Docker stop

    setup_logging()
    log_info("[main] Starting synth application...")

    # 🌐 Show where the Webtop/VNC interface is available
    host = os.environ.get("WEBVIEW_HOST", "localhost")
    port = os.environ.get("WEBVIEW_PORT", "3000")
    log_info(f"[vnc] Webtop GUI available at: http://{host}:{port}")

    log_info("[main] Starting bot initialization...")

    async def start_application():
        """Start the application and handle restart requests."""
        global _restart_requested, _restart_event, _shutdown_event, _main_event_loop

        # Test DB connectivity and initialize tables with retry mechanism
        # This must be done in the main event loop to avoid creating separate pools

        # Register main loop with notifier so it doesn't create new event loops
        from core.notifier import _set_main_loop

        loop = asyncio.get_running_loop()
        _set_main_loop(loop)
        _main_event_loop = loop
        _shutdown_event = asyncio.Event()

        max_retries = 30
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                log_info(
                    f"[main] Attempting database connection (attempt {attempt + 1}/{max_retries})..."
                )

                # Conditional execution of legacy MariaDB→Postgres migration
                if os.getenv("EXECUTE_MARIADB_POSTGRES_MIGRATION", "false").lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                ):
                    try:
                        from core.db_cutover import (
                            resume_legacy_mysql_cutover_if_needed,
                        )

                        migrated = await resume_legacy_mysql_cutover_if_needed()
                        if migrated:
                            log_info(
                                "[main] Legacy MySQL to Postgres cutover completed"
                            )
                    except Exception as e:
                        log_error(f"[main] Legacy DB cutover failed: {e}")
                        raise
                else:
                    log_info(
                        "[main] Skipping legacy MariaDB→Postgres migration (set EXECUTE_MARIADB_POSTGRES_MIGRATION=true to enable)"
                    )

                # Initialize database async
                if await initialize_database():
                    break
                else:
                    raise Exception("Database initialization failed")

            except Exception as e:
                if attempt < max_retries - 1:
                    log_warning(
                        f"[main] Database connection attempt {attempt + 1} failed: {e}"
                    )
                    log_info(f"[main] Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    log_error(
                        f"[main] Critical error during database initialization after {max_retries} attempts: {e}"
                    )
                    sys.exit(1)

        try:
            from core.db_backup import start_database_backup_scheduler

            if start_database_backup_scheduler() is not None:
                log_info("[main] Embedded database backup scheduler started")
        except Exception as e:
            log_warning(f"[main] Failed to start database backup scheduler: {e}")

        while True:
            _restart_requested = False
            _restart_event = asyncio.Event()

            # Initialize core components - they will auto-discover and load all interfaces/plugins/engines
            try:
                log_info("[main] Initializing core components...")
                from core.core_initializer import core_initializer

                # Restore dev components state if it was enabled before restart
                if _dev_components_enabled:
                    log_info("[main] Restoring dev components enabled state...")
                    core_initializer.enable_dev_components(True)

                await core_initializer.initialize_all()
                log_info("[main] Core components initialized successfully")

                # Start webui server if available
                try:
                    from core.core_initializer import INTERFACE_REGISTRY

                    # If discovery populated the registry, use that instance.
                    if "synth_webui" in INTERFACE_REGISTRY:
                        webui_interface = INTERFACE_REGISTRY["synth_webui"]
                        if hasattr(webui_interface, "start"):
                            await webui_interface.start()
                            log_info("[main] WebUI interface started")
                        elif hasattr(webui_interface, "start_server_async"):
                            webui_interface.start_server_async()
                            log_info("[main] WebUI server started")
                    else:
                        # Fallback: attempt to import and initialize the core.webui module
                        # This ensures the Web UI is created even if discovery missed it
                        try:
                            import importlib

                            log_info(
                                "[main] synth_webui not found in INTERFACE_REGISTRY - attempting direct initialization"
                            )
                            webui_mod = importlib.import_module("core.webui")
                            if hasattr(webui_mod, "initialize_interface"):
                                webui_mod.initialize_interface()
                                # If the module created the instance it should be in the registry now
                                if "synth_webui" in INTERFACE_REGISTRY:
                                    webui_interface = INTERFACE_REGISTRY["synth_webui"]
                                    if hasattr(webui_interface, "start"):
                                        await webui_interface.start()
                                        log_info(
                                            "[main] WebUI interface started (fallback initialization)"
                                        )
                                    elif hasattr(webui_interface, "start_server_async"):
                                        webui_interface.start_server_async()
                                        log_info(
                                            "[main] WebUI server started (fallback initialization)"
                                        )
                                else:
                                    log_warning(
                                        "[main] core.webui.initialize_interface() did not register synth_webui"
                                    )
                            else:
                                log_warning(
                                    "[main] core.webui has no initialize_interface() - cannot initialize Web UI"
                                )
                        except Exception as e:
                            log_warning(
                                f"[main] Fallback initialization of synth_webui failed: {e}"
                            )
                except Exception as e:
                    log_warning(f"[main] Could not start webui interface: {e}")

                # Start message queue consumer
                from core import message_queue

                asyncio.create_task(message_queue.run())
                log_info("[main] Message queue consumer started")
            except Exception as e:
                log_error(
                    f"[main] Critical error initializing core components: {repr(e)}"
                )
                import traceback

                traceback.print_exc()
                sys.exit(1)

            log_info("[main] All components auto-discovered and initialized")

            # 🎯 Display startup summary after all components are ready (this should be the last message)
            log_info("[main] All components initialized, displaying startup summary...")
            core_initializer.display_startup_summary()

            # Also display a quick resume even if some components are still loading
            resume = core_initializer.get_system_resume()
            log_info(
                f"[main] 🎯 QUICK STATUS: {resume['successful']}/{resume['total_components']} components loaded, {resume['total_actions']} actions available"
            )

            # Keep the application running indefinitely (or until restart requested)
            log_info(
                "[main] Application startup completed successfully - entering main loop"
            )
            try:
                # Wait for a restart request or a graceful shutdown request,
                # whichever comes first.
                restart_wait = asyncio.create_task(_restart_event.wait())
                shutdown_wait = asyncio.create_task(_shutdown_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {restart_wait, shutdown_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in pending:
                        task.cancel()

                if shutdown_wait in done:
                    log_info("[main] Shutdown requested - cleaning up...")
                    await stop_interfaces()
                    cleanup_components()
                    log_info("[main] Shutdown cleanup complete - exiting...")
                    break

                if _restart_requested:
                    log_info(
                        "[main] 🔄 Restart requested - cleaning up and restarting..."
                    )

                    # Cleanup components
                    cleanup_components()

                    # Clear registries
                    from core.core_initializer import (
                        INTERFACE_REGISTRY,
                        PLUGIN_REGISTRY,
                    )

                    INTERFACE_REGISTRY.clear()
                    PLUGIN_REGISTRY.clear()

                    # Clear Cortex registry
                    from core.cortex_registry import get_cortex_registry

                    cortex_registry = get_cortex_registry()
                    cortex_registry._engines.clear()

                    log_info("[main] ✅ Cleanup completed - restarting application...")
                    await asyncio.sleep(1)  # Brief pause before restart
                    continue  # Loop back to restart

            except KeyboardInterrupt:
                log_info("[main] Received shutdown signal, exiting...")
                await stop_interfaces()
                cleanup_components()
                break

    # Run the async application
    asyncio.run(start_application())
