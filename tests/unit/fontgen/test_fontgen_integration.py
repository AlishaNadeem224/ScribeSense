"""M1.6–M1.8 with the real pango-view, fc-cache and fc-match — bubblewrap runner only (M3.3).

The unit tests fake these tools; this is the check that the fakes and the tools agree, and the
card acceptance "font visible to fc-match in the sandbox right after install".
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scribesense.contracts import fonts as k7
from scribesense.contracts import paths
from scribesense.contracts.config import Configuration
from scribesense.fontgen import generator, naming

pytestmark = pytest.mark.integration

CFG = Configuration("Noto Sans", 0.12, 1.5, 1.8, 1.2, 1.0)


@pytest.fixture
def fontconfig(tmp_path: Path) -> None:
    missing = [t for t in ("pango-view", "fc-match", "fc-cache") if not shutil.which(t)]
    if missing:
        pytest.skip(f"CHECK_UNAVAILABLE: {', '.join(missing)} not installed")
    # The sandbox's FONTCONFIG_FILE is private and written by the test (M3.3): only our font dir.
    Path(os.environ["FONTCONFIG_FILE"]).write_text(
        f"<?xml version='1.0'?><fontconfig><dir>{paths.fonts_dir()}</dir>"
        f"<cachedir>{tmp_path / 'fc-cache'}</cachedir></fontconfig>\n")


def test_real_build_installs_and_fc_match_resolves_both_families(fontconfig, family_files) -> None:  # type: ignore[no-untyped-def]
    result = generator.build(CFG, files=family_files)  # real PangoRenderer + Fontconfig
    assert result.status == "built", (result.reason_code, result.detail, result.warnings)
    assert isinstance(result.fonts, k7.FontSet)
    for family in (result.fonts.reading_family, result.fonts.ui_family):
        out = paths.run_cmd(["fc-match", "-f", "%{family}\n%{file}", naming.fc_pattern(family)], timeout=10)
        name, file = out.stdout.decode().splitlines()
        assert name == family and Path(file).parent == paths.fonts_dir() / result.fonts.key


def test_real_preview_renders_without_installing(fontconfig, family_files) -> None:  # type: ignore[no-untyped-def]
    with generator.build_preview(CFG, files=family_files) as preview:
        assert all(Path(f).is_file() for f in preview.files)
    assert not os.path.exists(preview.temp_dir) and not paths.fonts_dir().exists()
