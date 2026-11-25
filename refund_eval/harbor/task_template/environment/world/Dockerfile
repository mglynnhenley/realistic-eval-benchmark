# `world`: the world service sidecar (clock, event queue, companies, mail, search and the browser).
# Build context is the task's environment/ directory, so it shares source/ with `main`.
FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS browser-node
FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
COPY --from=browser-node /usr/local/bin/node /usr/local/bin/node
COPY --from=browser-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
# curl: the task's collect hook reads the final snapshot from the admin port.
RUN printf '%s\n' 'Types: deb' 'URIs: https://snapshot.debian.org/archive/debian/20261118T000000Z/' 'Suites: bookworm bookworm-updates' 'Components: main' 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' 'Check-Valid-Until: no' '' 'Types: deb' 'URIs: https://snapshot.debian.org/archive/debian-security/20261118T000000Z/' 'Suites: bookworm-security' 'Components: main' 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' 'Check-Valid-Until: no' > /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# uv 0.11.7 (use a digest-only reference for Modal).
COPY --from=ghcr.io/astral-sh/uv@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON=3.14 UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers NODE_ENV=production
# The pinned Playwright MCP runtime (@playwright/mcp 0.0.83) and Chromium at /opt/browser,
# where the subprocess browser runtime looks for server.mjs.
WORKDIR /opt/browser
COPY source/refund_eval/published/browser_runtime/package.json source/refund_eval/published/browser_runtime/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && ./node_modules/.bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm
COPY source/refund_eval/published/browser_runtime/server.mjs ./
WORKDIR /app
COPY source/pyproject.toml source/uv.lock /app/
RUN uv sync --frozen --no-dev --no-install-project
COPY source/ /app/
RUN uv sync --frozen --no-dev && mkdir -p /var/log/world
COPY company-store/ /opt/company-store-seed/
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
# Ready once the world is set up and serving MCP (the port opens only after setup).
HEALTHCHECK --interval=3s --timeout=5s --start-period=600s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()"]
ENTRYPOINT ["python", "-m", "refund_eval.harbor.world_entrypoint"]
