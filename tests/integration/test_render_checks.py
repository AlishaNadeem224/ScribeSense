"""M3.4 acceptance — "patched vs unpatched font distinguished per engine". Runs only in the bubblewrap runner.

A private fontconfig file (the sandbox's FONTCONFIG_FILE) includes the system config plus a directory with a
test font. Without a rule the generic resolves elsewhere; with the rule the engine DRAWS the test family.
Engines that cannot run here skip with a reason (never pass).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness import render
from harness.testfonts import make_test_font

pytestmark = pytest.mark.integration

FAMILY = "AccessSans-rendertest"
SAMPLE = "The quick brown fox 0123456789"


def _conf(sandbox, *, patched: bool) -> None:  # type: ignore[no-untyped-def]
    fonts = sandbox.fonts_dir
    make_test_font(fonts / f"{FAMILY}-Regular.ttf", FAMILY)
    rule = (f'<alias binding="same"><family>sans-serif</family><prefer><family>{FAMILY}</family></prefer></alias>'
            if patched else "")
    Path(os.environ["FONTCONFIG_FILE"]).write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">\n'
        f"<fontconfig><include>/etc/fonts/fonts.conf</include><dir>{fonts}</dir>{rule}</fontconfig>\n")


@pytest.mark.parametrize("engine", ["pango", "qt", "firefox", "chromium"])
def test_patched_and_unpatched_are_distinguished(engine: str, sandbox) -> None:  # type: ignore[no-untyped-def]
    render.require_engine(engine)  # skip with a reason when the engine cannot run here
    _conf(sandbox, patched=False)
    assert FAMILY not in render.drawn_family(engine, SAMPLE, "sans-serif")
    _conf(sandbox, patched=True)
    assert render.drawn_family(engine, SAMPLE, "sans-serif") == FAMILY  # fresh process sees the new rule
    _conf(sandbox, patched=False)
    assert FAMILY not in render.drawn_family(engine, SAMPLE, "sans-serif")


def test_private_config_never_reaches_the_host(sandbox) -> None:  # type: ignore[no-untyped-def]
    _conf(sandbox, patched=True)
    assert Path(os.environ["FONTCONFIG_FILE"]).is_relative_to(sandbox.root)
    assert not Path("/home").exists()
