# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32
#
# Build stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:3ad497bedc14ffd0831dcd757d3c09ac8dfdb1d89d3e1ec47bbcb76f64a97c21 AS build
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGGREGATOR_PROXY=${VERSION}
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY aggregator_proxy aggregator_proxy
RUN uv build --no-cache --wheel --out-dir dist

# Final stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:3ad497bedc14ffd0831dcd757d3c09ac8dfdb1d89d3e1ec47bbcb76f64a97c21
COPY --from=build /app/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl
RUN addgroup -g 1000 aggregator_proxy && adduser -D -u 1000 -G aggregator_proxy aggregator_proxy
USER aggregator_proxy
WORKDIR /home/aggregator_proxy
EXPOSE 8080/tcp
CMD ["aggregator-proxy"]
