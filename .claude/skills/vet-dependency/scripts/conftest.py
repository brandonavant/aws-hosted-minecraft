"""Pytest configuration for vet-dependency script tests."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-e2e`` CLI option.

    Args:
        parser: The pytest CLI argument parser.
    """
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run end-to-end tests that hit real APIs.",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``e2e`` marker.

    Args:
        config: The pytest configuration object.
    """
    config.addinivalue_line("markers", "e2e: end-to-end test (hits real APIs)")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip e2e-marked tests unless ``--run-e2e`` is passed.

    Args:
        config: The pytest configuration object.
        items: The collected test items.
    """
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="pass --run-e2e to run")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
