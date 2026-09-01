# ============================================================================
# Stage 1: Build frontend
# ============================================================================
FROM node:22-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS frontend-build
# node:22-slim digest resolved 2026-07-28 (keep in sync with vibe-trading)

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend/ ./
RUN npm run build

# ============================================================================
# Stage 2: Python builder — compiles wheels + builds a self-contained venv.
# build-essential lives ONLY here; it never reaches the runtime image.
# ============================================================================
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS builder
# python:3.11-slim digest resolved 2026-07-13

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv we can copy wholesale into the runtime stage.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

# Python deps first for layer caching — hash-pinned lock.
# Task 9: psycopg[binary]>=3.1 + psycopg_pool via requirements-lock (hash-pinned, no extra apt needed; libpq via binary wheel).
COPY requirements-lock.txt requirements-lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements-lock.txt

# Copy project + install the entrypoint (editable — runtime re-creates same src tree).
# --no-deps because every dependency is already installed from the lock above.
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir --no-deps -e .

# ============================================================================
# Stage 3: Runtime — carries the prebuilt venv only, no compilers/dev headers.
# ============================================================================
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS runtime
# python:3.11-slim digest resolved 2026-07-13

LABEL org.opencontainers.image.title="hero-quant" \
    org.opencontainers.image.description="hero-quant - minimal quant research agent (single-machine Docker)" \
    org.opencontainers.image.version="0.2.0" \
    org.opencontainers.image.source="https://github.com/your-org/hero-quant" \
    org.opencontainers.image.licenses="MIT"

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime-only native libs. Keep minimal; these are weasyprint's deps (Pango/HarfBuzz/
# Fontconfig/Cairo/gdk-pixbuf) per vibe-trading — optional for hero-quant PDF export
# but harmless and keeps parity. fonts-dejavu-core gives non-blank PDFs. curl is
# optional — healthcheck uses python urllib so no curl is required, but kept if you
# switch to curl-based check.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    libfontconfig1 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Bring in the prebuilt venv from the builder stage.
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
COPY --from=builder /opt/venv /opt/venv

# Re-materialize the source tree the editable install references, plus the
# built frontend static assets.
COPY pyproject.toml README.md ./
COPY src/ src/
COPY --from=frontend-build /app/frontend/dist frontend/dist

# Runtime should not run as root. `vibe` owns the writable app-data dirs so
# named volumes inherit usable permissions. `vibe-sandbox` is an unprivileged
# system account (no home, no shell) that runner/sandbox drops into via
# subprocess.run(user="vibe-sandbox") to execute LLM-generated code — created
# here by fixed contract (uid 10001), not otherwise used.
RUN useradd --create-home --shell /usr/sbin/nologin vibe \
    && useradd --system --no-create-home --shell /usr/sbin/nologin --uid 10001 vibe-sandbox \
    && mkdir -p agent/runs agent/sessions /home/vibe/.hero-quant \
    && chown -R vibe:vibe /app /home/vibe/.hero-quant
USER vibe

# Default port
EXPOSE 8899

# Health check — hits /live (liveness probe)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8899/live')" || exit 1

# Run API server (serves frontend/dist as static files)
CMD ["python", "-m", "uvicorn", "hero_quant.api.server:app", "--host", "0.0.0.0", "--port", "8899"]
