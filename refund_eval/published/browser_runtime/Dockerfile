FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \
    NODE_ENV=production \
    HOME=/tmp/home
WORKDIR /opt/browser
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    && ./node_modules/.bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm
COPY server.mjs ./
ENTRYPOINT ["node", "/opt/browser/server.mjs"]
