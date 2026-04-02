"""Tests for structlog logging configuration."""

import structlog


def test_context_class_defaults_to_dict() -> None:
    """Verify that structlog's default context_class is dict.

    The configure_logging() call passes ``context_class=dict`` explicitly.
    This test confirms that ``dict`` is already the default, so the parameter
    can safely be removed without changing behaviour.
    """
    # Reset to structlog defaults
    structlog.reset_defaults()

    defaults = structlog.get_config()
    assert defaults["context_class"] is dict
