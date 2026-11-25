"""Hard ceilings. Code constants only: never read from settings or the environment.

Path resolution and matching live in the policy layer; this module is data.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# Upper bounds for run budgets. Settings above these values are clamped.
HARD_MAX: Final[Mapping[str, int]] = MappingProxyType(
    {
        "steps": 100,
        "model_calls": 100,
        "tool_calls": 200,
        "retries_per_operation": 5,
        "file_ops_per_run": 500,
        "browser_tabs": 10,
        "command_timeout_s": 120,
        "run_seconds": 1800,
        "tokens": 500_000,
        "strong_model_calls": 20,
    }
)

# Hard-deny zone templates (ARCHITECTURE section 4). Environment-variable and
# home-relative entries are expanded by the path policy, not here.
HARD_DENY_DIR_TEMPLATES: Final[tuple[str, ...]] = (
    "%WINDIR%",
    "%ProgramFiles%",
    "%ProgramFiles(x86)%",
    "%ProgramData%",
    "%APPDATA%",
    "%LOCALAPPDATA%",
    "~/.ssh",
    "~/.gnupg",
    "%LOCALAPPDATA%/Google/Chrome/User Data",
    "%LOCALAPPDATA%/Microsoft/Edge/User Data",
    "%APPDATA%/Mozilla/Firefox",
    "%LOCALAPPDATA%/Microsoft/Credentials",
    "%APPDATA%/Microsoft/Credentials",
    "%LOCALAPPDATA%/Microsoft/Vault",
    "%APPDATA%/Microsoft/Protect",
    "~/.aws",
    "~/.azure",
    "~/.config/gh",
    "~/.docker",
    "~/.kube",
)

# File name patterns denied everywhere, regardless of directory.
HARD_DENY_FILE_PATTERNS: Final[tuple[str, ...]] = (".env", "*.pem", "*.key")

# Drive roots and the DeskPilot settings directory are always denied.
HARD_DENY_DRIVE_ROOTS: Final[bool] = True
HARD_DENY_SETTINGS_DIR: Final[bool] = True
# The DeskPilot install directory is resolved at runtime by the path policy.
HARD_DENY_INSTALL_DIR: Final[bool] = True

# Risk classes whose handling settings can never relax.
ALWAYS_DENIED_RISK_CLASSES: Final[frozenset[str]] = frozenset({"forbidden"})
ALWAYS_APPROVED_RISK_CLASSES: Final[frozenset[str]] = frozenset({"dangerous"})
