# Python's official image supports ARM64; build with --platform linux/arm64.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENTCORE_SANDBOX_WORKSPACE=/workspace \
    AGENTCORE_SANDBOX_BOOT_FILE=/app/run/boot.json

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN mkdir -p /app/build-tmp \
    && TMPDIR=/app/build-tmp pip install --no-cache-dir . \
    && rm -rf /app/build-tmp \
    && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
    && mkdir -p /workspace /app/run \
    && chown sandbox:sandbox /workspace /app/run

USER sandbox
EXPOSE 8080
CMD ["python3", "-m", "agentcore_sandbox.server", "--host", "0.0.0.0", "--port", "8080"]
