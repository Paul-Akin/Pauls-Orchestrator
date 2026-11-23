# Hub image. Build context is the REPO ROOT (the hub reads ../protocol/schemas).
#   docker compose build hub
FROM python:3.12-slim

# uv from PyPI (pinned) keeps the build to one registry.
RUN pip install --no-cache-dir uv==0.11.32

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HUB_ROOT=/app \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

# Dependencies first (cached layer), then the code.
COPY pyproject.toml uv.lock ./
COPY hub/pyproject.toml hub/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --no-install-project

COPY protocol protocol
COPY hub hub
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 hub && mkdir -p /app/data && chown hub /app/data
USER hub

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)"
# Run the venv entry point directly: uv is a build tool, not needed (or writable) at runtime.
CMD ["hub", "serve"]
