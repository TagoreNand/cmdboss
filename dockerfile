# =============================================================================
# CMDBoss — Dockerfile
# Multi-stage build: keeps the final image lean and production-ready.
# =============================================================================

# ---- Stage 1: Dependency builder ----
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt


# ---- Stage 2: Runtime image ----
FROM python:3.11-slim AS runtime

LABEL maintainer="Your Name <you@example.com>"
LABEL description="CMDBoss — API-driven Configuration Management Database"
LABEL version="1.0.0"

# Create a non-root user for security
RUN groupadd --system cmdboss && useradd --system --gid cmdboss cmdboss

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=cmdboss:cmdboss . .

# Expose the API port
EXPOSE 8000

# Switch to non-root user
USER cmdboss

# Health check — polls the root endpoint every 30s
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/')" || exit 1

# Start via Gunicorn with Uvicorn workers (package entrypoint: cmdboss/app.py)
CMD ["gunicorn", "cmdboss.app:app", "--config", "gunicorn.conf.py"]
