Docker image build notes
========================

Problem observed
----------------

Sometimes the built container image lacked runtime packages (notably ``uvicorn``), which caused the WebUI to fail to start. The most common root causes are:

- Accidentally copying a host virtualenv (``venv/``) into the image build context which overwrote the venv created inside the image during the build. This happens when ``COPY . /app`` runs after creating the venv at ``/app/venv``.
- Bind-mounting the project into ``/app`` at runtime (e.g., ``docker run -v $PWD:/app``) which hides the venv inside the image.

What we changed
----------------

- Create the Python virtual environment at ``/opt/venv`` (``ENV VENV_DIR=/opt/venv``) instead of ``/app/venv`` so project copies or bind-mounts will not overwrite it.
- Create a symlink at ``/app/venv`` pointing to ``/opt/venv`` for backwards compatibility when no bind-mount is used.
- Add a build-time import check to fail the build if ``uvicorn`` is not importable after the final ``COPY`` step.
- Add ``venv/`` and common host env files to ``.dockerignore`` to prevent accidental copying of host virtual environments into the image context.
- Add a CI workflow (``.github/workflows/docker-smoke-test.yml``) that builds the image and performs a runtime import check for ``uvicorn``.
- Update the container launcher script (``automation_tools/container_synth.sh``) to prefer ``$VENV_DIR`` and fall back to ``/app/venv`` when necessary.

Why this fixes the root cause
-----------------------------

By moving the venv out of ``/app`` we remove the class of failures caused by copying or mount overlays of the project directory. The build-time and CI checks ensure problems are detected early and fail fast, preventing images that lack critical runtime packages from being released.
