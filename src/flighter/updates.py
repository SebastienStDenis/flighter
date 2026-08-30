"""The running build, the published one, and the handoff that swaps them.

The release workflow publishes an image on every push to main, and Watchtower is what
actually pulls it: the app cannot recreate its own container from inside it, and giving
it the Docker socket to try would hand the whole machine to whatever reaches the port.
So the app's share of an update is deliberately small: know which commit it was built
from (a build argument the workflow stamps into the image), ask the registry which
commit the tag now carries, and ask Watchtower over its HTTP API to do the swap.

Watchtower does the work inside the request, which makes the reply upside down: a quick
answer means nothing was pulled for this container, and no answer at all is the update
arriving - the process this code runs in is the one being stopped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# Stamped by the release workflow (a build argument the Dockerfile turns into this
# variable), so a container knows the commit it was built from. Empty in a checkout and
# in images from before the workflow stamped it, which reads as "cannot say" rather
# than breaking anything: the button still works, only the comparison goes quiet.
RUNNING_SHA: Final = os.environ.get("FLIGHTER_BUILD_SHA", "")

# What the compose stack pulls, which is what "a new version" is measured against.
IMAGE: Final = os.environ.get("FLIGHTER_IMAGE", "ghcr.io/sebastienstdenis/flighter:latest")

# The standard OCI label the release workflow's metadata step already writes.
REVISION_LABEL: Final = "org.opencontainers.image.revision"

# A fresh answer is good for an hour: builds land on pushes to main, not by the minute,
# and the settings page should not put four registry round trips behind every visit.
CHECK_EVERY: Final = 3600.0

_MANIFEST_TYPES: Final = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str

    @property
    def name(self) -> str:
        """The ref with the tag off, which is what Watchtower filters containers by."""
        return f"{self.registry}/{self.repository}"

    def manifest_url(self, reference: str) -> str:
        return f"https://{self.registry}/v2/{self.repository}/manifests/{reference}"

    def blob_url(self, digest: str) -> str:
        return f"https://{self.registry}/v2/{self.repository}/blobs/{digest}"


def parse_image(ref: str) -> ImageRef:
    """Split registry, repository and tag the way docker itself reads a ref.

    The first path component is a registry only when it could be a hostname - a dot or
    a port in it - which is how plain "postgres:16" stays a Docker Hub name rather than
    a machine called postgres.
    """
    path, _, last = ref.rpartition("/")
    name, colon, tag = last.partition(":")
    if not colon:
        tag = "latest"
    path = f"{path}/{name}" if path else name
    first, slash, rest = path.partition("/")
    if slash and ("." in first or ":" in first):
        return ImageRef(first, rest, tag)
    return ImageRef("registry-1.docker.io", path if slash else f"library/{path}", tag)


async def _anonymous_token(client: httpx.AsyncClient, challenge: str) -> str | None:
    """The registry names its own token desk in the 401; public pulls get one for free."""
    if not challenge.lower().startswith("bearer"):
        return None
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.get("realm")
    if not realm:
        return None
    params = {name: value for name, value in fields.items() if name in ("service", "scope")}
    response = await client.get(realm, params=params)
    response.raise_for_status()
    payload = response.json()
    token = payload.get("token") or payload.get("access_token")
    return token if isinstance(token, str) else None


async def _registry_get(
    client: httpx.AsyncClient, auth: dict[str, str], url: str, accept: str
) -> httpx.Response:
    """One registry request, picking up the anonymous bearer token on the first 401.

    `auth` is shared across the walk from index to config so the dance happens once,
    not once per hop.
    """
    response = await client.get(url, headers={"Accept": accept, **auth})
    if response.status_code == 401 and not auth:
        token = await _anonymous_token(client, response.headers.get("www-authenticate", ""))
        if token is not None:
            auth["Authorization"] = f"Bearer {token}"
            response = await client.get(url, headers={"Accept": accept, **auth})
    response.raise_for_status()
    return response


def _annotated(payload: Mapping[str, Any]) -> str | None:
    value = (payload.get("annotations") or {}).get(REVISION_LABEL)
    return value if isinstance(value, str) and value else None


def _platform_digest(index: Mapping[str, Any]) -> str | None:
    """The digest of a real platform's manifest.

    Buildx attaches attestation manifests to the same index under platform
    unknown/unknown, and those carry no image config worth reading.
    """
    for entry in index.get("manifests", ()):
        platform = entry.get("platform") or {}
        if platform.get("os") in (None, "unknown"):
            continue
        digest = entry.get("digest")
        if isinstance(digest, str):
            return digest
    return None


async def published_revision(ref: ImageRef, client: httpx.AsyncClient) -> str | None:
    """The commit the registry's copy of the tag was built from, or None if it is unsaid.

    The release workflow labels every image with its commit, so the answer is in the
    image config; index and manifest annotations are read on the way because either
    is the same answer one or two round trips sooner when present.
    """
    auth: dict[str, str] = {}
    response = await _registry_get(client, auth, ref.manifest_url(ref.tag), _MANIFEST_TYPES)
    payload: dict[str, Any] = response.json()
    if revision := _annotated(payload):
        return revision
    if "manifests" in payload:
        digest = _platform_digest(payload)
        if digest is None:
            return None
        response = await _registry_get(client, auth, ref.manifest_url(digest), _MANIFEST_TYPES)
        payload = response.json()
        if revision := _annotated(payload):
            return revision
    config = payload.get("config") or {}
    digest = config.get("digest")
    if not isinstance(digest, str):
        return None
    response = await _registry_get(client, auth, ref.blob_url(digest), "application/octet-stream")
    labels = (response.json().get("config") or {}).get("Labels") or {}
    value = labels.get(REVISION_LABEL)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class UpdateStatus:
    running: str
    latest: str | None = None
    error: str | None = None
    checked: float | None = None

    @property
    def available(self) -> bool | None:
        """Three-valued on purpose: with either commit unknown, "no" would be a guess."""
        if not self.running or not self.latest:
            return None
        return self.latest != self.running


_status = UpdateStatus(running=RUNNING_SHA)
_lock = asyncio.Lock()


async def status(
    *, refresh: bool = True, transport: httpx.AsyncBaseTransport | None = None
) -> UpdateStatus:
    """What is running against what is published, from the cache when it is fresh.

    A failed read keeps the last answer but is not stamped as fresh, so the registry is
    asked again on the next visit rather than the failure standing for an hour.
    """
    global _status
    if not refresh:
        return _status
    async with _lock:
        if _status.checked is not None and time.monotonic() - _status.checked < CHECK_EVERY:
            return _status
        try:
            # Blobs come back as a redirect onto the registry's CDN, which httpx does
            # not follow on its own (and whose cross-origin hop drops our bearer token,
            # as it should - the redirect carries its own signature).
            async with httpx.AsyncClient(
                timeout=10, transport=transport, follow_redirects=True
            ) as client:
                latest = await published_revision(parse_image(IMAGE), client)
        except Exception as exc:
            log.warning("could not read the registry for %s", IMAGE, exc_info=True)
            _status = UpdateStatus(RUNNING_SHA, _status.latest, str(exc))
        else:
            _status = UpdateStatus(RUNNING_SHA, latest, None, time.monotonic())
    return _status


def api_base(settings: Settings) -> str:
    """The Watchtower API as an origin, with the scheme nobody types put on for them."""
    url = settings.watchtower_url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    return url


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def probe(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[bool, str]:
    """Prove the address leads to Watchtower's API without asking it to update anything.

    The metrics endpoint shares the update API's token check, so where it is switched on
    the token is proven outright. Where it is not (a 404), the update endpoint is asked
    with a token that is wrong on purpose: a 401 back proves Watchtower is there and
    guarding the door, and is as far as a check can go, because the right token would
    run the update on the spot.
    """
    base = api_base(settings)
    async with httpx.AsyncClient(timeout=10, transport=transport) as client:
        try:
            answer = await client.get(
                f"{base}/v1/metrics", headers=_bearer(settings.watchtower_token)
            )
        except Exception as exc:
            return False, f"could not reach Watchtower at {base}: {exc}"
        if answer.status_code == 200:
            return True, "token accepted"
        if answer.status_code == 401:
            return False, "Watchtower rejected the token"
        try:
            # POST, not GET: the maintained fork (nickfedor/watchtower) routes the
            # update endpoint for POST alone and answers anything else 405 before its
            # auth is ever consulted, while the original wraps auth around every
            # method. A POST with a wrong token is refused at the door by both.
            answer = await client.post(f"{base}/v1/update", headers=_bearer("deliberately-wrong"))
        except Exception as exc:
            return False, f"could not reach Watchtower at {base}: {exc}"
    if answer.status_code == 401:
        return True, "Watchtower is answering; the token itself is only proven by an update"
    return False, (
        f"HTTP {answer.status_code} from {base}/v1/update; is "
        "WATCHTOWER_HTTP_API_UPDATE=true set on the Watchtower container?"
    )


@dataclass(frozen=True)
class Outcome:
    ok: bool
    detail: str
    restarting: bool = False


def _note_late_answer(task: asyncio.Task[httpx.Response]) -> None:
    """Where the update request lands when it outlives the page that asked for it.

    Usually nowhere: the common way for the wait to run out is this process being
    replaced, and a process that was not is worth a line saying how the request ended.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("the update request failed after the page stopped waiting: %s", exc)
    else:
        log.info(
            "watchtower answered HTTP %s after the page stopped waiting", task.result().status_code
        )


async def trigger(
    settings: Settings,
    *,
    wait: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Outcome:
    """Ask Watchtower for the update, and read its silence correctly.

    Watchtower answers only once every container it looked at is dealt with, and
    dealing with this one means stopping this process. So a quick answer means nothing
    was pulled for us - a refusal, or nothing newer - and `wait` running out means the
    update is under way. The request is left running rather than cancelled: a pull that
    turns out slow rather than fatal still deserves to finish, and its outcome goes to
    the log.
    """
    base = api_base(settings)

    async def ask() -> httpx.Response:
        # Scoped to this update alone rather than filtering nothing: on a stack whose
        # Watchtower minds other containers too, the button is about this app only.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(600, connect=5), transport=transport
        ) as client:
            return await client.post(
                f"{base}/v1/update",
                params={"image": parse_image(IMAGE).name},
                headers=_bearer(settings.watchtower_token),
            )

    task = asyncio.create_task(ask())
    done, _ = await asyncio.wait({task}, timeout=wait)
    if not done:
        task.add_done_callback(_note_late_answer)
        return Outcome(True, "Watchtower is updating.", restarting=True)
    try:
        response = task.result()
    except Exception as exc:
        return Outcome(False, f"could not reach Watchtower at {base}: {exc}")
    if response.status_code == 401:
        return Outcome(False, "Watchtower rejected the token.")
    if response.status_code == 429:
        # The fork holds one update at a time and says so; the original never sends it
        # for a targeted request.
        return Outcome(False, "Watchtower is busy with another update; try again in a moment.")
    if response.status_code == 202:
        # Accepted-but-not-done, from the fork's async mode: the outcome is unknown, so
        # it is watched for the way silence is.
        return Outcome(True, "Watchtower is updating.", restarting=True)
    if response.status_code != 200:
        return Outcome(False, f"Watchtower answered HTTP {response.status_code}.")
    # Answering at all means this container was not restarted, and that means there
    # was nothing newer to restart it onto.
    return Outcome(True, "Watchtower found nothing newer to pull.")
