# ============================================
# Stage 1: builder — system build deps + Python packages.
# Only rebuilds when requirements.txt changes.
# ============================================
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=0 \
    PYTHONPATH=/app

WORKDIR /app

# System dependencies and latest security upgrades
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Python dependencies — cached separately from app code
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    && rm -rf ~/.cache/pip

# ============================================
# Stage 2: final — runtime deps + app code only.
# Rebuilds fast: only copies source files.
# ============================================
FROM python:3.11-slim-bookworm AS final

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=0 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Security upgrades and non-root user creation
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && useradd -m -u 1000 celery

# Python runtime dependencies from builder; build-essential stays out of final image.
COPY --from=builder /opt/venv /opt/venv

# Application runtime and operator scripts
COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh ./docker-entrypoint.sh

# Required directories + ownership
# /app/beat is used by celery beat for schedule persistence (named volume mounted at runtime).
# The volume is created by Docker as root — pre-create the dir here so the chown covers it
# before the volume is mounted. At runtime the entrypoint also creates it defensively.
RUN mkdir -p /app/imports/processed /app/beat && \
    chown -R celery:celery /app

# Fix line endings (Windows CRLF -> LF) and make entrypoint executable.
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh

USER celery

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["bash"]
