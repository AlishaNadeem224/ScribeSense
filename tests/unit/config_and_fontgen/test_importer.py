"""W1 user font import (fontgen/importer.py; contract amendment 2026-11-24: K1 rule 6, K2 rule 2, K8, K9).

The eligibility check runs in a real child process (`rules.is_eligible(isolate=True)`), hence `real_subprocess`:
only `sys.executable` is started. Source fonts are copies of the bundled Noto Sans, renamed where a non-bundled
family name is needed.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.config import bundled, rules, settings
from scribesense.contracts.config import Configuration
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.paths import user_fonts_dir
from scribesense.fontgen import importer
from scribesense.store import Store, default_path
from scribesense.store import serialize as ser

pytestmark = pytest.mark.real_subprocess


@pytest.fixture
def store(sandbox) -> Iterator[Store]:  # type: ignore[no-untyped-def]
    s = Store(default_path())
    settings.set_imported_families(s.user_fonts.families)
    bundled.set_imported_files(lambda family: importer.stored_files(family, repo=s.user_fonts))
    yield s
    settings.set_imported_families(None)  # module state: never leak into other tests
    bundled.set_imported_files(None)
    s.close()


def font(tmp_path: Path, style: str = "Regular", *, family: str | None = "Test Sans", name: str | None = None,
         weight: int | None = None, selection: int | None = None, revision: float | None = None) -> Path:
    """A copy of bundled Noto Sans <style>, optionally renamed / re-weighted."""
    source = bundled.base_font_files("Noto Sans")[style]
    out = tmp_path / (name or f"{family or 'Noto'}-{style}.ttf")
    out.parent.mkdir(parents=True, exist_ok=True)
    f = TTFont(source)
    if family is not None:
        for record in list(f["name"].names):
            if record.nameID in (1, 16, 21):
                f["name"].setName(family, record.nameID, record.platformID, record.platEncID, record.langID)
        f["name"].removeNames(nameID=21)
    if weight is not None:
        f["OS/2"].usWeightClass = weight
    if selection is not None:
        f["OS/2"].fsSelection = selection
    if revision is not None:
        f["head"].fontRevision = revision  # different bytes, same family and style
    f.save(out)
    return out


def test_a_family_is_copied_checked_and_usable_by_name(store: Store, tmp_path: Path) -> None:
    report = importer.import_files([font(tmp_path), font(tmp_path, "Bold")], repo=store.user_fonts)
    assert report.refused == [] and report.imported == {"Test Sans": ["Regular", "Bold"]}
    files = bundled.base_font_files("Test Sans")  # K2: the stored copies, never the source
    assert set(files) == {"Regular", "Bold"}
    for path in files.values():
        assert path.is_relative_to(user_fonts_dir().resolve()) and path.name in ("Regular.ttf", "Bold.ttf")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert settings.validate(Configuration("Test Sans", 0.12, 1.5, 1.5, 1.2, 1.0)) == []
    assert list((user_fonts_dir() / importer.STAGING).iterdir()) == []


def test_a_bundled_name_gets_the_imported_suffix(store: Store, tmp_path: Path) -> None:
    report = importer.import_files([font(tmp_path, family=None)], repo=store.user_fonts)
    assert report.imported == {"Noto Sans (imported)": ["Regular"]}
    assert bundled.base_font_files("Noto Sans")["Regular"].is_relative_to(bundled.fonts_root())  # still bundled


def test_the_users_file_name_never_becomes_a_path(store: Store, tmp_path: Path) -> None:
    odd = font(tmp_path, name="..\\x ; rm -rf ~.ttf")
    importer.import_files([odd], repo=store.user_fonts)
    [entry] = store.user_fonts.by_family("Test Sans")
    assert entry.file_name == "Regular.ttf"


def test_a_family_needs_a_regular(store: Store, tmp_path: Path) -> None:
    report = importer.import_files([font(tmp_path, "Bold")], repo=store.user_fonts)
    assert report.imported == {} and [r.detail for r in report.refused] == [importer.NO_REGULAR]
    assert store.user_fonts.families() == [] and not any(user_fonts_dir().glob("*/*.ttf"))
    importer.import_files([font(tmp_path)], repo=store.user_fonts)
    assert importer.import_files([font(tmp_path, "Bold")], repo=store.user_fonts).imported == {"Test Sans": ["Bold"]}


def test_a_broken_file_is_refused_and_nothing_is_kept(store: Store, tmp_path: Path) -> None:
    bad = tmp_path / "broken.ttf"
    bad.write_bytes(b"\x00\x01\x00\x00" + b"junk" * 100)
    report = importer.import_files([bad], repo=store.user_fonts)
    assert [(r.code, r.detail) for r in report.refused] == [(ErrorCode.IMPORT_NOT_ELIGIBLE, rules.FONT_UNREADABLE)]
    assert store.user_fonts.families() == [] and list((user_fonts_dir() / importer.STAGING).iterdir()) == []


def test_an_oversized_file_is_refused_before_any_parse(store: Store, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "MAX_FONT_BYTES", 1000)
    report = importer.import_files([font(tmp_path)], repo=store.user_fonts)
    assert [r.detail for r in report.refused] == [rules.FONT_TOO_LARGE]


def test_a_style_outside_the_four_is_refused_not_promoted(store: Store, tmp_path: Path) -> None:
    light = font(tmp_path, name="Light.ttf", weight=300, selection=0)
    report = importer.import_files([font(tmp_path), light], repo=store.user_fonts)
    assert report.imported == {"Test Sans": ["Regular"]}
    assert [r.detail for r in report.refused] == [importer.STYLE_UNSUPPORTED]


def test_identical_bytes_twice_is_one_copy(store: Store, tmp_path: Path) -> None:
    path = font(tmp_path)
    importer.import_files([path], repo=store.user_fonts)
    importer.import_files([path], repo=store.user_fonts)
    assert len(list(user_fonts_dir().glob("*/Regular.ttf"))) == 1


def test_replacing_a_style_in_use_is_refused_and_keeps_the_old_copy(store: Store, tmp_path: Path) -> None:
    importer.import_files([font(tmp_path)], repo=store.user_fonts)
    old = bundled.base_font_files("Test Sans")["Regular"]
    store.execute("UPDATE active_config SET config = ?, font_key = 'k'",
                  (ser.config_to_json(Configuration("Test Sans", 0.12, 1.5, 1.5, 1.2, 1.0)),))
    changed = font(tmp_path / "v2", revision=9.5)  # same family + style, different bytes
    report = importer.import_files([changed], repo=store.user_fonts)
    assert [r.code for r in report.refused] == [ErrorCode.IMPORTED_FONT_IN_USE]
    assert bundled.base_font_files("Test Sans")["Regular"] == old and old.exists()
    assert len(list(user_fonts_dir().glob("*/Regular.ttf"))) == 1  # the refused copy was removed


def test_replacing_a_style_not_in_use_removes_the_old_copy(store: Store, tmp_path: Path) -> None:
    importer.import_files([font(tmp_path)], repo=store.user_fonts)
    old = bundled.base_font_files("Test Sans")["Regular"]
    importer.import_files([font(tmp_path / "v2", revision=9.5)], repo=store.user_fonts)
    assert not old.exists() and len(list(user_fonts_dir().glob("*/Regular.ttf"))) == 1


def test_delete_family_removes_the_copies_but_not_while_in_use(store: Store, tmp_path: Path) -> None:
    importer.import_files([font(tmp_path), font(tmp_path, "Bold")], repo=store.user_fonts)
    store.execute("UPDATE active_config SET config = ?, font_key = 'k'",
                  (ser.config_to_json(Configuration("Test Sans", 0.12, 1.5, 1.5, 1.2, 1.0)),))
    with pytest.raises(ScribeSenseError) as exc:
        importer.delete_family("Test Sans", repo=store.user_fonts)
    assert exc.value.code is ErrorCode.IMPORTED_FONT_IN_USE
    store.execute("UPDATE active_config SET config = NULL, font_key = NULL")
    assert importer.delete_family("Test Sans", repo=store.user_fonts) is True
    assert not any(user_fonts_dir().glob("*/*.ttf"))
    with pytest.raises(ScribeSenseError):  # unknown again: neither bundled nor imported
        bundled.base_font_files("Test Sans")


def test_a_stored_copy_that_vanished_is_font_unreadable(store: Store, tmp_path: Path) -> None:
    importer.import_files([font(tmp_path)], repo=store.user_fonts)
    bundled.base_font_files("Test Sans")["Regular"].unlink()
    with pytest.raises(ScribeSenseError) as exc:
        bundled.base_font_files("Test Sans")
    assert exc.value.code is ErrorCode.FONT_UNREADABLE


def test_a_folder_import_takes_only_its_own_font_files(store: Store, tmp_path: Path) -> None:
    folder = tmp_path / "family"
    font(folder)
    font(folder, "Bold")
    font(folder / "sub", "Italic")  # sub-folders are not read
    (folder / "notes.txt").write_text("x")
    os.symlink(font(tmp_path / "elsewhere", "BoldItalic"), folder / "link.ttf")  # symlinks are not followed
    report = importer.import_folder(folder, repo=store.user_fonts)
    assert report.imported == {"Test Sans": ["Regular", "Bold"]} and report.refused == []


def test_installed_fonts_are_copied_not_used_in_place(store: Store, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    system = font(tmp_path / "system")
    monkeypatch.setattr(importer, "_fc_list", lambda: [("Test Sans", system), ("AccessSans-0123456789ab", system)])
    assert importer.installed_families() == ["Test Sans"]
    report = importer.import_installed("Test Sans", repo=store.user_fonts)
    assert report.imported == {"Test Sans": ["Regular"]}
    assert bundled.base_font_files("Test Sans")["Regular"] != system
    assert store.user_fonts.by_family("Test Sans")[0].source_kind == "installed"
    assert importer.import_installed("Missing", repo=store.user_fonts).refused[0].detail == "NOT_INSTALLED"


def test_too_many_files_in_one_import_is_refused(store: Store, tmp_path: Path) -> None:
    report = importer.import_files([tmp_path / f"{i}.ttf" for i in range(importer.MAX_FILES + 1)],
                                   repo=store.user_fonts)
    assert [r.detail for r in report.refused] == [importer.TOO_MANY_FILES]


def test_nothing_is_ever_read_outside_the_user_fonts_folder(store: Store, tmp_path: Path) -> None:
    """K2: a provider pointing outside user_fonts_dir() is refused (defence against a bad store row)."""
    outside = font(tmp_path / "outside")
    bundled.set_imported_files(lambda family: {"Regular": outside} if family == "Test Sans" else None)
    with pytest.raises(ScribeSenseError) as exc:
        bundled.base_font_files("Test Sans")
    assert exc.value.code is ErrorCode.FONT_UNREADABLE
    shutil.rmtree(tmp_path / "outside")
