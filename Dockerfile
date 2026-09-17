# syntax=docker/dockerfile:1.7
ARG VERSION=0.0.0.dev0

FROM python:3.14-slim-bookworm AS builder
RUN pip install --no-cache-dir uv==0.11.7
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
ARG VERSION
RUN --mount=type=cache,target=/root/.cache/uv \
    sed -i "0,/^version = \".*\"/s//version = \"${VERSION}\"/" pyproject.toml \
    && sed -i "/name = \"open-webui-gpt-researcher\"/{n;s/^version = \".*\"/version = \"${VERSION}\"/;}" uv.lock \
    && uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install --no-install-recommends --yes chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 research \
    && useradd --uid 10001 --gid research --create-home --home-dir /home/research research \
    && mkdir -p /app /data/artifacts \
    && chown -R research:research /app /data
WORKDIR /app
COPY --from=builder --chown=research:research /app/.venv /app/.venv
COPY --chown=research:research alembic.ini ./
COPY --chown=research:research migrations ./migrations
COPY --chown=research:research openwebui_functions ./openwebui_functions
ARG VERSION
LABEL org.opencontainers.image.source="https://github.com/yevheniisemendiak/open-webui-gpt-researcher" \
      org.opencontainers.image.version="${VERSION}"
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION="${VERSION}"
USER 10001:10001
EXPOSE 8090
ENTRYPOINT ["open-webui-gpt-researcher"]
CMD ["api"]
