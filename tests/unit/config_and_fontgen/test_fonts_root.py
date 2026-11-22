"""M1.2 -- where the bundled font tree is found (PLAN_v2 Phase III, W2.1).

The bug these tests exist for: `config/bundled.py` located the font tree as
``Path(__file__).parents[3] / "fonts"``. In a checkout that is the repository root. In an
installed package the ``src/`` layer is gone, so the same three parents land on the Python
library directory, no font tree is found, and **every** build is refused with
``BUNDLED_FONT_MISSING`` -- silently, because a missing directory is a legitimate outcome
of that function.

`test_an_installed_layout_resolves_the_real_families` is the regression test: it builds a
realistic installed layout out of the repository's own font files and proves the pipeline
finds them when they are *not* at ``parents[3]``. It would have failed before the change.

These tests never rely on the machine's real environment: every one of them pins
`BUNDLED_FONTS_ENV` and ``XDG_DATA_DIRS`` explicitly. That matters here more than usual,
because this package sits outside `tests/` and therefore does **not** get the sandboxing
`tests/conftest.py` applies (see this package's `__init__`).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scribesense.config import bundled
from scribesense.contracts.config import SHORTLIST
from scribesense.contracts.errors import ErrorCode

REPO_FONTS = Path(__file__).resolve().parents[3] / "fonts"
"""The repository's own tree. Same arithmetic as `bundled._repo_fonts_root`, on purpose:
these tests sit at the same depth, so if one moves and the other does not, they disagree
and the suite says so."""


@pytest.fixture(autouse=True)
def _no_ambient_font_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralise the host environment, so no test here depends on the dev machine.

    Without this, a Fedora machine with ScribeSense installed would have a real
    ``/usr/share/scribesense/fonts`` on ``XDG_DATA_DIRS`` and these tests would quietly
    measure the installed copy instead of the fixture. Tests that want a data directory
    set ``XDG_DATA_DIRS`` themselves.
    """
    monkeypatch.delenv(bundled.BUNDLED_FONTS_ENV, raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "no-such-data-dir"))


def _make_tree(root: Path, families: tuple[str, ...] = ()) -> Path:
    """A minimal but well-formed font tree: the manifest plus empty family directories."""
    root.mkdir(parents=True, exist_ok=True)
    (root / bundled.MANIFEST_NAME).write_text("# fixture\n", encoding="utf-8")
    for family in families:
        (root / family).mkdir(parents=True, exist_ok=True)
    return root


def _installed_layout(share: Path) -> Path:
    """``<share>/scribesense/fonts`` -- the path an RPM would install to under /usr/share."""
    return share / "scribesense" / "fonts"


# ---- the override ---------------------------------------------------------------------


def test_the_override_is_the_only_candidate_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An override must not fall through, even to a tree that really exists.

    This is what lets a test prove a refusal: point at an empty directory and get
    ``BUNDLED_FONT_MISSING``, not the repository's real fonts.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv(bundled.BUNDLED_FONTS_ENV, str(empty))

    assert bundled.candidate_roots() == (empty,)
    assert bundled.fonts_root() == empty
    assert REPO_FONTS.is_dir(), "the repository tree exists, so this proves no fall-through"


def test_an_empty_override_tree_refuses_with_the_contract_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """K2's error contract is unchanged by the new resolution."""
    monkeypatch.setenv(bundled.BUNDLED_FONTS_ENV, str(tmp_path / "nothing-here"))

    with pytest.raises(bundled.BundledFontMissing) as exc:
        bundled.base_font_files("Noto Sans")
    assert exc.value.code is ErrorCode.BUNDLED_FONT_MISSING
    # Errors contract: the message is the bare code, never a path.
    assert str(exc.value) == "BUNDLED_FONT_MISSING"


def test_the_failure_detail_names_where_it_looked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A packaging failure and an unbundled family need opposite fixes, so say which."""
    missing = tmp_path / "nothing-here"
    monkeypatch.setenv(bundled.BUNDLED_FONTS_ENV, str(missing))

    with pytest.raises(bundled.BundledFontMissing) as exc:
        bundled.base_font_files("Noto Sans")
    assert str(missing) in exc.value.detail and "searched" in exc.value.detail


# ---- installed vs development ----------------------------------------------------------


def test_an_installed_data_directory_is_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case the old constant could not express at all."""
    installed = _make_tree(_installed_layout(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))

    assert bundled.fonts_root() == installed


def test_an_installed_tree_wins_over_the_repository_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pins PLAN_v2 W2.1's stated order: installed first, "the repo path as the
    development fallback".

    The order only matters when both exist, which is a developer with the package also
    installed. If the team would rather the checkout win in that case, this is the test
    to flip -- it is here so the decision is visible rather than implicit.
    """
    installed = _make_tree(_installed_layout(tmp_path / "share"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))

    assert bundled.fonts_root() == installed != REPO_FONTS


def test_the_repository_tree_is_used_in_a_checkout() -> None:
    """With nothing installed, development is unaffected by the change (the autouse
    fixture has already pointed XDG_DATA_DIRS at nothing)."""
    assert bundled.fonts_root() == REPO_FONTS
    assert (REPO_FONTS / bundled.MANIFEST_NAME).is_file()


def test_every_data_dir_on_the_path_is_searched_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _make_tree(_installed_layout(second))
    monkeypatch.setenv("XDG_DATA_DIRS", os.pathsep.join((str(first), str(second))))

    roots = bundled.candidate_roots()
    assert roots[:2] == (_installed_layout(first), _installed_layout(second))
    assert roots[-1] == REPO_FONTS, "the development tree is always the last resort"
    assert bundled.fonts_root() == _installed_layout(second)


def test_xdg_defaults_are_used_when_the_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """XDG's own default, so a distribution package is found without the variable set."""
    monkeypatch.delenv("XDG_DATA_DIRS", raising=False)

    roots = bundled.candidate_roots()
    assert Path("/usr/local/share/scribesense/fonts") in roots
    assert Path("/usr/share/scribesense/fonts") in roots


# ---- the manifest marker ---------------------------------------------------------------


def test_a_directory_without_the_manifest_is_not_our_font_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/usr/share/fonts` exists on every Linux box; a bare `is_dir()` would match junk."""
    decoy = _installed_layout(tmp_path / "share")
    decoy.mkdir(parents=True)
    (decoy / "Noto Sans").mkdir()  # looks right, has no manifest
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "share"))

    assert bundled.fonts_root() == REPO_FONTS


# ---- the property the module constant could not have ------------------------------------


def test_the_root_is_re_read_on_every_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole reason this is a function and not a constant.

    A constant is evaluated once at import, so the sandbox (M3.3) and any test that sets
    the variable afterwards could not change it -- which is why the old code had to be
    monkeypatched attribute-by-attribute instead.
    """
    one, two = _make_tree(tmp_path / "one"), _make_tree(tmp_path / "two")

    monkeypatch.setenv(bundled.BUNDLED_FONTS_ENV, str(one))
    assert bundled.fonts_root() == one
    monkeypatch.setenv(bundled.BUNDLED_FONTS_ENV, str(two))
    assert bundled.fonts_root() == two


# ---- the regression test -----------------------------------------------------------------


def test_an_installed_layout_resolves_the_real_families(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end proof, with the repository's real fonts at an installed path.

    Copies the actual bundled tree to ``<share>/scribesense/fonts`` -- nowhere near
    ``parents[3]`` -- and asserts the pipeline still finds every shortlist family with a
    usable Regular. Before the change this raised ``BUNDLED_FONT_MISSING`` for all four,
    which is precisely what an installed RPM would have done to a user on first Apply.
    """
    share = tmp_path / "share"
    installed = _installed_layout(share)
    installed.parent.mkdir(parents=True)
    shutil.copytree(REPO_FONTS, installed, ignore=shutil.ignore_patterns("tools"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(share))

    assert bundled.fonts_root() == installed
    for family in SHORTLIST:
        files = bundled.base_font_files(family)
        assert bundled.REQUIRED_STYLE in files, family
        assert files[bundled.REQUIRED_STYLE].is_file()
        # Resolved from the installed copy, not from the repository.
        assert installed in files[bundled.REQUIRED_STYLE].parents


def test_space_width_is_measured_from_the_installed_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """K2 `resolve()` measures the Regular face, so it must read the installed files too.

    Noto Sans' space is 260/1000 em (recorded in ARCHITECTURE.md M1.2); getting that from
    an installed layout proves the measurement is not quietly falling back to
    `presets.REFERENCE_SPACE_WIDTH_EM`.
    """
    share = tmp_path / "share"
    installed = _installed_layout(share)
    installed.parent.mkdir(parents=True)
    shutil.copytree(REPO_FONTS, installed, ignore=shutil.ignore_patterns("tools"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(share))

    assert bundled.space_width_em("Noto Sans") == pytest.approx(0.26)
