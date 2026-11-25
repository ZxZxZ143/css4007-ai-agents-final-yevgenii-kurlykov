"""Shared pytest fixtures for DeskPilot tests."""

import contextlib
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    r"""Scratch dir under tests/.work.

    pytest's default tmp_path lives in %LOCALAPPDATA%\Temp, which the path policy
    hard-denies, so tests that use tmp_path as an allowed root need a dir elsewhere.
    """
    base = Path(__file__).parent / ".work" / uuid.uuid4().hex
    base.mkdir(parents=True)
    yield base
    shutil.rmtree(base, ignore_errors=True)
    with contextlib.suppress(OSError):
        base.parent.rmdir()
