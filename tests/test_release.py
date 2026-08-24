"""Which build is running, what is published, and who is asked to close the gap."""

from __future__ import annotations

import json

import httpx
import pytest

from flighter import release
from flighter.config import Settings
from tests.conftest import BLANK

REVISION = "0f1e2d3c4b5a69788796a5b4c3d2e1f001122334"
CONFIG_DIGEST = "sha256:" + "ab" * 32
MANIFEST_DIGEST = "sha256:" + "cd" * 32

INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


def settings(**fields: str) -> Settings:
    return Settings(**BLANK | fields)


def registry(*, revision: str = REVISION, indexed: bool = True) -> httpx.MockTransport:
    """A registry that answers the three requests the check makes, and nothing else."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            assert request.url.params["scope"] == f"repository:{release.IMAGE}:pull"
            return httpx.Response(200, json={"token": "anonymous"})
        # Every request past the token carries it.
        assert request.headers["Authorization"] == "Bearer anonymous"
        if path.endswith("/manifests/latest"):
            if not indexed:
                return _manifest()
            return httpx.Response(
                200,
                headers={"Content-Type": INDEX_TYPE},
                content=json.dumps(
                    {"manifests": [{"digest": MANIFEST_DIGEST, "platform": {"os": "linux"}}]}
                ),
            )
        if path.endswith(f"/manifests/{MANIFEST_DIGEST}"):
            return _manifest()
        if path.endswith(f"/blobs/{CONFIG_DIGEST}"):
            return httpx.Response(
                200, json={"config": {"Labels": {release.REVISION_LABEL: revision}}}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    def _manifest() -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": MANIFEST_TYPE},
            content=json.dumps({"config": {"digest": CONFIG_DIGEST}}),
        )

    return httpx.MockTransport(handle)


async def test_the_published_revision_is_read_off_the_image_the_tag_points_at() -> None:
    """An index names one manifest per platform and they are the same build, so the
    labels on any of them are the labels on all of them."""
    assert await release.published_revision(transport=registry()) == REVISION


async def test_a_tag_that_is_one_manifest_rather_than_an_index_is_read_the_same_way() -> None:
    assert await release.published_revision(transport=registry(indexed=False)) == REVISION


async def test_the_config_is_followed_to_the_storage_it_really_sits_on() -> None:
    """A registry answers a blob with a redirect to signed storage. The bearer does not
    go with it: the signature is the authorization there, and a store reading a token it
    did not issue can refuse the request for carrying it."""
    carried: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        if path.endswith("/manifests/latest"):
            return httpx.Response(
                200,
                headers={"Content-Type": MANIFEST_TYPE},
                content=json.dumps({"config": {"digest": CONFIG_DIGEST}}),
            )
        if path.endswith(f"/blobs/{CONFIG_DIGEST}"):
            return httpx.Response(
                307, headers={"Location": "https://pkg-containers.example.com/blob?sig=abc"}
            )
        carried.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"config": {"Labels": {release.REVISION_LABEL: REVISION}}})

    found = await release.published_revision(transport=httpx.MockTransport(handle))

    assert found == REVISION
    assert carried == [None]


async def test_an_image_with_no_revision_on_it_is_an_answer_rather_than_a_wrong_one() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anonymous"})
        if request.url.path.endswith("/manifests/latest"):
            return httpx.Response(
                200,
                headers={"Content-Type": MANIFEST_TYPE},
                content=json.dumps({"config": {"digest": CONFIG_DIGEST}}),
            )
        return httpx.Response(200, json={"config": {"Labels": {}}})

    with pytest.raises(LookupError):
        await release.published_revision(transport=httpx.MockTransport(handle))


async def test_a_deployment_is_behind_when_the_two_commits_differ() -> None:
    behind = await release.check(settings(flighter_revision="9" * 40), transport=registry())
    assert behind == release.Update(running="9" * 40, published=REVISION, behind=True)

    current = await release.check(settings(flighter_revision=REVISION), transport=registry())
    assert current.behind is False


async def test_a_build_that_was_not_published_has_nothing_to_be_behind() -> None:
    """A checkout, or a local `docker compose build`. The page says so rather than
    calling every such build out of date."""
    update = await release.check(settings(), transport=registry())
    assert update.running == ""
    assert update.published == REVISION
    assert update.behind is False


def wired() -> Settings:
    return settings(watchtower_url="http://watchtower:8080/", watchtower_token="shared-secret")


async def test_the_update_is_asked_of_watchtower_with_the_token_it_shares() -> None:
    asked: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(200, text="")

    await release.take_update(wired(), transport=httpx.MockTransport(handle))

    (request,) = asked
    assert str(request.url) == "http://watchtower:8080/v1/update"
    assert request.headers["Authorization"] == "Bearer shared-secret"


async def test_an_answer_that_never_arrives_is_the_update_arriving() -> None:
    """What was asked for is this container being replaced, so the connection dying part
    way through is the ask having landed rather than having failed."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection closed")

    await release.take_update(wired(), transport=httpx.MockTransport(handle))


async def test_a_token_watchtower_does_not_accept_is_worth_saying() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(RuntimeError, match="refused the token"):
        await release.take_update(wired(), transport=httpx.MockTransport(handle))


async def test_a_deployment_with_no_watchtower_is_not_asked_to_guess_where_one_is() -> None:
    with pytest.raises(RuntimeError, match="No Watchtower"):
        await release.take_update(settings(), transport=httpx.MockTransport(lambda _: None))
    assert settings(watchtower_url="http://watchtower:8080").watchtower_configured is False
    assert wired().watchtower_configured is True
