"""The site (docs/index.html) shows the release version by hand; keep it in sync."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "index.html"


@pytest.mark.skipif(not SITE.exists(), reason="docs/ is not shipped in the sdist")
def test_site_shows_current_version():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    shown = set(re.findall(r"v(\d+\.\d+\.\d+)", SITE.read_text(encoding="utf-8")))

    assert shown, "docs/index.html shows no vX.Y.Z version"
    assert shown == {version}, (
        f"docs/index.html shows {sorted(shown)} but pyproject.toml is {version}; "
        "update the hero label and footer stamp"
    )
