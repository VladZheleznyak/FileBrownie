FROM python:3.12.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    PATH="/opt/filebrownie/.venv/bin:$PATH"

WORKDIR /opt/filebrownie
RUN pip install --no-cache-dir uv==0.8.22
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen
COPY tests ./tests
CMD ["filebrownie", "--help"]
