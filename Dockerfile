# khive computer: one data directory, MCP over HTTP, actions in local slots.
# Python 3.12 for the server, git for import/export/push, uv to install the package and the toolbox
# (pytest, ruff) that actions may call, and uv itself stays on PATH for the projects actions run.
# Toolchains (Rust, Node, Debian packages) are layered on top by `kc build --rust --node --apt ...`,
# which generates the Dockerfile for you; see docs/DEPLOY.md.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --group toolbox
# The server runs as root so it can hand each action's slot to the action user; actions run as kc.
RUN useradd --uid 1000 --create-home --shell /bin/bash kc
ENV KC_ROOT=/data/computer KC_ACTION_USER=kc KC_DRAIN_SECONDS=300 PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8810
CMD ["sh", "-c", "kc init >/dev/null && exec kc serve --host 0.0.0.0 --port 8810"]
