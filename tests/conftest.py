"""Shared test configuration."""

import os

# Set required environment variables before any application module is imported.
os.environ.setdefault("AGGREGATOR_PROXY_PROVIDER_URL", "http://aggregator.test/nsi-v2/ConnectionServiceProvider")
os.environ.setdefault("AGGREGATOR_PROXY_BASE_URL", "http://proxy.test")
