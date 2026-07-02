"""Shared PostHog analytics client for all Beacon services."""
import atexit
import os

from posthog import Posthog

_POSTHOG_PROJECT_TOKEN = os.environ.get("POSTHOG_PROJECT_TOKEN", "")
_POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
_POSTHOG_DISABLED = os.environ.get("POSTHOG_DISABLED", "false").lower() == "true"

posthog_client: Posthog | None = None

if _POSTHOG_PROJECT_TOKEN and not _POSTHOG_DISABLED:
    posthog_client = Posthog(
        _POSTHOG_PROJECT_TOKEN,
        host=_POSTHOG_HOST,
        enable_exception_autocapture=True,
    )
    atexit.register(posthog_client.shutdown)
