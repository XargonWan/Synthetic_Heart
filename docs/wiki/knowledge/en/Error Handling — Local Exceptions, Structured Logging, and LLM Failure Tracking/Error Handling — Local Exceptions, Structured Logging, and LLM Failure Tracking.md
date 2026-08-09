---
kind: error_handling
name: Error Handling — Local Exceptions, Structured Logging, and LLM Failure Tracking
category: error_handling
scope:
    - '**'
source_files:
    - core/logging_utils.py
    - core/llm_failure_log.py
    - core/variables_engine.py
    - interface/fluxer_interface/fluxer_interface.py
    - plugins/radio_host/azuracast_client.py
    - core/action_parser.py
    - core/command_registry.py
    - core/config.py
---

Synthetic Heart does not use a single centralized error-type hierarchy or framework-wide middleware. Instead, error handling is distributed across the codebase with three dominant patterns: (1) domain-specific Exception subclasses raised locally, (2) Python built-in exceptions for validation/lookup failures, and (3) structured logging via `core.logging_utils` that captures tracebacks and optionally notifies through LogChat.

**Exception types and conventions**
- Domain-specific errors are defined close to their usage site rather than in a shared package:
  - `ValidationError(ValueError)` in `core/variables_engine.py` for exposed-variable validation failures.
  - `_FatalGatewayError(Exception)` in `interface/fluxer_interface/fluxer_interface.py` to signal unrecoverable gateway auth failures.
  - `AzuraCastError(Exception)` in `plugins/radio_host/azuracast_client.py` for HTTP/WebSocket failures against AzuraCast.
  - `NetworkError`, `TelegramError` defined inside interface modules for transport-layer issues.
- Validation and configuration errors consistently raise built-ins (`ValueError`, `KeyError`, `FileNotFoundError`, `PermissionError`) from registries and config modules (e.g. `command_registry.py`, `config.py`, `auris_registry.py`, `animation_uploads.py`).
- Abstract base classes enforce implementation by raising `NotImplementedError` on unimplemented hooks in `core/ai_plugin_base.py`.

**Structured logging as the primary error surface**
- All modules import `log_debug`, `log_info`, `log_warning`, `log_error` from `core/logging_utils.py`. The logger writes to `logs/synth.log` plus an additive `logs/synth_errors.log` for quick scanning.
- `log_error(msg, exc=None, log_file=None)` automatically appends a full traceback when an exception object is passed; many callers do this around network calls and DB operations.
- A separate per-component log file can be targeted via the `log_file` parameter, enabling isolated logs (e.g. `webui.log`, `matrix.log`).
- Startup-time logging configuration is resilient: if the file handler cannot open the log directory, it falls back to stdout/stderr so the process never crashes during initialization.
- Retention is enforced at every rollover via `core.log_archive.enforce_retention`, compressing old days and pruning beyond `LOG_RETENTION_DAYS`.

**LLM failure tracking subsystem**
- `core/llm_failure_log.py` provides a dedicated, schema-backed table (`llm_failure_log`) plus an in-memory fallback for transient DB outages.
- Failures are classified into stable codes (`timeout`, `provider_unreachable`, `delivery_failed`, `malformed_json`, `correction_loop`, `invalid_action`, etc.) via `infer_failure_code`, which inspects the reason string and optional `correction_context` / `metadata`.
- `record_failure_entry` attempts DB persistence first, then silently falls back to an in-memory list and logs a warning — callers never need to handle the fallback.
- `mark_failure_processed` sets `metadata.processed_by_recovery = True` to guarantee the recovery plugin never re-processes the same failure (anti-loop invariant).
- Query APIs support filtering by `failure_code`, `stage`, and free-text search, with pagination and sort order.

**Propagation and recovery patterns**
- Network/client layers wrap low-level exceptions into domain errors (e.g. `aiohttp.ClientError` → `AzuraCastError`) and log warnings before returning or propagating.
- Broad `except Exception` blocks appear in hot paths (notably `core/action_parser.py`) where defensive parsing must never crash the message pipeline; they log the full traceback and return safe defaults.
- Configuration and registry lookups raise descriptive `ValueError`/`KeyError` messages naming the missing key/engine, making them easy to catch and surface in the WebUI.
- No `try/except` global handlers or `sys.excepthook` overrides were found; unhandled exceptions bubble up to the event loop and are captured by the structured logger wherever they occur.

**No panics/recover strategy**
- The codebase does not use `panic`/`recover` (Python equivalent would be top-level `try/except BaseException`), nor any custom middleware that intercepts all exceptions uniformly. Error containment is achieved through localized try/except blocks and resilient logging.