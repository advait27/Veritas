"""Smoke tests for the package scaffold (M0)."""

import re

import veritas


def test_version_is_semver() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", veritas.__version__)


def test_default_server_name() -> None:
    assert veritas.DEFAULT_SERVER_NAME == "veritas"
