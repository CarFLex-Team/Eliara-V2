# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/srv/eliara/.hf

WORKDIR /srv/eliara

# The project itself is installed, so the build backend needs the package
# sources present — pyproject.toml alone is not enough.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir ".[ml]"

# Bake the embedding model into the image: runtime needs NO internet access.
RUN python -c "from sentence_transformers import SentenceTransformer; \
SentenceTransformer('BAAI/bge-base-en-v1.5')"

COPY scripts ./scripts
COPY companies ./companies
COPY companies.yaml ./companies.yaml

# Mount points, created up front so bind/volume targets inherit sane
# ownership. One data subdir per company; companies.yaml is the single
# source of truth for which companies exist and their db paths (relative
# to this WORKDIR).
RUN mkdir -p /srv/eliara/data/companies/beta /srv/eliara/data/companies/tire_guru \
             /srv/eliara/cache /srv/eliara/audit

EXPOSE 8000
# start_period covers model load on a cold container; without it the first
# ~30s of startup gets counted as failed health checks
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=90s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
