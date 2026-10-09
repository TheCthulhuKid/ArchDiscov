# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.5.29 AS uv

FROM python:3.13-slim-bookworm AS development

COPY --from=uv /uv /uvx /usr/local/bin/

ARG DEV_UID=1000
ARG DEV_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/home/dev/.cache/uv \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/dev

WORKDIR /workspace

RUN groupadd --gid "${DEV_GID}" dev \
    && useradd --uid "${DEV_UID}" --gid "${DEV_GID}" --create-home --shell /bin/bash dev \
    && mkdir -p /opt/venv /home/dev/.cache/uv \
    && chown -R dev:dev /opt/venv /workspace /home/dev/.cache

USER dev

# Keep dependency installation cached when only application source changes.
COPY --chown=dev:dev pyproject.toml uv.lock ./
RUN --mount=type=cache,id=archdiscov-uv-${DEV_UID}-${DEV_GID},target=/home/dev/.cache/uv,uid=${DEV_UID},gid=${DEV_GID} \
    uv sync --frozen --group dev --no-install-project

COPY --chown=dev:dev . .
RUN --mount=type=cache,id=archdiscov-uv-${DEV_UID}-${DEV_GID},target=/home/dev/.cache/uv,uid=${DEV_UID},gid=${DEV_GID} \
    uv sync --frozen --group dev

CMD ["bash"]
