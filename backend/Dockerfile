# One image, three commands: api, worker, ingestor.
#
# They share a codebase, a dependency set and a release cadence, so building three near-identical
# images would only mean three things to patch. The Kubernetes Deployments differ by command.

# --- build ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# Dependencies are resolved from the committed lock file and exported with hashes, so the image
# contents are reproducible and pip verifies every artefact it downloads.
RUN pip install --no-cache-dir uv==0.12.6

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt

# --- runtime -------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# The source is copied rather than installed as a package so that paths resolved relative to the
# code (alembic.ini, the migration directory) are the ones we shipped.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Runs as a non-root user with no shell. Combined with a read-only root filesystem in the Helm
# chart, a compromised process has very little to work with.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin convoscore \
    && chown -R convoscore:convoscore /app
USER 10001

EXPOSE 8000

# Overridden per Deployment: `python -m app.worker`, `python -m app.ingestor`, `python -m app.migrate`.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
