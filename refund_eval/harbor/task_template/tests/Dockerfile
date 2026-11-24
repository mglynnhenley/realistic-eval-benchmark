# Separate verifier: the benchmark package (for refund_eval.grading) and /tests/test.sh.
# Harbor restores the world's /var/log/world here from the sidecar artifacts.
FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
COPY --from=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON=3.14 UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1
WORKDIR /app
COPY source/pyproject.toml source/uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY source/ /app/
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
