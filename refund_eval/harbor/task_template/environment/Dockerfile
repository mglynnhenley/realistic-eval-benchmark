# `main`: the agent container. Inspect and the benchmark package; no browser, no world.
# Built by Harbor from the adapter's allowlisted agent/ payload.
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON=3.14 UV_PYTHON_DOWNLOADS=never TIKTOKEN_CACHE_DIR=/opt/tiktoken PYTHONUNBUFFERED=1
WORKDIR /app
COPY agent/pyproject.toml agent/uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY agent/ /app/
RUN /app/.venv/bin/python /app/install-agent.py
RUN uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"
ENV PATH="/app/.venv/bin:$PATH"
