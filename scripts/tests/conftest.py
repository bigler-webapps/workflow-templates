"""Make `scripts/kuma_sync.py` importable without the Kuma client installed.

`kuma_sync` does `from uptime_kuma_api import UptimeKumaApi` at module level and
`sys.exit(1)` on ImportError, so a plain `import kuma_sync` would kill the test
process wherever the package is absent (it is not a test-time dependency — it is
installed only by the composite actions that run the sync). Stubbing the module
before import keeps the production import path untouched.
"""

import sys
import types
from enum import Enum
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

if "uptime_kuma_api" not in sys.modules:
    stub = types.ModuleType("uptime_kuma_api")

    class UptimeKumaApi:  # noqa: D401 - stand-in, never instantiated in tests
        def __init__(self, *args, **kwargs):
            raise AssertionError("tests must not open a real Kuma connection")

    stub.UptimeKumaApi = UptimeKumaApi
    sys.modules["uptime_kuma_api"] = stub


class MonitorType(str, Enum):
    """Mirrors the real client's enum closely enough for the comparison tests.

    The shape is what matters: `uptime_kuma_api` converts a monitor's `type`
    into an enum member ON READ, and for a `(str, Enum)` member `str()` yields
    "MonitorType.HTTP", not "http" — the CI-4 root cause.
    """

    HTTP = "http"
    PING = "ping"
    PORT = "port"
