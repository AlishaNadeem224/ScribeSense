"""M3.8 — uninstall against the REAL font cache (M1.7) in the bubblewrap runner: only `<fonts_dir>/<key>/` for a
generated content key is ever removed; a user's own fonts, sibling keys and everything outside are untouched."""

from __future__ import annotations

from pathlib import Path

import pytest

from scribesense import uninstall as un
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.contracts.cli import ExitCode
from scribesense.contracts.config import Configuration
from scribesense.contracts.paths import fonts_dir
from scribesense.safety.testing import RecoverySystem, fake_fontset

pytestmark = pytest.mark.integration

A = Configuration("Noto Sans", 0.12, 1.5, 1.5, 1.2, 1.0)
KEEP, GONE = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


def make_key_dir(key: str) -> Path:
    d = fonts_dir() / key
    d.mkdir(parents=True)
    (d / "AccessSans-Regular.ttf").write_bytes(b"font bytes")
    return d


def test_real_removal_deletes_only_the_named_generated_directory(sandbox) -> None:  # type: ignore[no-untyped-def]
    keep, gone = make_key_dir(KEEP), make_key_dir(GONE)
    own = sandbox.home / ".local" / "share" / "fonts" / "MyOwnFont.ttf"  # a font the user installed themselves
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_bytes(b"theirs")
    (fonts_dir() / "not-a-key").mkdir()  # not a content key: never listed, never touched

    ops = un.CacheFontOps()
    assert ops.installed_keys() == [KEEP, GONE]
    assert ops.remove(GONE) is True
    assert not gone.exists() and keep.is_dir() and own.read_bytes() == b"theirs" and (fonts_dir() / "not-a-key").is_dir()
    assert ops.remove(GONE) is False  # already absent: success that removed nothing
    with pytest.raises(Exception):
        ops.remove("../..")  # a path is not a key: refused before it reaches the filesystem
    assert fonts_dir().is_dir() and keep.is_dir()


def test_the_whole_uninstall_with_real_font_directories(sandbox) -> None:  # type: ignore[no-untyped-def]
    world = FakeWorld()
    world.set_file("/fake/conf.d/99.conf", None)
    system = RecoverySystem(world, [FakeAdapter(world, {"t": ["/fake/conf.d/99.conf"]})])
    system.apply(A, keep=True)
    used = fake_fontset(A).key
    make_key_dir(used)
    stray = make_key_dir(GONE)
    calls: list[int] = []
    report = un.uninstall(system.journal, system.adapters, keybind_remover=lambda: calls.append(1) or True)
    assert un.exit_code(report) is ExitCode.OK and calls == [1]
    assert sorted(report.fonts_removed) == sorted([used, GONE])
    assert not (fonts_dir() / used).exists() and not stray.exists()
    assert (fonts_dir()).is_dir()  # the fonts directory itself stays
