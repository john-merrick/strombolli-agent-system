# Stromboli sandbox image (PRD §4) — runs a target repo's tests isolated from
# the host. The coding node mounts the per-task worktree at /work and runs the
# test command here via `docker run --rm --network none` (see sandbox/runner.py).
#
# Build:  docker build -f docker/sandbox.Dockerfile -t stromboli-sandbox:latest .
FROM python:3.12-slim

# uv for fast, reproducible installs inside the sandbox.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Common build/test toolchain. Extend per target-repo needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git make \
    && rm -rf /var/lib/apt/lists/*

# The default test command is `python -m pytest` (sandbox/runner.py) and the
# container runs with --network none, so the test runner must be pre-baked.
RUN uv pip install --system pytest

WORKDIR /work

# Tests run as a non-root user against the mounted worktree.
RUN useradd --create-home --uid 1000 sandbox
USER sandbox

# The command is supplied at `docker run` time by the SandboxRunner; default to
# the project test command so a bare `docker run` is still meaningful.
CMD ["python", "-m", "pytest", "-q"]
