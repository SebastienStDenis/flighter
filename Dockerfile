FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm

RUN useradd --create-home --uid 10001 app

COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app alembic.ini /app/alembic.ini
COPY --chown=app:app alembic /app/alembic
COPY --chown=app:app scripts /app/scripts
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

USER app
WORKDIR /app
# Mounted at /app/data rather than /data so the default relative paths resolve the same
# way here as they do in a checkout, and one configuration serves both. It holds the
# SQLite database and every credential.
VOLUME ["/app/data"]

EXPOSE 8000
HEALTHCHECK --interval=1m --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status == 200 else 1)"

ENTRYPOINT ["flighter"]
# One process runs the API, the poll worker and the mail loop. There is one user and a
# handful of flights; splitting them across containers would buy nothing but moving parts.
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
