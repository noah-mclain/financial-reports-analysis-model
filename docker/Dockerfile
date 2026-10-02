# One image for the api and the worker. Built from the repository root: docker compose build.
#
# A frozen sync of uv.lock, so the image has exactly the locked, hash-checked packages. The lock
# is what keeps the image lean: on Linux it resolves torch and torchvision to the CPU wheels (no
# CUDA libraries, no triton), and the `mac` extra's ocrmac edge carries a darwin marker, so
# ocrmac, and pyobjc which only it pulls in, are never installed here.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src

# Dependencies first, so editing source does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-workspace

COPY apps apps
COPY packages packages
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app
COPY --from=build /opt/venv /opt/venv
COPY configs /app/configs
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FRA_PROFILE=docker \
    FRA_ROOT=/app
WORKDIR /app
USER app
# FRA_API_PORT comes from compose; there is no second copy of it here.
CMD ["sh", "-c", "exec uvicorn fra_api.main:app --host 0.0.0.0 --port ${FRA_API_PORT:?FRA_API_PORT is not set}"]
