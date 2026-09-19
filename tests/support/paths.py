"""Stable paths for tests regardless of their domain subdirectory."""

from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = PLUGIN_ROOT / "tests"
FIXTURES_ROOT = TESTS_ROOT / "fixtures"
