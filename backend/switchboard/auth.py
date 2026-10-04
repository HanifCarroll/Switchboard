"""Require the isolated synthetic demo; other identity modes are unsupported."""

import os
from typing import Literal


def get_auth_mode() -> Literal["demo"]:
    """Fail closed if an old deployment still requests employee authentication."""
    if os.getenv("SWITCHBOARD_AUTH_MODE", "demo") != "demo":
        raise RuntimeError("Switchboard only supports SWITCHBOARD_AUTH_MODE=demo")
    return "demo"
