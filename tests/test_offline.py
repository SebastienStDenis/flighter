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
