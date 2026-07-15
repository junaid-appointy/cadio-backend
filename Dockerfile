# CADIO backend — API only (no frontend). The React app ships as its own image
# (frontend/Dockerfile) and reaches this over the network; see docker-compose.yml.
# For the old single-origin combined image, git history has the multi-stage build.
FROM python:3.12-slim-bookworm

# System libraries the CAD/vision stack loads at runtime:
#   libGL/libglib/libX* — OpenCASCADE (OCP) + matplotlib/opencv
#   libgomp1            — OpenMP (manifold3d, fast-simplification)
#   fontconfig          — matplotlib text rendering (headless PNG renders)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libx11-6 \
        libxext6 \
        libxrender1 \
        libsm6 \
        libgomp1 \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# heavy CAD wheels (build123d/OCP, trimesh, manifold3d, opencv) + psycopg + boto3.
# cache mount keeps wheels across rebuilds without bloating the image layer.
COPY pyproject.toml ./
COPY cadio/ ./cadio/
RUN --mount=type=cache,target=/root/.cache/pip pip install .

# starter program served by GET /api/example (read from /app/examples at runtime)
COPY examples/ ./examples/

# runtime data (SQLite when no DATABASE_URL, plus the local artifact cache that
# fronts R2) lives on a volume so it survives container restarts.
ENV CADIO_HOME=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
# run from /app source (see PYTHONPATH); reload OFF (drops WS + recycles CAD pool)
CMD ["python", "-m", "uvicorn", "cadio.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
