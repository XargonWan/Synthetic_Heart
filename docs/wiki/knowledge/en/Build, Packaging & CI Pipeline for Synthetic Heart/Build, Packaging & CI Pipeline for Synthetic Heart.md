---
kind: build_system
name: Build, Packaging & CI Pipeline for Synthetic Heart
category: build_system
scope:
    - '**'
source_files:
    - Dockerfile
    - docker-compose.yml
    - pyproject.toml
    - .github/workflows/build-release.yml
    - .github/workflows/deploy-pages.yml
    - frontend/package.json
    - run_tests.sh
    - GitVersion.yml
    - automation_tools/container_synth.sh
---

Synthetic Heart uses a multi-stage Docker build driven by GitHub Actions to produce cross-architecture container images, with a separate Node.js frontend build and a Python dependency strategy centered on `uv`.

**Python packaging and dependencies**
- `pyproject.toml` declares the project (`synthetic-heart`, requires Python ≥3.11) and all runtime/optional dependencies. Optional audio engines (e.g. `vosk`, `whisper`) are exposed via `[project.optional-dependencies]` so users install only what they need.
- Dependency resolution is handled by `uv` (`uv.lock` is committed). The Dockerfile sets `UV_PROJECT_ENVIRONMENT=/app/venv` and runs `uv sync --frozen --no-cache` to create an isolated venv inside the image. Dev tooling (pytest, ruff, sphinx, mypy) lives under `[dependency-groups.dev]`.
- Custom package sources are declared in `[tool.uv.sources]` (e.g. `kittentts` from a direct wheel URL, `torch`/`torchaudio` from a CPU-only PyPI index) and pinned via `[[tool.uv.index]]`.
- The setuptools build backend is configured in `[build-system]` and `[tool.setuptools.packages.find]` includes `core`, `plugins`, `interface`, `llm_engines`, `vendor`, `plugins_dev`, `interface_dev`, `automation_tools`, `tests` while excluding `res`, `skins`, `webtop`, `website`.

**Frontend build**
- The Vue 3 / Vite SPA lives under `frontend/`. Its `package.json` defines `pnpm` as the package manager and exposes `dev`, `build` (typecheck + vite build), `preview`, `typecheck` scripts.
- The Dockerfile builds the frontend in a dedicated `node:22-slim` stage (`stage_builder`), caches layers by copying `package.json` and `pnpm-lock.yaml` first, then copies source and runs `pnpm build`. The resulting `/build/dist` is copied into the final image at `/app/frontend/dist` so the backend can serve it.

**Container image construction**
- The multi-stage `Dockerfile` starts from `ghcr.io/astral-sh/uv:latest` to fetch the `uv` binary, then builds the frontend in a `node:22-slim` stage, and finally assembles a `python:3.12-slim` runtime image.
- System packages include ffmpeg, MariaDB/PostgreSQL clients, espeak, OpenSSL, and development headers needed by C extensions. Node.js is optionally installed via `INSTALL_NODE=true` (default) to support the Minecraft Rift Vessel connector; a node-free image can be built with `--build-arg INSTALL_NODE=false`.
- S6-overlay is downloaded per-arch (`TARGETARCH`) and registered as service managers. Two services are defined: `synth` (the main app) and `searxng` (self-hosted search engine cloned from source and installed into its own venv).
- Build args `GITVERSION_TAG`, `BUILD_DATE`, `VERSION` are injected by CI and surfaced at runtime via `SYNTH_VERSION`.

**Local development and testing**
- `run_tests.sh` installs `uv` if missing, runs `uv sync --frozen`, installs pytest extras, then executes `uv run run_tests.py`. It always exits 0 and writes a GitHub Actions summary when `GITHUB_STEP_SUMMARY` is set.
- `docker-compose.yml` orchestrates three services: `synth` (builds from source or pulls a prebuilt image), `synth-db` (pgvector/postgres), and an optional `synth-selenium-llm-engine`. Persistent volumes cover config, skins, logs, and DB data. Environment variables drive DB connections, ports, and model/media directories.

**CI/CD pipeline**
- `.github/workflows/build-release.yml` triggers on pushes to `main`, `develop`, `fix/**`, `feat/**` and PRs against those branches. It:
  - Computes a SemVer tag via GitVersion (`GitVersion.yml` in ContinuousDelivery mode) and exposes it as `semver`.
  - Builds multi-arch images (`linux/amd64` on `ubuntu-latest`, `linux/arm64` on `ubuntu-24.04-arm`) using `docker/build-push-action@v6` with GH cache, pushing each architecture by digest.
  - Runs unit tests, agent integration tests (with a MySQL service), and agent E2E tests (MySQL + Selenium Chrome).
  - Runs `mypy . --strict`.
  - Assembles a multi-arch manifest from the digests, tagging with branch-derived tags (`latest`, `latest-develop`, `pr<N>-<semver>`, `<branch>-<semver>`).
  - Cleans up untagged images and PR tags older than two weeks via the Docker Hub API.
- `.github/workflows/deploy-pages.yml` deploys the static `website/` directory to GitHub Pages on pushes to `develop`.

**Versioning strategy**
- `GitVersion.yml` enforces a Continuous Delivery flow: `main` increments Patch, `develop` and feature/fix branches increment Minor/Patch respectively, with commit-message-based bumps (`+semver: ...`). Tags are derived from branch names and commit history rather than manual releases.

**Runtime orchestration**
- `automation_tools/container_synth.sh` is the container entrypoint script: it loads `.env`, ensures writable `logs/` and `skins/` directories (chowning to `PUID:PGID` or falling back to world-writable), then execs `main.py` either interactively or in `--service` mode via s6.