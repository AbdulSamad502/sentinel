# Sentinel's hosted brain, as Bedrock AgentCore Runtime wants it.
#
# AgentCore runs an **arm64** container that listens on port 8080 and answers
# `POST /invocations` and `GET /ping`. That is the whole contract, and
# `sentinel/interfaces/agentcore_app.py` is the whole implementation.
#
# Build from the repo root, not from this directory:
#   docker build --platform linux/arm64 -f deploy/Dockerfile -t sentinel-runtime .
#
# See docs/deploy-agentcore.md for the deployment around it.

FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

# The repo's own requirements, deliberately, rather than a trimmed copy for the
# container. A second dependency list is a second thing to keep in sync, and it
# drifts silently. The Ollama extra is a few unused kilobytes here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel/ ./sentinel/
COPY sentinel.config.json .

# The model provider inside the runtime. The service refuses to start on
# `agentcore` - that would make it a proxy to itself.
ENV SENTINEL_PROVIDER=bedrock \
    PYTHONUNBUFFERED=1

# Nothing here needs to be root, and the runtime's whole job is to accept
# strangers' input.
RUN useradd --create-home --uid 10001 sentinel
USER sentinel

EXPOSE 8080

CMD ["python", "-m", "sentinel.interfaces.agentcore_app"]