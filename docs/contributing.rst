Contributing
============

.. image:: res/contributing.png
   :width: 600px
   :alt: Contributing Guide


We welcome pull requests for bug fixes and new features. Ensure your code passes the validation sequence (``uv run ruff format .``, ``uv run ruff check --fix .``, ``uv run ty check <files>``, ``uv run pytest``), and use the following commit message format:

- ``fix(element): description`` – bug fixes
- ``feat(element): description`` – new features
- ``minor(element): description`` – small improvements
- ``chore(element): description`` – small improvements
- ``doc(element): description`` – documentation changes
- ``patch(element): description`` – incremental updates

Example::

   fix(api): resolved an authentication issue

For larger changes, open an issue first to discuss your ideas. Documentation contributions are also appreciated. Update the ``docs/`` sources and verify that ``sphinx-build`` completes without errors.

.. _releases:

Releases
--------

Every push to ``main``, ``develop``, ``feat/**`` or ``fix/**`` publishes the Docker images
(``.github/workflows/build-release.yml``). A pull request only builds. Only two branches have
GitHub releases:

- ``main`` publishes a valid release ``v<version>`` with the Windows installer. It is the only
  one that is: it is the latest release, and the tag it creates is what the next release counts
  its version and notes from. Nobody tags by hand.
- ``develop`` publishes two **pre-releases** on every push. ``v<version>`` (the version
  already carries the branch: ``v1.31.0-develop``) is kept, so earlier builds stay
  downloadable. ``latest-develop`` is the one rolling pre-release: every push deletes it and
  makes it again at the new commit, so it always holds the newest installer. Neither is the
  latest release and neither tag is a plain ``vX.Y.Z``, so neither counts as a release for
  the version or the notes.
- ``feat/**`` and ``fix/**`` publish images but no release; their installer is a workflow
  artifact for 30 days.

**The version** is computed by ``scripts/version.py`` from the commit messages. It starts
from the newest plain ``vX.Y.Z`` tag reachable from the commit (none: ``0.0.0`` and the whole
history) and walks the non-merge commits after it:

- a breaking change (``type!:`` or a ``BREAKING CHANGE:`` footer) bumps the major;
- ``feat`` bumps the minor;
- anything else (``fix``, ``minor``, ``patch``, ``chore``, ``doc``, or a message that is not
  a conventional commit) bumps the patch.

On a branch other than ``main`` the branch name is appended (``1.31.0-develop``). Images
follow the same version: ``main`` also gets
``latest``, ``develop`` also gets ``latest-develop``, no other branch gets either.

**The release notes** are written by ``scripts/release_notes.py`` from the commits since the
previous release (on ``develop``, what it has that ``main`` has not yet shipped). Each line is the commit's scope and description, without its type, with its
hash at the end as a link:

- *Changes*: ``feat`` and breaking changes;
- *Fixes*: ``fix``, ``perf``, ``minor``, ``patch`` and a message that is not a conventional
  commit;
- left out: ``chore``, ``doc``, ``docs``, ``res``, ``style``, ``test``, ``refactor`` and the
  rest.

So the subject of a commit is what the user reads: write it for them. The notice at the top
of every release is ``.github/release-header.md`` (``{version}`` in it is the version); the
pre-releases add a notice that they are not a stable release.
Running ``python3 scripts/version.py`` and ``python3 scripts/release_notes.py --repo
xargonwan/synthetic_heart --version <v>`` locally shows what a push would publish.
