# Single backend image: Python 3.11, non-root runtime, Linux compatible.
# Source code only: knowledge/model/index, secrets, outputs and local sessions
# are excluded by .dockerignore and supplied at deploy time.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_RETRIES=10 \
    PIP_TIMEOUT=60

RUN groupadd --gid 10001 rvs \
    && useradd --uid 10001 --gid rvs --create-home --shell /usr/sbin/nologin rvs

WORKDIR /app

# Docker-only requirements: sentence-transformers pulls PyPI ``torch`` for
# Linux, whose default wheel depends on the multi-GB CUDA 13 toolkit.  The
# container only runs CPU embedding inference, so install the official CPU
# wheel in its own cached layer; local development keeps requirements.txt
# unchanged.  The BuildKit pip cache keeps already-downloaded wheels when a
# later package fetch has to be retried.
COPY requirements.txt requirements-docker.txt /app/
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://pypi.org/simple \
      -r /app/requirements-docker.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install -r /app/requirements.txt

# PyTorch/onnxruntime wheels load libgomp at import time; python:slim does not
# ship it.  Install the small runtime library explicitly instead of relying on
# a developer machine's toolchain.  Keep this layer after the slow pip layer so
# a native-library fix does not force a full dependency re-download.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=rvs:rvs runtime /app/runtime
COPY --chown=rvs:rvs robot_vacuum /app/robot_vacuum
COPY --chown=rvs:rvs product_ui /app/product_ui
COPY --chown=rvs:rvs project.json README.md /app/
COPY --chown=rvs:rvs docker/entrypoint.py docker/healthcheck.py /usr/local/bin/

RUN chmod 0555 /usr/local/bin/entrypoint.py /usr/local/bin/healthcheck.py \
    && mkdir -p /data /assets \
    && chown -R rvs:rvs /data /app /assets

USER 10001:10001

VOLUME ["/data"]
EXPOSE 8000

# Liveness only: a degraded (no model/key/index) container is still alive.
# Readiness is checked separately: healthcheck.py ready or GET /api/health/ready.
HEALTHCHECK --interval=30s --timeout=6s --start-period=90s --retries=3 \
    CMD ["python", "/usr/local/bin/healthcheck.py", "live"]

ENTRYPOINT ["python", "/usr/local/bin/entrypoint.py"]
