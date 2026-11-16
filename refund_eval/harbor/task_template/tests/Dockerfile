# Separate verifier: the benchmark package (for refund_eval.grading) and /tests/test.sh.
# Harbor restores the world's /var/log/world here from the sidecar artifacts.
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON=3.14 UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app
COPY source/pyproject.toml source/uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY source/ /app/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
