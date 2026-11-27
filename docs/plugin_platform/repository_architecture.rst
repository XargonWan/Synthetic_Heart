F. Repository and Distribution Architecture
===========================================

Inspired by Linux package managers. Everything is a **Proposal** unless tagged
**Decision**.

User-facing management
----------------------

**Decision**: repositories are added and removed from the **WebUI** and from a
**CLI**. In the WebUI they live in the **Plugins > Catalog** and **Plugins > Repositories** sub-tabs
(``core/webui_templates/sections/plugins.html``), which today shows "Coming
soon". The existing "Development Components" switch (not persistent, restarts
SyntH, covers ``interface_dev``, ``plugins_dev`` and ``engines/<kind>/dev``)
must be rethought around the mountable ``plugins_dev`` root.

Layers
------

Keep **transport** independent from **format**:

* **Repository format** (identical for a cloud and a local repository): a signed index document listing packages (id,
  version, file name, size, hash, publisher, signature, ``core_api`` range,
  ``default_install`` flag where applicable) plus the package files (one archive
  per extension, see :doc:`manifest_proposal`).
* **Transport**: how the index and files are fetched. Candidates from the
  brief: GitHub, GitLab, generic HTTP/HTTPS, local filesystem, optionally
  FTP/SFTP, others later. Each is a small adapter behind one interface, so a
  repository hosted anywhere serves the same format.
* **Official repository**: holds all official non-core extensions that live in
  the main repository today (interfaces, engines, VRM renderer, Vessel
  adapters, Dashboard, Stage, AzuraCast, Zen, ...).
* **User repositories**: any number, with priorities.

Local repositories (Decision)
-----------------------------

A repository can be added from a **local filesystem path**. This is crucial for
testing that the whole platform works **before publishing**, and it helps
extension developers.

* **Same logic as a cloud repository.** The local path is only another
  *transport*. The index is read, versions are resolved, packages are
  "downloaded" (copied through the transport), hashes and signatures are
  verified, and the extension is installed exactly as if it came from the cloud.
  There is no special install path for local repositories. Anything that works
  from a local repository works from a remote one, and the reverse.
* **Same format.** A local repository has the same layout as a published one
  (index plus one archive per extension), so a repository can be built and
  tested locally and then published unchanged.
* **Trust is not bypassed.** A local repository is still a repository with a
  trust level; the user adds it explicitly (WebUI Plugins > Store, or CLI). If
  signatures are required for a repository, they are required for the local one
  too; testing without signing is a per-repository, explicit setting, not an
  implicit exemption of local paths.
* **Git or not.** A local path may be a plain directory or a Git working tree
  (the first official repository is a Git repository). Git history only
  matters for rollback, as described below; it is not required.
* **Container paths.** When SyntH runs in Docker, the path is resolved inside
  the container, so the repository directory must be mounted as a volume.
  This is part of the mount-point layout in :doc:`open_questions`.

First repository
~~~~~~~~~~~~~~~~

The official repository is ``https://github.com/XargonWan/synthetic-heart-official-plugins``
(locally ``/home/xargon/gits/synthetic-heart-official-plugins/``). It is a repository of kind
**github**: a *catalog* published as the assets of a **rolling GitHub release**, with each
extension its own ``.zip`` asset.

.. list-table::
   :header-rows: 1
   :widths: 20 25 55

   * - Branch
     - Release tag
     - Notes
   * - ``develop``
     - ``latest-develop``
     - a pre-release; the channel SyntH follows for now (``OFFICIAL_CHANNEL``)
   * - ``main``
     - ``latest``
     -

There is no release per update: the tag moves to the newest commit and the assets are replaced.
The version of an extension is its own, in the manifest inside its zip and in the catalog.
A release holds ``official-<name>.zip`` for each extension, ``synth-repo.json`` (the catalog:
version, SHA-256, size, platform and capabilities of each package) and ``synth-repo.json.sig``
(Ed25519 over the exact bytes of the catalog). A ``github`` repository is configured as
``github:owner/name`` and the release tag in ``branch``; the Core reads the assets from
``https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>``, following the redirect to
GitHub's CDN (``https`` only, at most five hops, size capped, a token only ever sent to the first
host). GitHub does not have to be trusted: the catalog is signed and names every package's
SHA-256. This also lifts the 100 MB file limit of a Git host: a release asset can be 2 GB, so
the 162 MB user-account Telegram package needs nothing special.

SyntH adds the repository by itself the first time the extension runtime starts
(``official``: trusted, signed, key pinned in ``core/extensions/repo/config.py``); removing it
afterwards sticks. A default it flags (``default_install``) is installed granted, like those of
the baked-in repository. The publisher key is ``~/.config/synth/publisher.key`` and never goes
in a repository; in CI it is the secret ``SYNTH_PUBLISHER_KEY``.

**The pipeline** (``.github/workflows/publish.yml`` in that repository) runs on a push to
``develop`` or ``main`` (and on demand, or when SyntH dispatches ``core-updated``): it checks out
this repository (the sources are ``<plugins repo>/extensions/`` here and the packer is ``core/extensions``),
builds the Stage client, runs ``python -m core.extensions.cli release`` (one zip per extension and
the signed catalog), checks the catalog against the pinned key with ``verify_release.py``, moves
the rolling tag, and uploads the assets (replacing the old ones). A pull request only builds,
unsigned. The repository variable ``SYNTH_CORE_REF`` selects which ref of this repository to build
from (for example ``refactor/plugin-system`` until it is merged).

``python -m core.extensions.cli release SOURCES... --out DIR --sign KEY`` writes exactly what the
release carries. The older layout (``packages/<publisher>/<name>-<version>.synthx`` and an index,
for ``local``, ``http`` and ``git`` repositories, with ``url`` for oversized packages) is still
supported for private or self-hosted repositories.

Metadata and resolution
-----------------------

* **Package identity** is ``publisher/name`` plus version; the manifest states
  the exact version of the published package.
* **Version resolution** uses semver ranges from ``requires.extensions`` and
  ``core_api``. Repository priority breaks ties; the same id from two
  repositories is a conflict the user resolves explicitly, never silently.
* **Caching** keeps the last index and the installed package files locally.
* **Offline operation**: installed extensions run offline; first-run
  ``default_install`` extensions are installable offline from a **cache baked
  into the Docker image** (to confirm; see :doc:`open_questions`).

Version retention (Decision)
----------------------------

Repositories are **not required** to keep every historical release. They
distribute the **currently published** versions. A Git-hosted repository keeps
history through Git; a non-Git repository need not. No mandatory permanent
archive is designed.

Implications to handle:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Concern
     - Handling
   * - Installed version tracking
     - The runtime records exact id, version and hash of each installed file.
   * - Updates
     - Replace atomically; keep the previous file locally until the new version
       starts successfully.
   * - Downgrade / rollback
     - Possible from the local copy of the last installed version, or from Git
       history where the repository is Git-based. Otherwise it may be
       unavailable; the UI must say so.
   * - Dependency resolution
     - Resolves against currently published versions; a pinned older version
       that is no longer published is reported as missing, not guessed.
   * - Reproducibility
     - An install records hashes so the same set can be verified, but a missing
       historical package makes exact reinstall impossible. Accepted.
   * - Missing historical packages
     - A clear error naming the missing id and version.

Trust, signatures, authentication
---------------------------------

* **Integrity**: hashes in the index and a signature over the index (and/or the
  packages). **Decision (M6)**: an Ed25519 signature over the exact bytes of the
  index, with the public key pinned in the repository's configuration (trust on first
  use); see :doc:`repository_implementation`.
* **Publisher identity and trust**: each repository has a trust level and a key.
  Installing from a non-official repository requires an explicit user action and
  a visible warning. The ``experimental`` flag adds a tag and a pre-configured
  disclaimer.
* **Authentication** for private repositories belongs to the transport adapter
  (tokens, SSH keys); credentials are Core secrets, never given to extensions.
* **Official extensions are not trusted more at runtime**: they use the same
  public API and the same process sandbox (**Decision**). Signing affects what
  may be installed, not what an installed extension may do.

Install, update and removal semantics
-------------------------------------

**Decision**: *installed/uninstalled* and *on/off* are independent.

* **Off**: the extension stays installed (files present), its process is
  stopped.
* **Uninstalled**: it is off, and its **files are deleted**.
* **Data policy** (open): on uninstall either delete the extension's stored data
  or offer to keep it; behaviour on update must preserve data across
  compatible versions.
* **Default installs**: ``default_install`` marks extensions installed at first
  start. The user can uninstall them; the Core must work without them (for
  example without any Cortex endpoint it shows an explicit error message).

Lifecycle states
~~~~~~~~~~~~~~~~

The runtime tracks, per extension: **available** (in a repository),
**installed**, **configured** (required configuration present), **enabled**
(on), **running** (process alive and registered). Failures at each step are
isolated: failed installation (nothing changed), failed initialisation or crash
(restart with backoff, then marked failed; the Core continues), missing
dependency or incompatible API (refuses to start, with a reason), external
service unavailable (the extension reports itself unavailable), failed upgrade
(roll back to the local previous file), partial uninstall (retried on next
start). A failing optional extension must never compromise the Core.
