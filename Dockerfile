# syntax=docker/dockerfile:1@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
#
# Build stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:099503f2fe3e97d8b3c0bf972203a18594abf0f546599a04f457c658ee5b3943 AS build
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY aggregator_proxy aggregator_proxy
RUN uv build --no-cache --wheel --out-dir dist

# Final stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:099503f2fe3e97d8b3c0bf972203a18594abf0f546599a04f457c658ee5b3943
COPY --from=build /app/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl
RUN addgroup -g 1000 aggregator_proxy && adduser -D -u 1000 -G aggregator_proxy aggregator_proxy
USER aggregator_proxy
WORKDIR /home/aggregator_proxy
EXPOSE 8080/tcp
CMD ["aggregator-proxy"]
