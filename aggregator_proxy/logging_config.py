"""Logging configuration using structlog.

Both structlog-native loggers and foreign stdlib loggers (e.g. uvicorn) are
routed through a structlog ``ProcessorFormatter``, ensuring consistent
formatting across all log sources.
"""

import logging

import structlog

from aggregator_proxy.settings import settings


def configure_logging() -> None:
    """Configure structlog and the stdlib root logger to share a single output pipeline.

    Both structlog-native loggers and foreign stdlib loggers (e.g. uvicorn) are
    routed through a structlog ``ProcessorFormatter``, ensuring consistent
    formatting across all log sources. The log level is read from settings.

    Access log records for ``/health`` are suppressed entirely via a
    ``logging.Filter`` attached to the ``uvicorn.access`` logger so that
    frequent liveness probes from load balancers and k8s do not appear in the
    logs at all.
    """
    numeric_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Applied to every log record regardless of origin.
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # wrap_for_formatter hands structlog-native records off to ProcessorFormatter
    # so they are rendered by the same pipeline as foreign (uvicorn) records.
    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Propagate uvicorn logs through the root handler instead of uvicorn's own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvi_logger = logging.getLogger(name)
        uvi_logger.handlers.clear()
        uvi_logger.propagate = True

    class _SuppressHealthCheck(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return " /health " not in record.getMessage()

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters.clear()
    access_logger.addFilter(_SuppressHealthCheck())
