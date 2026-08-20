"""The Google client, and the consent flow that fills in the refresh token.

The flow is the browser redirect one: it hands the token straight back to the running
app, so the only thing left for a person to do is click through Google's screens.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from .config import Settings, write_secret

log = logging.getLogger(__name__)

# Write one calendar, and nothing else. Creating the calendar the app writes into needs
# `calendars.insert`, which the read-only scope does not cover.
SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

CALLBACK_PATH = "/settings/google/callback"


def callback_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{CALLBACK_PATH}"


def client_config(settings: Settings, redirect_uri: str) -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def credentials(settings: Settings) -> Credentials:
    """Credentials from the stored refresh token; the access token is minted on demand."""
    return Credentials(
        token=None,
        refresh_token=settings.google_refresh_token,
        token_uri=TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )


def _flow(settings: Settings, redirect_uri: str, state: str | None = None) -> Flow:
    flow = Flow.from_client_config(
        client_config(settings, redirect_uri), scopes=SCOPES, state=state
    )
    # Set on the flow as well as in the client config: the library sends what is here,
    # and Google rejects an authorisation request that names no redirect at all.
    flow.redirect_uri = redirect_uri
    return flow


def consent_url(settings: Settings, redirect_uri: str) -> tuple[str, str]:
    """The URL to send the browser to, and the state to check when it comes back.

    `prompt=consent` every time: Google only returns a refresh token on a fresh grant,
    and re-authorising after one expired is exactly when you need a new one.
    """
    url, state = _flow(settings, redirect_uri).authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return str(url), str(state)


async def exchange_code(settings: Settings, redirect_uri: str, state: str, code: str) -> Settings:
    """Trade the callback's code for a refresh token and store it.

    Blocking, like every Google client call here, so it crosses into a worker thread
    rather than parking the event loop on someone else's round trip.
    """
    flow = _flow(settings, redirect_uri, state=state)
    await asyncio.to_thread(flow.fetch_token, code=code)
    token = getattr(flow.credentials, "refresh_token", None)
    if not token:
        raise RuntimeError(
            "Google returned no refresh token. Revoke this app's access in your Google "
            "account and connect again."
        )
    return write_secret("GOOGLE_REFRESH_TOKEN", str(token))
