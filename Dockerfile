# Single backend image: Python 3.11, non-root runtime, Linux compatible.
# Source code only: knowledge/model/index, secrets, outputs and local sessions
# are excluded by .dockerignore and supplied at deploy time.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 10001 rvs \
    && useradd --uid 10001 --gid rvs --create-home --shell /usr/sbin/nologin rvs

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

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
