"""Startup banner for z8l-agent SDK.

Prints a welcome message with helpful links when the SDK is first imported.
Can be suppressed by setting the OPENHANDS_SUPPRESS_BANNER environment variable.
"""

import os
import sys


# Not guarded by a lock; worst case in a race is the banner prints twice.
_BANNER_PRINTED = False


def _print_banner(version: str) -> None:
    """Print the z8l-agent SDK startup banner to stderr."""
    global _BANNER_PRINTED

    # Check if banner should be suppressed (check this first, before setting flag)
    suppress = os.environ.get("OPENHANDS_SUPPRESS_BANNER", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if suppress:
        return

    if _BANNER_PRINTED:
        return
    _BANNER_PRINTED = True

    banner = f"""\
+----------------------------------------------------------------------+
|  z8l-agent SDK v{version:<53}|
|                                                                      |
|  Report a bug: github.com/z8l-agent/software-agent-sdk/issues        |
|  Get help: z8l-agent.dev/joinslack                                   |
|  Scale up: z8l-agent.dev/product/sdk                                 |
|                                                                      |
|  Set OPENHANDS_SUPPRESS_BANNER=1 to hide this message                |
+----------------------------------------------------------------------+
"""
    print(banner, file=sys.stderr)
