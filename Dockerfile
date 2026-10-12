# OPTIONAL local quickstart only — not the product's runtime, not production.
#
# No `# syntax=` directive on purpose: that line makes BuildKit pull
# `docker/dockerfile` from a registry, and on a mirror that rejects anonymous
# pulls the whole build dies before it starts. Nothing below needs it.
#
# OpsPilot is designed to run as two systemd units on a plain Ubuntu/Debian
# host (see README "Ubuntu systemd 部署"). This file exists so someone can
# stand the stack up locally in a few minutes without installing MySQL and
# Node first. Nothing in the product reads or requires it.
#
# Hard limits that must survive edits to this file:
#   * no Kubernetes manifests, no Helm charts, no operator — ever
#   * the image runs as a non-root user, same as the systemd units
#   * migrations and bootstrap stay out of the process entrypoint; they are
#     explicit one-shot commands (see docker-compose.yml `init`)
#   * no real secrets baked in — everything arrives at runtime from .env
#
# Registry knobs (both default to the official upstreams; override only if your
# link to them is unusable — see README "5 分钟跑起来"):
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple
#   --build-arg NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
# Never bake a mirror into this file: the public default must stay official.

# ---------------------------------------------------------------------------
# Frontend build (Node) — only needed by the `web` target
# ---------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web-build

ARG NPM_CONFIG_REGISTRY=https://registry.npmjs.org/
ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY}

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./

# `npm ci` silently SKIPS any optional dependency whose download fails. Vite 8
# ships rolldown as a set of platform-specific optional bindings, so one dropped
# download turns into `Cannot find module '@rolldown/binding-linux-x64-gnu'` from
# deep inside the bundler — far from the cause. Retry hard, then assert the
# binding resolved before building, so a flaky registry fails here with a
# sentence instead of there with a stack trace.
RUN npm ci --no-audit --no-fund \
        --fetch-retries=5 \
        --fetch-retry-mintimeout=2000 \
        --fetch-retry-maxtimeout=60000 \
    && node -e "import('rolldown').catch(function (e) { \
         console.error('rolldown native binding is missing after npm ci.'); \
         console.error('npm skips a failed optional download without erroring.'); \
         console.error('Re-run the build, or set NPM_CONFIG_REGISTRY to a reachable mirror.'); \
         console.error(String((e && e.message) || e)); \
         process.exit(1); \
       })"

COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Python runtime — shared by `api`, `worker` and the one-shot `init`
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS api

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PYTHONPATH=/app

WORKDIR /app

# Installed first so a source-only change does not re-resolve the world.
COPY pyproject.toml README.md ./
COPY shared/ shared/
COPY backend/ backend/
COPY worker/ worker/
COPY templates/ templates/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY scripts/bootstrap.py scripts/bootstrap.py
RUN pip install .

# Non-root, matching systemd/opspilot-*.service (both refuse to run as root).
RUN useradd --system --create-home --home-dir /var/lib/opspilot \
        --shell /usr/sbin/nologin opspilot \
    && mkdir -p /var/log/opspilot \
    && chown -R opspilot:opspilot /var/log/opspilot /var/lib/opspilot
USER opspilot

# The API port. The app also honours OPSPILOT_API_HOST / OPSPILOT_API_PORT.
EXPOSE 8080

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# ---------------------------------------------------------------------------
# Static frontend + reverse proxy to the API
# ---------------------------------------------------------------------------
FROM nginx:alpine AS web
COPY --from=web-build /build/dist /usr/share/nginx/html
COPY deploy/quickstart-nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
