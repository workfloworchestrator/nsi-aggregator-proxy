# syntax=docker/dockerfile:1@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
#
# Build stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:213ea9e877219e7bcb0f55070c9130c3fc84995475fca9eb8fb362916dc3f808 AS build
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY aggregator_proxy aggregator_proxy
RUN uv build --no-cache --wheel --out-dir dist

# Final stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:213ea9e877219e7bcb0f55070c9130c3fc84995475fca9eb8fb362916dc3f808
COPY --from=build /app/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl
RUN addgroup -g 1000 aggregator_proxy && adduser -D -u 1000 -G aggregator_proxy aggregator_proxy
USER aggregator_proxy
WORKDIR /home/aggregator_proxy
EXPOSE 8080/tcp
CMD ["aggregator-proxy"]
