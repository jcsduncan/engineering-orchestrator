FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d

ARG CLAUDE_CODE_VERSION=2.1.319
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && test "$(claude --version)" = "${CLAUDE_CODE_VERSION} (Claude Code)" \
    && useradd --create-home --uid 10001 engineer

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/
COPY images/claude/worker /usr/local/bin/claude-worker
RUN chmod 0755 /usr/local/bin/claude-worker \
    && mkdir -p /run/secrets

ENV DISABLE_UPDATES=1
USER engineer
WORKDIR /home/engineer
ENTRYPOINT ["claude-worker"]
