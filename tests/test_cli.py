import subprocess
import sys

import pytest

from deskpilot import __version__
from deskpilot.cli import main


def test_version_output(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"deskpilot {__version__}"


def test_no_args_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: deskpilot" in capsys.readouterr().out


def test_unknown_command_fails() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code != 0


def test_python_dash_m_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "deskpilot", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"deskpilot {__version__}"
