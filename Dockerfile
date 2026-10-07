# Two stages so the wheels are built once and the runtime image carries no
# build toolchain. The slim base is enough because shapely, pyproj and lxml all
# publish manylinux wheels, and pyshp is pure Python - no GDAL to compile.
FROM python:3.12-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.12-slim AS runtime

# Run unprivileged: the service writes only to its upload directory.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GEOAPI_STORAGE_DIR=/data/uploads

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY samples ./samples

RUN mkdir -p /data/uploads && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
