"""Which build is running, whether a newer one is published, and how to take it.

Two questions that look like one. "Am I behind?" is answerable from inside the
container by anyone who can reach the registry: the image the deployment pulls carries
the commit it was built from, and the registry says which commit `:latest` is now.
"Update me" is not. A process in a container cannot pull an image or replace the
container it is running in - exiting only hands the same image back to the restart
policy - and the only way to do it from in here is a mounted Docker socket, which is
root on the host in exchange for a button.

So the button asks the thing that already holds that socket. Watchtower runs beside the
app in the same stack for exactly this job, and with its HTTP API turned on a POST is
the same update it would have made by itself on its next poll. Nothing here touches
Docker; the app knows a URL and a token, and the container it is asking about is its
own - which is why the answer to that request never arrives.
"""

from __future__ import annotations

import logging
from typing import Any, Final, NamedTuple

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

# The package this deployment pulls. Written here rather than worked out, because a
# container cannot see the name it was started from: an image knows its own contents and
# nothing about the tag anybody fetched it under.
REGISTRY: Final = "https://ghcr.io"
IMAGE: Final = "sebastienstdenis/flighter"
TAG: Final = "latest"

# The label every image built by the release workflow carries, written by
# docker/metadata-action: the commit the image was built from.
REVISION_LABEL: Final = "org.opencontainers.image.revision"

# A registry hands back an index of per-platform manifests or a single manifest, and
# says which by content type. Both are asked for at once.
MANIFEST_TYPES: Final = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
INDEX_TYPES: Final = MANIFEST_TYPES[:2]

HTTP_TIMEOUT_SECONDS: Final = 15.0


class Update(NamedTuple):
    """What the settings page says about this deployment's version.

    `running` is empty on a build that did not come from the release workflow - a local
    `docker compose build`, or a checkout - and then there is nothing to compare and
    `behind` is False whatever the registry says.
    """

    running: str
    published: str
    behind: bool


def running_revision(settings: Settings | None = None) -> str:
    return (settings or get_settings()).flighter_revision


async def published_revision(*, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """The commit the published `:latest` was built from, as the registry has it.

    Three requests: a pull token, the manifest `:latest` points at, and the config blob
    that carries the labels. Anonymous throughout - the package is public, and a token
    endpoint that asks for nothing still has to be visited, because the registry refuses
    a manifest to a request with no bearer at all.
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS), transport=transport
    ) as http:
        granted = await http.get(
            f"{REGISTRY}/token", params={"service": "ghcr.io", "scope": f"repository:{IMAGE}:pull"}
        )
        granted.raise_for_status()
        auth = {"Authorization": f"Bearer {granted.json()['token']}"}

        manifest = await _manifest(http, TAG, auth)
        if manifest.headers.get("content-type", "").split(";")[0] in INDEX_TYPES:
            # An index names one manifest per platform. They are the same build, so the
            # labels on any of them are the labels on all of them.
            listed = manifest.json().get("manifests") or []
            if not listed:
                raise LookupError("the registry listed no manifests for that tag")
            manifest = await _manifest(http, listed[0]["digest"], auth)

        digest = manifest.json().get("config", {}).get("digest")
        if not digest:
            raise LookupError("that manifest names no config")
        blob = await _blob(http, digest, auth)
        return _revision(blob.json())


async def _manifest(
    http: httpx.AsyncClient, reference: str, auth: dict[str, str]
) -> httpx.Response:
    answer = await http.get(
        f"{REGISTRY}/v2/{IMAGE}/manifests/{reference}",
        headers={**auth, "Accept": ", ".join(MANIFEST_TYPES)},
    )
    answer.raise_for_status()
    return answer


async def _blob(http: httpx.AsyncClient, digest: str, auth: dict[str, str]) -> httpx.Response:
    """The image config, which the registry does not serve itself.

    It answers with a redirect to the storage the package really sits on, and that
    storage carries its own signature in the URL it hands out. The bearer is not carried
    over onto it: a signed URL needs no second authorization, and a store that reads one
    it did not issue can refuse the request for carrying it.
    """
    answer = await http.get(f"{REGISTRY}/v2/{IMAGE}/blobs/{digest}", headers=auth)
    if answer.is_redirect:
        answer = await http.get(answer.headers["location"])
    answer.raise_for_status()
    return answer


def _revision(config: dict[str, Any]) -> str:
    """The commit out of an image config, wherever that image put its labels."""
    for holder in (config.get("config") or {}, config.get("container_config") or {}):
        labels = holder.get("Labels") or {}
        if labels.get(REVISION_LABEL):
            return str(labels[REVISION_LABEL])
    raise LookupError("the published image carries no revision label")


async def check(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> Update:
    """What is running, what is published, and whether the two differ."""
    running = running_revision(settings)
    published = await published_revision(transport=transport)
    return Update(
        running=running, published=published, behind=bool(running) and running != published
    )


async def take_update(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> None:
    """Ask Watchtower to do now what it would have done on its next poll.

    It pulls, and if the digest moved it recreates every container it watches - this one
    among them, which is why nothing here waits for a body. A request that is answered
    and a request whose connection dies half way through both mean the same thing: the
    ask landed. Only a refusal to take it is a failure worth showing anybody.
    """
    settings = settings or get_settings()
    if not settings.watchtower_configured:
        raise RuntimeError("No Watchtower is configured for this deployment.")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS), transport=transport
    ) as http:
        try:
            answer = await http.post(
                f"{settings.watchtower_url.rstrip('/')}/v1/update",
                headers={"Authorization": f"Bearer {settings.watchtower_token}"},
            )
        except httpx.HTTPError:
            # The update it was asked for is what closed the connection.
            log.info("watchtower did not answer; it is updating this container")
            return
        if answer.status_code in (401, 403):
            raise RuntimeError("Watchtower refused the token.")
        answer.raise_for_status()
