Repositories and the installer (M6, implemented)
================================================

What :doc:`repository_architecture` proposed, as built. Code: ``core/extensions/repo/``
(index, transports, config, installer, pack), ``core/extensions/cli.py``,
``core/extensions/webui_store.py``. Tests: ``tests/test_extensions_repo.py``,
``test_extensions_cli.py``, ``test_extensions_store.py``.

Format
------

A repository is a directory (or URL) with ``synth-repo.json``, ``synth-repo.json.sig`` and
one archive per extension version (``packages/<publisher>/<name>-<version>.synthx``: an
uncompressed zip with the manifest at its root). The index lists, per version: ``id``,
``version``, ``file``, ``size``, ``sha256``, ``core_api``, ``platform``, ``name``,
``description``, ``experimental``, ``default_install`` and the capabilities the manifest
declares (shown before install). It is validated strictly: a malformed index is refused
whole, with every reason.

The same files serve a local folder, a Git working tree, a Git URL or a web server; only
the **transport** differs (``LocalTransport``, ``HttpTransport``, ``GitTransport``). A
transport reads small files and copies files with a size limit; it never verifies, so a
new one cannot weaken verification.

Trust
-----

* **Integrity**: every download is checked against the index's size and SHA-256 before
  anything else, then for structure (no ``..``, absolute paths, links, compressed
  members), then the manifest must be the id and version the index promised. Extraction
  checks every path again and caps members and bytes, into a staging directory that is
  swapped into place; a failure leaves the installed version as it was.
* **Signature**: Ed25519 over the exact bytes of ``synth-repo.json``. The publisher's
  public key is **pinned** when the repository is added (``repo add --key``); a different
  key later is a refusal, not an update. With a key, a signature is required unless the
  operator says otherwise for that repository. The CLI prints the fingerprint.
* **Trust level**: a repository is added untrusted unless ``--trusted``; installing from
  an untrusted one needs an explicit confirmation (``--yes``, or a dialog in the Store).
  A local path is not exempt from any of this.
* **Permissions**: installing grants nothing. The manifest's capabilities are shown and
  the operator grants them (``grant``, or the Store's prompt), never more than declared.
* **GitHub access token.** One setting, *Settings > Extensions > GitHub access token*
  (``GITHUB_TOKEN``, masked), is used for every GitHub repository: it reads private releases
  through the GitHub API and raises the anonymous download limit. A repository's own token wins.
  When GitHub answers 401/403/429 the error tells the user to enter it and where.
* **Credentials** of a private repository (a token) are stored with the repository
  (mode 0600), used only by the transport and never sent to an extension or to the page.

Behaviour
---------

* **Choosing**: the highest version that fits this Core (``core_api``) and this machine
  (``platform``: ``any`` or ``linux-x86_64-py312``-style, set when the package contains
  compiled libraries). The same version with different content in two repositories is a
  conflict the operator resolves with ``--repo``; identical content takes the lower
  priority number. A repository that cannot be read is reported and the others still work.
* **Dependencies** are installed first, checked with the resolver (``check_install``)
  before anything changes; a missing dependency is a clear error naming it.
* **Update** replaces atomically and keeps the previous version for **rollback**; a
  switched-off extension stays off. **Remove** deletes the files, keeps the data unless
  ``--purge``, revokes its grants, and refuses while something installed needs it.
* **On/off** is independent of installed (``enable``/``disable``); the runtime starts only
  what is on.
* **Defaults**: ``default_install`` packages are installed once, at the first start, from
  the **official repository**, which is built in (below). The first start needs the network:
  while the repository cannot be read nothing is marked done, and the next start tries
  again. One that fails is retried at the next start without stopping the others; one the
  operator uninstalled is never put back. A default that comes from the official
  repository is also **switched on and granted what its manifest declares** (it is the one
  source trusted to do that); a default that another repository merely flags is installed
  but not granted.
* **The official repository is built in** (``builtin_repositories()`` in
  ``core/extensions/repo/config.py``): pinned address and publisher key, a rolling GitHub
  release (``latest-develop``; ``SYNTH_OFFICIAL_CHANNEL`` picks another, ``latest`` for the
  ``main`` channel). It is never written to ``repositories.json``, cannot be removed or
  edited (the Repositories tab shows it as *built in*), and no other repository can take
  its name. The image carries no extension at all. Until now the image baked the defaults
  in (``scripts/build_bundled_extensions.py``, ``extensions_bundled/``, commit that removes
  them: find it with ``git log --diff-filter=D -- scripts/build_bundled_extensions.py``).
* **From the Store** an install switches the extension on and then shows what it asks for
  and offers to grant it; nothing is granted without that confirmation.
* **Live**: the Catalog and Repositories tabs (Plugins) and the CLI use the same installer. From the
  Store the running extension is stopped, replaced and started again at once; from the
  CLI the change applies at the next start.

Building
--------

``pack`` turns an extension folder into one package with its libraries built into
``lib/`` (from ``[python] requirements``), so installing needs no network and no build
tool. Packing is reproducible (sorted entries, fixed dates): the same tree gives the same
SHA-256. ``index`` writes and signs the index; ``keygen`` makes a publisher key.

::

    uv run python -m core.extensions.cli keygen ~/.config/synth/publisher.key
    uv run python -m core.extensions.cli pack <plugins repo>/extensions/telegram_bot --out ./repo
    uv run python -m core.extensions.cli index ./repo --sign ~/.config/synth/publisher.key \
        --name official --default-install official/telegram_bot
    uv run python -m core.extensions.cli repo add official ./repo --key <public key> --trusted
    uv run python -m core.extensions.cli install official/telegram_bot --grant

Screenshots, progress and updates in the Store
----------------------------------------------

* **Screenshots.** An extension may carry a ``screenshots/`` folder (``.png``, ``.jpg``,
  ``.webp`` or ``.gif``; at most 8, each at most 2 MB). The catalog shows them in file-name
  order. The builder (``build_index`` / ``build_release``) takes them out of the package and
  publishes each as a file of the repository beside it (``<archive name>--<file name>``, a
  release asset in the GitHub layout); the signed index lists them with their SHA-256 and size
  (``screenshots``), so a picture can be shown **before** the extension is installed and is
  still checked against the signature. ``GET /api/extensions/store/screenshot/<id>/<n>`` serves
  picture ``n``: from the installed package when there is one, otherwise from the repository
  (cached by SHA-256; one that does not match is never shown).
* **Progress.** ``Installer.install`` opens an operation (``core/extensions/repo/progress.py``);
  the transports report the bytes they write through a context variable, so no signature
  changed. ``GET /api/extensions/store/progress`` answers ``{id: {percent, phase}}`` and the
  Install and Update buttons give way to a bar in the highlight colour with the percentage.
  Downloads are 90 % of it (a package already in the cache counts as downloaded), placing the
  packages is the last 10 %.
* **One source for "update available" and "update".** An update comes from the repository the
  extension was installed from (``Installer._update_source``), and so does the marker:
  ``updates()`` and the catalog rows ask the same question. A newer version in another
  repository is not an update (installing it would be a change of publisher); if the
  extension's repository is gone, any repository may update it.
* **Questions are a modal inside Synthetic Heart** (``synth-dialog.js``), not the browser's
  ``confirm()`` box.

What was measured, and what was not
-----------------------------------

* A real pack and install of both Telegram extensions into a scratch repository worked,
  and their bundled libraries import under the sandbox interpreter (``python -S -I``).
* **Package size is the cost of "one file, its libraries inside"**: ``telegram_bot`` is
  5.6 MB; ``telegram`` is **162 MB**, almost all of it the native ``ntgcalls`` library
  (136 MB) that py-tgcalls brings. That package is also platform-specific
  (``linux-x86_64-py312``), so an official repository needs one build per platform, or
  the call support split into an optional extension. Not decided.
* **The image side**: the Dockerfile builds the Core's environment (multi-stage: no compiler, no
  dev dependencies, no cache) and nothing else. It holds no extension, no Node.js and no
  SearXNG; the extensions come from the official repository at the first start.
* **The official repository is a GitHub-release catalog** (2026-11-26; see
  :doc:`repository_architecture`): kind ``github``, a rolling release per branch
  (``latest-develop``, ``latest``), one zip per extension, a signed catalog, built and published
  by a pipeline. The transport, the builder (``release`` command) and the catalog are tested
  against a fake GitHub that redirects to a CDN path, and the build was run for real: six
  packages, signed, verified with the pinned key. **Not done**: the pipeline has not run on
  GitHub (it needs the secret ``SYNTH_PUBLISHER_KEY`` and, until the extension platform is
  merged, the variable ``SYNTH_CORE_REF``), so no release exists yet; and one build per platform
  (the packages with native libraries are ``linux-x86_64-py312`` only).
* Not exercised: a real Git clone of it over the network (the Git transport is only tested
  against its protocol allow-list and command line),
  a private repository with a token, and a real HTTPS server (HTTP is tested against a
  local server).
* Not built: signed *packages* (only the index is signed; the index's SHA-256 binds every
  package to it) and key rotation. The post-update step that carries the old plugins'
  settings over is in :doc:`migration_plan`; it fetches the userbot from the official
  repository once the release asset above is uploaded.
