# `main`: the agent container. Inspect and the benchmark package; no browser, no world.
# Built by Harbor from the adapter's allowlisted agent/ payload.
FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
COPY --from=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON=3.14 UV_PYTHON_DOWNLOADS=never TIKTOKEN_CACHE_DIR=/opt/tiktoken PYTHONUNBUFFERED=1
WORKDIR /app
COPY agent/pyproject.toml agent/uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY agent/ /app/
RUN /app/.venv/bin/python /app/install-agent.py
RUN uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"
ENV PATH="/app/.venv/bin:$PATH"
