E. Extension Package and Manifest Proposal
==========================================

Everything here is a **Proposal** unless tagged **Decision**.

Package format (Decision, details open)
---------------------------------------

**One file is one plugin.** An extension is a single archive file: an
**uncompressed zip** ("stored" entries) with a dedicated file extension (name to
be decided). It contains the manifest, code, assets, ``guide.md``, ``icon.png``
and the libraries the extension carries with it.

Why an archive with a manifest inside: identity, version and hash are those of
one file; a repository index only has to list files; installation, removal and
integrity checks act on one unit. Why uncompressed: fast reads, simple hashing
and signing.

Open points the report flags (see :doc:`open_questions`):

* ``zipimport`` loads pure Python but **not native extensions** (``.so``/``.dll``)
  or executables. Installation therefore extracts the archive into a per-
  extension install directory (cache or environment), while the single file
  stays the unit of distribution and integrity.
* Carrying libraries inside the archive (bundled wheels) versus resolving them
  at installation from an index: file size, reproducibility, offline operation.
* A plain directory in the mountable ``plugins_dev`` root with the same
  structure stays valid for development; a command packs it.

Manifest: single file or several?
---------------------------------

Proposal: **one** ``synth-extension.toml`` for identity and requirements, inside
the archive, plus convention sidecars (``guide.md``, ``icon.png``) as today. The
**repository index** (package metadata such as file name, size, hash, signature,
publisher) is a **separate** document kept by the repository, not part of the
manifest. Reasoning: the manifest describes what the package *is and needs*,
which must not change when the file is hosted elsewhere; the index describes how
a repository *distributes* it. Whether the index entry is generated from the
manifest or stored separately is open.

Fields, with the evidence that justifies each
---------------------------------------------

The brief lists candidate fields and says not to assume all are needed. This
table keeps a field only if the current code shows a need.

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Field
     - Kept?
     - Evidence
   * - ``id`` (namespaced, for example ``official/telegram_bot``)
     - Yes
     - Today the key is the module's last name segment; ``get_metadata().name``
       is informational. Collisions on ``INTERFACE_REGISTRY`` overwrite
       silently.
   * - ``name``, ``description``, ``author``, ``license``
     - Yes
     - ``get_metadata()`` already has ``name``, ``description``, ``author``.
   * - ``version``
     - Yes
     - ``version`` exists but is "informational only"; the repository needs the
       exact version of each package.
   * - ``provides`` (capabilities such as ``interface``, ``actions``, ``engine``,
       ``hooks``, ``ui``, ``vessel-adapter``)
     - Yes (**Decision**: no plugin/interface split)
     - Replaces the two scans and the ``type``/``category`` guesses
       (``derive_plugin_category`` derives category from the module path).
   * - ``entry``
     - Yes
     - ``PLUGIN_CLASS`` for plugins, ``initialize_interface`` for interfaces
       today.
   * - ``core_api`` (semver range)
     - Yes
     - No compatibility mechanism exists today.
   * - ``requires.extensions`` (version range, ``optional``)
     - Yes
     - ``depends_on`` is advisory and used by 3 plugins; 158 plugin-to-plugin
       imports show the need. An extension never imports another; it declares
       the dependency (**Decision**), only where it makes sense, for example
       RetroXR depending on the VRM renderer.
   * - ``requires.services`` (Zen, SearXNG, ...)
     - Yes
     - Distinguishes the integration from the external service (Zen, SearXNG).
   * - ``python`` requirements / bundled libraries
     - Yes
     - One global dependency list today; self-containment is a decision.
   * - ``permissions``
     - Yes
     - Nothing exists; ``security_level`` is self-declared.
   * - ``config`` schema
     - Yes
     - ``get_var`` declarations are scattered; keys are global.
   * - ``data`` / ``storage`` declaration
     - Depends on the storage API shape
     - 13 plugins create tables inline; if the API is table-based, the schema
       and migrations are declared here.
   * - ``hooks`` (which Core hooks it implements)
     - Yes
     - Hook discovery is ``hasattr`` today; it becomes an explicit declaration
       so the Core knows what to call.
   * - ``ui`` slots
     - Yes
     - Replaces ``register_plugin_*``.
   * - ``paths`` (interface path schema)
     - Yes for interfaces
     - Each interface declares its own paths (**Decision**).
   * - ``default_install``
     - In the repository index of the official repository
     - **Decision**: some extensions install at first run and stay removable.
   * - ``experimental``
     - Yes
     - **Decision**: tag and disclaimer in the store.
   * - Lifecycle hook declarations
     - Not as separate fields
     - Lifecycle is managed by the runtime (start, stop, restart of the
       process); the extension implements ``start``/``stop`` callbacks.

Example manifest. It is valid against the M1 parser (``tests/test_extensions_manifest.py``
parses an equivalent file); the schema itself is not frozen until the SDK (M2)
exists:

.. code-block:: toml

   schema = 1

   [extension]
   id = "official/example_chat"
   name = "Example Chat"
   version = "1.2.0"
   description = "Example of an interface extension."
   author = "Example Author"
   license = "MIT"
   experimental = false

   [compat]
   core_api = ">=0.1,<1"

   [entry]
   module = "example_chat.main"
   function = "main"

   [provides]
   actions = ["message_example_chat"]

   [[provides.interfaces]]
   id = "example_chat"
   paths = ["example_chat/{chat}", "example_chat/{chat}/{thread}"]
   # Optional: what the Core may assume about the interface (all default off,
   # except user_facing). See "Interface traits".
   voice_input_tts_only = false

   # Optional: web content (needs web.static / web.routes / web.ws). See
   # "Web content" in extension_runtime.
   [provides.web]
   static = "web"        # served at /x/<id>/
   alias = "example"     # also at /example/
   routes = true         # /x/<id>/api/...
   websockets = true     # /x/<id>/ws/...

   [requires]
   services = []

   [[requires.extensions]]
   id = "official/vrm_renderer"
   version = ">=1"
   optional = true

   [permissions]
   capabilities = ["interfaces.register", "interfaces.inbound", "storage", "config", "stats", "web.static", "web.routes", "web.ws"]
   network = ["api.example.org:443"]

   [python]
   requirements = ["some-client-lib==1.4.0"]

   [config.API_TOKEN]
   type = "string"
   sensitive = true
   label = "API token"

Implemented in M1
-----------------

The parser, validator and resolver exist in ``core/extensions/`` (not yet wired
into start-up; see :doc:`migration_plan`):

* ``manifest.py``: the immutable ``Manifest`` model and ``parse_manifest`` /
  ``load_manifest``. The schema is **strict**: an unknown key is an error, so a
  typo cannot silently drop a permission. Every problem is reported in one pass
  (``ManifestError.errors``). Checked: id ``publisher/name``, PEP 440 version
  and ``core_api`` / dependency specifiers, PEP 508 requirements, capability
  names, ``host:port`` network destinations, engine kinds (``llm``, ``vox``,
  ``auris``, ``iris``, ``live``), ``UPPER_SNAKE_CASE`` config keys with typed
  defaults, interface path patterns starting with the interface id, duplicate
  and self dependencies.
* ``archive.py``: reads and checks a single-file extension (an uncompressed
  zip) **without extracting it**: manifest at the root, size-limited manifest,
  rejects unsafe member names (``..``, absolute, backslash, drive letter),
  symbolic links, duplicates and, by default, compressed members; SHA-256 of the
  file.
* ``resolver.py``: ``resolve`` returns the start order and the excluded
  extensions with reasons; ``check_install`` reports what installing a candidate
  would break before anything changes. See "Dependency resolution" below.
* ``manager.py``: ``ExtensionManager`` scans directories (development layout)
  and archives, and resolves. One bad extension is
  recorded as a ``LoadIssue`` and the scan continues. The first registration of
  an id wins.

The archive file extension is still undecided, so the scan takes the suffixes as
a parameter (default ``.zip``).

Dependency resolution
---------------------

* **Declared, version-constrained, optional where appropriate.** The resolver
  builds a graph from ``requires.extensions``, excludes the members of a cycle
  with a clear reason and orders startup by the graph (dependencies first,
  alphabetical otherwise, so the order is deterministic).
* **Conflicts** (an unsatisfied version, or two extensions providing the same
  interface identity or action name) exclude the extension; ``check_install``
  reports them **before** anything is changed. An extension that depends on an
  excluded one is excluded too. All providers of a conflicting
  identity are excluded: nothing wins by being older.
* **Optional dependencies** change behaviour, not installability: a missing or
  excluded optional dependency is not a problem, an unsatisfied optional version
  is a warning, and an optional edge that would close a cycle is ignored.
* **Shared infrastructure**: if several extensions need the same library or
  service, evaluate promoting it to the Core. Today every interface/engine
  library is a Core dependency; with processes per extension those can leave the
  Core ``pyproject.toml``.

Self-containment and isolation of dependencies
----------------------------------------------

**Decision**: an extension brings the libraries the Core does not provide, and
does not rely on another extension's code. Current Python and runtime
facts are in :doc:`current_architecture`. Proposal: a per-extension environment
(for example created with ``uv``), extending ``package_installer`` and
``modules_registry``, which are limited to a fixed allow-list today. With one
process per extension this is natural. Upgrades replace the install directory
atomically; uninstall removes the install directory and the extension's data
according to the data policy (see :doc:`repository_architecture`).

Extension data: why not a SQLite file per extension
---------------------------------------------------

**Decision**: data lives in the Core database, accessible **only** through the
``storage`` API, never directly. The alternative, one SQLite file per extension,
was evaluated and set aside:

* *SQLite per extension, pros*: real isolation, uninstall is "delete the file",
  per-extension backup and portability, no coupling to the Core SQL dialect
  (the Core uses PostgreSQL and has MariaDB/Postgres compatibility code in
  ``core/db.py``), schema migrations owned by the extension.
* *Cons*: Core backups (``backup_database``/``backup_table`` and the backup
  scheduler in ``main.py``) would not cover those files without a hook;
  concurrent access across restarts; no cross-extension or Core joins; more
  files to manage; no central control over quotas and content.
* *Core database via API, pros*: one backup and one place for quotas and
  control, transactions on the Core side, no scattered files. *Cons*: the Core
  becomes guardian of third-party schemas; namespacing and migrations need
  design; uninstall needs a coordinated drop; the 13 plugins with inline
  ``CREATE TABLE`` and the hard-coded init list in ``core/db.py`` must be
  migrated.

Data owned by the Core (history, memories, configuration, ``interface_paths``,
aliases, events, goals) always stays in the Core and is reached by API only.


Interface traits
----------------

The Core used to keep lists of interface names to answer a few questions about
an interface. An interface now **declares** the answers in its manifest
(``[[provides.interfaces]]``) and the Core reads them from ``core.interface_traits``
(in-tree interfaces declare theirs when they register):

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Key
     - Meaning
   * - ``user_facing`` (default true)
     - A person is on the other end, so an empty turn is a missed reply.
   * - ``voice_input_tts_only``
     - Speech is added to a reply only when the incoming message was spoken.
   * - ``negative_chat_id_is_group``
     - Chat ids are numbers; negative means a group, positive a person.
   * - ``bot_username_suffix``
     - Account names ending in this belong to bots (case-insensitive).
   * - ``peer_ids``
     - The ids in ``SYNTH_PEERS`` are user ids of this platform.
   * - ``reply_only``
     - A request/response interface (an HTTP API): the only place a turn may answer is
       the conversation the request came from. The Core rewrites a corrected action that
       points elsewhere back to it.
   * - ``actions``
     - Names of extra actions the interface may offer besides ``send_message``
       (described at register time).

An interface the Core does not know (no declaration, not one of the few that ship
in the Core) is not treated as user-facing and gets the ``send_message`` action.
