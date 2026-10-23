# ARIA-X — minimal, dependency-free image.
#
# ARIA-X is pure Python standard library, so this image installs no packages and
# works from a fully offline Docker build context (given a local base image).
#
# Build from the repository root:
#   docker build -f docker/Dockerfile -t aria-x:0.1.0 .
# Run (loopback-published, token-protected):
#   docker run --rm -p 127.0.0.1:8787:8787 \
#     -e ARIA_X_AUTH_TOKEN="$(openssl rand -hex 16)" \
#     -v "$PWD/data:/app/data" aria-x:0.1.0
#
# The container cannot reach host files except the mounted data volume. Target
# traffic still needs network access; see docker/kali-sandbox.md before enabling
# a bridge network for an engagement.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="ARIA-X" \
      org.opencontainers.image.description="Autonomous Reasoning & Investigation Agent, eXtended — authorized security research" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://example.invalid/aria-x"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    ARIA_X_DATA_DIR=/app/data

WORKDIR /app

# Copy only what is needed at runtime. All ARIA-X code is stdlib-only.
COPY LICENSE NOTICE.md THIRD_PARTY.md README.md ./
COPY src ./src

# Unprivileged runtime user; /app/data is the only writable location.
RUN addgroup --system --gid 10001 ariax \
 && adduser --system --uid 10001 --ingroup ariax --home /app ariax \
 && mkdir -p /app/data /app/runs \
 && chown -R ariax:ariax /app/data /app/runs
USER ariax

# Listen on all container interfaces; publish to 127.0.0.1 on the host only.
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3).status == 200 else 1)"

ENTRYPOINT ["python", "-m", "aria_x"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
