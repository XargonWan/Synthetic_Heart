#!/usr/bin/env python3
"""Test runner for the synth project.

Supports both unittest and pytest frameworks for maximum compatibility.
Produces JUnit XML output for GitHub Actions CI/CD.
"""

from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    """Run tests using the best available framework."""
    # Basic console logging so unexpected exceptions are visible.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Check if we're in GitHub Actions
    is_github = os.getenv("GITHUB_ACTIONS", "").lower() == "true"

    # Try pytest first (preferred for modern testing)
    try:
        import pytest

        # Build pytest arguments
        pytest_args = [
            "tests/",
            "-v",
            "--tb=short",
            "--strict-markers",
            "--disable-warnings",
        ]

        if is_github:
            # Create test results directory
            os.makedirs("test-results", exist_ok=True)
            pytest_args.extend(
                [
                    "--junitxml=test-results/junit.xml",
                    "--cov=core",
                    "--cov-report=xml:coverage.xml",
                    "--cov-report=term-missing",
                ]
            )

        # Run pytest
        result = pytest.main(pytest_args)
        return 0 if result == 0 else 1

    except ImportError:
        logging.warning("pytest not available, falling back to unittest")

    # Fallback to unittest
    try:
        import unittest

        # Discover tests
        suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")

        # Create test runner
        if is_github:
            try:
                import xmlrunner

                os.makedirs("test-results", exist_ok=True)
                with open(os.path.join("test-results", "unittest.xml"), "wb") as output:
                    runner = xmlrunner.XMLTestRunner(output=output, verbosity=2)
                    result = runner.run(suite)
            except ImportError:
                logging.warning("xmlrunner not available, using basic runner")
                runner = unittest.TextTestRunner(verbosity=2)
                result = runner.run(suite)
        else:
            runner = unittest.TextTestRunner(verbosity=2)
            result = runner.run(suite)

        return 0 if result.wasSuccessful() else 1

    except Exception as e:
        logging.exception(f"Failed to run tests: {e}")
        return 1


if __name__ == "__main__":  # pragma: no cover - manual execution
    try:
        sys.exit(main())
    except Exception:  # Catch any unexpected exception to ensure a proper exit code
        logging.exception("Unhandled exception during tests")
        sys.exit(1)
