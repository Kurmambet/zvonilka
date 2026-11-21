FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY zvonilka/backend/src/ zvonilka/backend/src/
COPY zvonilka/backend/alembic.ini zvonilka/backend/alembic.ini
COPY zvonilka/backend/migrations/ zvonilka/backend/migrations/

RUN uv sync --locked --no-dev --no-editable

WORKDIR /app/zvonilka/backend
USER 10001:10001

# CMD ["python", "-m", "uvicorn", "zvonilka.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "5"]
CMD ["python", "-m", "zvonilka.server", "--host", "0.0.0.0"]
