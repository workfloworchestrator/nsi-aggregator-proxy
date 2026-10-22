# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# Build stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:1fd17509ae6051e6cad9dc1b83705483c0b24a15740b3c4d230b6b6f2e2a0241 AS build
ARG VERSION
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGGREGATOR_PROXY=${VERSION}
WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY aggregator_proxy aggregator_proxy
RUN uv build --no-cache --wheel --out-dir dist

# Final stage
FROM ghcr.io/astral-sh/uv:python3.13-alpine@sha256:1fd17509ae6051e6cad9dc1b83705483c0b24a15740b3c4d230b6b6f2e2a0241
# Drops emSign ECC Root CA - G3 (ca-certificates 20261020); remove once the base image has it.
RUN apk upgrade --no-cache ca-certificates ca-certificates-bundle
COPY --from=build /app/dist/*.whl /tmp/
RUN uv pip install --system --no-cache /tmp/*.whl && rm /tmp/*.whl
RUN addgroup -g 1000 aggregator_proxy && adduser -D -u 1000 -G aggregator_proxy aggregator_proxy
USER aggregator_proxy
WORKDIR /home/aggregator_proxy
EXPOSE 8080/tcp
CMD ["aggregator-proxy"]
