# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183
#
# Build stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:6e3e7a0061d007bf3d7f1b24c258b62f384ef14fbe2df673493d2b53e88b526b AS build
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGGREGATOR_PROXY=${VERSION}
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY aggregator_proxy aggregator_proxy
RUN uv build --no-cache --wheel --out-dir dist

# Final stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:6e3e7a0061d007bf3d7f1b24c258b62f384ef14fbe2df673493d2b53e88b526b
# Drops emSign ECC Root CA - G3 (ca-certificates 20261020); remove once the base image has it.
RUN apk upgrade --no-cache ca-certificates ca-certificates-bundle
COPY --from=build /app/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl
RUN addgroup -g 1000 aggregator_proxy && adduser -D -u 1000 -G aggregator_proxy aggregator_proxy
USER aggregator_proxy
WORKDIR /home/aggregator_proxy
EXPOSE 8080/tcp
CMD ["aggregator-proxy"]
