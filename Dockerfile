FROM python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    PATH="/opt/filebrownie/.venv/bin:$PATH"

WORKDIR /opt/filebrownie
RUN pip install --no-cache-dir uv==0.8.22
# Fonts are for reproducible multilingual synthetic PDF fixtures, not OCR models.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen
COPY tests ./tests
CMD ["filebrownie", "--help"]
