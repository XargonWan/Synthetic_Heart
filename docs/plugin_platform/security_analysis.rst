G. Security Analysis
====================

Threat model
------------

**The adversary** is a third-party extension (or a compromised official one) that
tries to hook directly into the Core: call internal functions, read secrets,
touch the database, bypass the message chain, or interfere with other
extensions. **Out of scope**: attacks on the kernel or container runtime
(a process-escape), and an attacker with root on the host.

What is true today (Verified)
-----------------------------

* Extensions are Python modules loaded **in the Core process** with full access
  to ``core.*``, the database, the environment and the filesystem.
  ``core/core_initializer.py`` rejects a plugin source only if it contains a
  write to ``message_queue._queue``: a text scan, not a control.
* ``core/action_safety.py`` trusts ``security_level`` and ``external_effects``
  **declared by the plugin itself**.
* No permissions, no import restrictions, no per-plugin credentials, no network
  policy, no resource limits.
* Existing narrow guards worth reusing: ``outbound_file_utils`` (sandbox roots),
  ``url_safety``, ``delivery_guard``, ``webui_auth`` (WebUI tokens),
  ``external_endpoints/crypto`` (endpoint secrets).

Why in-process permissions are not enough
-----------------------------------------

API permissions do not provide execution isolation (the brief says as much).
Inside one Python process an extension can read ``sys.modules``, use ``gc`` to
reach live objects, call ``ctypes``, import any ``core`` module and open the
database with the Core's credentials. Linting for forbidden imports is a
**development aid**, not a guarantee. Hence the **Decision**: every extension
runs in its own process and only the gateway connects it to the Core.

Trust boundary and controls
---------------------------

.. list-table::
   :header-rows: 1
   :widths: 24 38 38

   * - Vector
     - Control (Proposal)
     - Residual risk
   * - Python import of ``core.*`` or other extensions
     - Separate process; the Core is not on the extension's ``sys.path``;
       dependencies come from the extension's own environment.
     - None from import once the process is separate; an extension can still ship
       its own code that does anything inside its sandbox.
   * - Environment variables and secrets
     - Clean environment per process; secrets reach an extension only through
       ``config`` for its own keys.
     - A secret granted to an extension is visible to it.
   * - Database
     - No credentials; the Core is the only client; extension data only through
       ``storage`` (parameterised, quota-limited, namespaced).
     - Flaws in the storage API itself (injection, quota bypass).
   * - Filesystem
     - Dedicated data directory per extension; a dedicated OS user without read
       access to the Core's ``.env`` and data; reuse the sandbox-root idea of
       ``outbound_file_utils``/``AGENT_FS_ROOTS``.
     - Depends on OS support; Linux first, guarded secondary path elsewhere
       (``sys.platform``). **Unknown**: how much is enforceable on non-Docker
       Windows.
   * - Network
     - Per-extension policy declared in the manifest and granted by the user
       (``url_safety`` as precedent); egress filtering where the platform allows.
     - Without OS-level egress rules a process can still open sockets.
   * - Process execution
     - Declared capability; a sandboxed process may still spawn children
       (Minecraft spawns Node, Vox/Auris spawn helpers, 9 plugin files use
       ``subprocess``).
     - Children inherit the sandbox; needs verification per adapter.
   * - IPC between extensions
     - None direct; only through the gateway and declared dependencies.
     - Message content shared via the Core is visible to the receiver.
   * - Message chain
     - Inbound goes through ``interfaces.inbound`` which calls
       ``message_queue.enqueue``; outbound is invoked by the Core. No other entry
       exists.
     - Flooding: rate limits and quotas at the gateway.
   * - Gateway abuse
     - Per-instance token, capability check on every call, argument validation,
       rate limits, timeouts, size limits.
     - Token theft by a same-user process: separate OS users.
   * - Resource exhaustion
     - Supervisor with restart backoff; CPU/memory limits where the OS offers
       them.
     - Without cgroups limits are advisory.
   * - Privileged operations
     - Each is a named capability granted explicitly, shown to the user at
       install time.
     - Users approve too much (permission fatigue).

Trust models for official vs community extensions
-------------------------------------------------

**Decision**: official extensions use **exactly** the same API and sandbox as
community extensions. Signing and publisher identity decide *what may be
installed*, never *what an installed extension may do*. If an official extension
cannot do its job through the public API, that is a signal that either an API is
missing (propose it) or the capability is large enough to belong in the Core.

Supply chain: signed index and packages, publisher keys, per-repository trust
levels, explicit user confirmation for untrusted repositories, hashes recorded at
install. **Unknown**: signing scheme and key distribution (see
:doc:`open_questions`).

Explicitly outside the guarantee
--------------------------------

* A process escape or kernel vulnerability.
* An extension abusing a capability it was legitimately granted (for example
  reading the chat history it was allowed to read).
* Extensions that need the model files of another extension: they must declare
  the dependency; the Core does not police semantic misuse.
* Per-extension **containers** are not part of this design. The only container
  SyntH ships for an extension is Zen's, as an external service.

What must be built to make the boundary real
--------------------------------------------

#. The extension gateway and the process launcher with a clean environment, a
   dedicated user and data directory (M2 in :doc:`migration_plan`).
#. A **security test** with a deliberately hostile extension: it must fail to read
   ``.env``, connect to the database, import ``core`` and bypass the queue
   (see :doc:`testing_strategy`).
#. Permission verification inside the gateway, not in the SDK.

State after M2
--------------

What is enforced today, as opposed to proposed above (details in
:doc:`extension_runtime`):

* **Enforced and tested with real hostile processes:** the process boundary and the
  pipe as identity, a clean environment, no ``import core`` by accident, capability
  checks on every call (declared **and** granted, revocable at once), strict
  parameter validation, rate limiting, size limits, storage namespacing and quotas,
  containment of crashes, and killing an extension that is stuck or that the gateway
  has cut off.
* **Enforced but never run:** the dedicated numeric user per extension (needs root).
  Without it an extension runs as the Core's user and can read ``.env`` and signal
  the Core. The test exists in ``tests/test_extensions_privileged.py``; it must be run
  as root (in the container) before the isolation is relied upon.
* **Found while building it:** in the container the start-up script runs
  ``chmod a+rwx -R /config``. The runtime now repairs the permissions of its own
  tree before launching anything (``secure_runtime_tree``), otherwise an extension
  could rewrite ``grants.json``. The blanket ``chmod`` itself is unchanged.
* **Not enforced:** network egress, per-extension memory and CPU, and escape from the
  process group with ``setsid``.
* **The message chain (M3):** there is exactly one way in, ``interfaces.inbound``,
  and the Core performs every step itself (history, path tracking, commands,
  ``message_queue``). An extension can only inject into an interface it registered,
  and that it declared in its manifest, on paths matching its declared patterns. It
  cannot register an id already in use. **Residual risk:** it asserts the sender's
  identity, so it can speak as the trainer on *its own* interface (see
  :doc:`extension_runtime`).
* **Files (M3b):** an extension cannot make the Core read a file by naming it. Inbound
  paths must resolve inside the extension's data directory, be regular files owned by
  it, and are checked again on the descriptor that was opened; the Core copies them and
  the message chain only ever sees the copy. Outbound files pass the Core's sandbox rule
  first and are copies, deleted after delivery. Residual: an extension can still send
  the Core any file it owns (an image bomb or a huge document) up to the size limits;
  what the model does with it is the Core's existing handling.
