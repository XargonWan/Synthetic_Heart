---
kind: dependency_management
name: Multi-Tool Dependency Management with uv Lockfiles and pnpm
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - frontend/package.json
    - frontend/pnpm-lock.yaml
    - package.json
    - package-lock.json
    - docs/requirements.txt
    - Dockerfile
---

Synthetic Heart manages dependencies across three distinct ecosystems — Python, Node.js (frontend), and documentation tools — using a combination of modern lockfile-based tooling and explicit source overrides.

**Python dependencies** are declared in `pyproject.toml` under `[project.dependencies]`, pinned with version ranges (e.g., `fastapi>=0.128.0`, `uvicorn>=0.40.0`). The project uses **uv** as the primary resolver and installer: `uv.lock` is the authoritative lockfile, generated via `uv sync --frozen`. Dependencies are installed inside a venv at `/app/venv` (set via `UV_PROJECT_ENVIRONMENT`). Optional audio engines (VOSK, Whisper) are exposed through `[project.optional-dependencies]` extras (`[vosk]`, `[whisper]`) so users install only what they need. Special packages are sourced from non-PyPI locations: `kittentts` is pulled directly from a GitHub release URL, while `torch` and `torchaudio` are resolved from the PyTorch CPU index (`https://download.pytorch.org/whl/cpu`) via `[tool.uv.sources]` and a custom `[tool.uv.index]` entry. Platform constraints are enforced through `required-environments` restricting installation to Linux x86_64 and Windows AMD64.

**Frontend dependencies** use two separate npm registries. The main WebUI lives at the repo root with its own `package.json` and `package-lock.json` (lockfileVersion 3), managed by npm. The standalone Vue 3 avatar frontend (`frontend/`) uses **pnpm** with `pnpm-lock.yaml` (lockfileVersion 9.0) and enforces deterministic installs via `--frozen-lockfile` in CI/Docker. Both frontends pin exact versions through their respective lockfiles.

**Documentation dependencies** are isolated in `docs/requirements.txt` (Sphinx, sphinx-rtd-theme, myst-parser) and also duplicated in `pyproject.toml` under `[dependency-groups] dev` for local development.

**Containerization** builds the frontend separately in a `node:22-slim` stage, then copies the built artifacts into the final `python:3.12-slim` image. Python deps are installed via `uv sync --frozen --no-cache` against `uv.lock`, ensuring reproducible builds. SearXNG is installed in an isolated venv within the container.

**No vendoring strategy** is used for Python packages — the `vendor/` directory exists but is empty; all third-party code comes from PyPI or the configured sources. There is no private registry configured beyond the explicit PyTorch CPU index.