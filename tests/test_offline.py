"""The service worker keeps the last copy of each page for when the server cannot be
reached, and only the last copy: the server is asked first, and a copy a day old is
dropped rather than shown."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER = Path(__file__).parents[1] / "src" / "flighter" / "static" / "sw.js"
HARNESS = Path(__file__).parent / "fixtures" / "sw_pages.js"
LOGO_HARNESS = Path(__file__).parent / "fixtures" / "sw_logos.js"
UPGRADE_HARNESS = Path(__file__).parent / "fixtures" / "sw_upgrade.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the worker")
def test_the_worker_serves_the_server_first_and_the_last_copy_when_it_cannot() -> None:
    rendered = subprocess.run(
        ["node", str(HARNESS), str(WORKER)], capture_output=True, text=True, check=True
    ).stdout
    outcome = json.loads(rendered)
    fresh, copy, after_a_day = outcome["served"]
    assert "fresh" in fresh
    assert copy == fresh
    assert "No connection" in after_a_day
    assert not outcome["kept"]
    # Which tab the address names is not what makes it a different page.
    assert "fresh" in outcome["byTab"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the worker")
def test_the_page_the_worker_draws_keeps_the_bar_and_asks_again_for_the_same_page() -> None:
    """With no copy to serve, the notice is still the app: the sections are one tap
    away, Try again asks for the page a person is on, and a flight keeps its way back
    up to the board."""
    rendered = subprocess.run(
        ["node", str(HARNESS), str(WORKER)], capture_output=True, text=True, check=True
    ).stdout
    drawn = json.loads(rendered)["drawn"]

    for page in drawn.values():
        assert "No connection" in page
        assert 'aria-label="Sections"' in page
        assert 'href="/mail"' in page and 'href="/settings"' in page
        # The add box posts to a server that is not there.
        assert 'href="/f/new"' not in page

    assert 'href="/?tab=mine"' in drawn["board"]
    assert 'href="/f/12?from=friends"' in drawn["flight"]

    # One level down from the board, and no browser chrome to get back up with.
    chevron = 'd="m15 18-6-6 6-6"'
    assert chevron in drawn["flight"]
    assert chevron not in drawn["board"]

    # The section a person is standing in is the one lit up on the bar.
    assert 'data-variant="secondary" aria-current="page"' in drawn["settings"]
    assert 'data-variant="secondary"' not in drawn["board"]
    assert 'aria-current="page"' in drawn["board"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the worker")
def test_the_worker_keeps_loaded_airline_logos_for_offline_use() -> None:
    rendered = subprocess.run(
        ["node", str(LOGO_HARNESS), str(WORKER)], capture_output=True, text=True, check=True
    ).stdout
    outcome = json.loads(rendered)

    assert outcome["online"] == {"handled": True, "body": "logo"}
    assert outcome["offline"] == {"handled": True, "body": "logo"}
    assert outcome["fetches"] == 6
    assert outcome["cachedType"] == "opaque"
    assert not outcome["cachedFailure"]
    assert outcome["missingFailed"]
    assert outcome["storageFailures"] == ["storage fallback"] * 3
    assert outcome["ignored"] == [False, False, False]
    assert outcome["caches"] == ["airline-logos-v1"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the worker")
def test_a_new_release_starts_its_caches_over_and_never_trusts_the_browsers() -> None:
    """An app saved to a home screen keeps the old release's files exactly as long as
    anything lets it: the new worker's shell must be fetched past the browser's cache,
    and the old release's shell and pages must not survive its activation."""
    rendered = subprocess.run(
        ["node", str(UPGRADE_HARNESS), str(WORKER)], capture_output=True, text=True, check=True
    ).stdout
    outcome = json.loads(rendered)
    assert outcome["caches"] == ["airline-logos-v1", "shell-newbuild"]
    assert outcome["precached"] and set(outcome["precached"]) == {"reload"}
    assert outcome["refresh"]["cache"] == "no-cache"
