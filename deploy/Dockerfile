# The agent as it runs on a server (deploy/server/README.md). Built from a staged copy of the repo at HEAD with
# agent-coding/shared copied in as ./shared (deploy/server/atlas-server.sh does the staging).
FROM python:3.12-slim-bookworm
WORKDIR /app
# HOME is on the data volume: the Claude CLI keeps the agent's sessions in ~/.claude, the LLM inbox is under
# ~/Library/Application Support, and the Roam backup's SSH key is in ~/.ssh. All three must outlive the container.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    EA_DATA=/data EA_CONFIG=/app/config.toml EA_CONFIG_OVERLAY=/app/config.server.toml EA_SHARED_DIR=/app/shared \
    HOME=/data/home PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# git + ssh: the Roam backup is a git checkout that is pulled every few hours. libatomic1: the Node that Playwright
# bundles will not start without it, and the slim image does not have it (build failed 2026-11-17).
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates tzdata curl libatomic1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright
COPY ea ./ea
COPY instructions ./instructions
COPY shared ./shared
# Not root: the Claude CLI refuses to run tools without permission prompts as root.
USER 1000:1000
EXPOSE 8800
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s CMD curl -fsS http://127.0.0.1:8800/health || exit 1
CMD ["python", "-m", "ea", "serve"]
