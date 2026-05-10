"""Pytest configuration for ``scripts/server/`` tests.

Adds a ``--run-e2e`` CLI flag and an ``e2e`` marker. Tests marked ``e2e`` are skipped by default and run only when
the flag is passed. The e2e tests hit real external APIs (PaperMC Fill v3, Geyser/Floodgate v2, Hangar) per
``.claude/rules/external-api-grounding.md``.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-e2e`` CLI option.

    Args:
        parser: Pytest's option parser.
    """
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Run e2e tests that hit real external APIs (skipped by default).",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``e2e`` marker so it is documented in ``--markers`` output.

    Args:
        config: Pytest's session config.
    """
    config.addinivalue_line("markers", "e2e: test hits real external APIs; opt in with --run-e2e.")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``@pytest.mark.e2e`` tests unless ``--run-e2e`` was passed.

    Args:
        config: Pytest's session config.
        items: Collected test items, mutated in place.
    """
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="e2e tests skipped by default; pass --run-e2e to enable.")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)
