"""K11 amendment 2026-11-22 — web-font copies for the browser extension: adapters/webfont.py, the service's
`get_font` / `get_style` fields, and the native host's validation. Unit sandbox only; the "installed" font set is
a copy of the bundled Noto Sans inside the sandbox fonts dir.
"""

from __future__ import annotations

import base64
import io
import shutil
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from scribesense.adapters import webfont
from scribesense.adapters.fake import FakeAdapter, FakeWorld
from scribesense.config import bundled
from scribesense.config.presets import BUILTINS
from scribesense.contracts import paths
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontBuildResult, FontSet
from scribesense.contracts.messages import MAX_MESSAGE_BYTES, WEBFONT_STYLES
from scribesense.controller.service import Service
from scribesense.fontgen import generator
from scribesense.safety import native_host

KEY = "0123456789ab"
FAMILY = f"AccessSans-{KEY}"


@pytest.fixture(autouse=True)
def short_runtime_dir(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    runtime = sandbox.data_dir.parent / "run"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    webfont._cache.clear()


def installed_set(styles: tuple[str, ...] = ("Regular", "Bold")) -> FontSet:
    """A FontSet whose reading files really exist in fonts_dir() (copies of the bundled Noto Sans)."""
    folder = paths.fonts_dir() / KEY
    folder.mkdir(parents=True, exist_ok=True)
    source = bundled.base_font_files("Noto Sans")
    files = []
    for style in styles:
        target = folder / f"{FAMILY}-{style}.ttf"
        shutil.copyfile(source[style], target)
        files.append(str(target))
    return FontSet(KEY, FAMILY, f"AccessSansUI-{KEY}", tuple(files), styles)


# ---------------------------------------------------------------- adapters/webfont.py

def test_the_web_copy_is_latin_only_much_smaller_and_cached() -> None:
    fonts = installed_set()
    data = webfont.web_font(fonts, "Regular")
    original = Path(fonts.files[0]).stat().st_size
    assert len(data) < original / 3
    cmap = TTFont(io.BytesIO(data)).getBestCmap()
    assert ord("A") in cmap and ord("é") in cmap and ord("—") in cmap
    assert ord("Ж") not in cmap and ord("Ω") not in cmap  # Cyrillic / Greek fall back to the page's fonts
    assert webfont.web_font(fonts, "Regular") is data  # cached


def test_the_copy_keeps_the_source_name_records() -> None:
    """The page registers the copy under the CSS family via FontFace; the copy's own names are kept unchanged."""
    data = webfont.web_font(installed_set(), "Bold")
    names = {str(r) for r in TTFont(io.BytesIO(data))["name"].names}
    assert "Noto Sans" in names  # this test's "installed" file is a Noto Sans copy


@pytest.mark.parametrize("style", ["Italic", "Black", "../Regular"])
def test_styles_that_are_not_installed_or_not_known_are_unavailable(style: str) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        webfont.web_font(installed_set(("Regular",)), style)
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE


def test_a_file_outside_the_generated_fonts_dir_is_never_served(tmp_path: Path) -> None:
    outside = tmp_path / f"{FAMILY}-Regular.ttf"
    shutil.copyfile(bundled.base_font_files("Noto Sans")["Regular"], outside)
    fonts = FontSet(KEY, FAMILY, f"AccessSansUI-{KEY}", (str(outside),), ("Regular",))
    with pytest.raises(ScribeSenseError) as exc:
        webfont.web_font(fonts, "Regular")
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE


def test_a_symlink_out_of_the_fonts_dir_is_never_served(tmp_path: Path) -> None:
    fonts = installed_set(("Regular",))
    real = tmp_path / "elsewhere.ttf"
    shutil.copyfile(fonts.files[0], real)
    Path(fonts.files[0]).unlink()
    Path(fonts.files[0]).symlink_to(real)
    with pytest.raises(ScribeSenseError) as exc:
        webfont.web_font(fonts, "Regular")
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE


def test_a_copy_over_the_size_limit_or_unreadable_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    fonts = installed_set(("Regular",))
    limit = webfont.MAX_WEBFONT_BYTES
    monkeypatch.setattr(webfont, "MAX_WEBFONT_BYTES", 1000)
    with pytest.raises(ScribeSenseError) as exc:
        webfont.web_font(fonts, "Regular")
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE
    monkeypatch.setattr(webfont, "MAX_WEBFONT_BYTES", limit)  # never monkeypatch.undo(): it would undo the sandbox
    webfont._cache.clear()
    Path(fonts.files[0]).write_bytes(b"not a font")
    with pytest.raises(ScribeSenseError) as exc:
        webfont.web_font(fonts, "Regular")
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE


def test_served_styles_follow_the_contract_order() -> None:
    fonts = FontSet(KEY, FAMILY, "x", (), ("BoldItalic", "Regular", "Bold"))
    assert webfont.served_styles(fonts) == ["Regular", "Bold", "BoldItalic"]
    assert set(webfont.served_styles(fonts)) <= set(WEBFONT_STYLES)


# ---------------------------------------------------------------- the service (controller/service.py)

@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    world = FakeWorld()
    world.set_file("/fake/fc.conf", b"orig")
    svc = Service.open(adapters=[FakeAdapter(world, {"fc": ["/fake/fc.conf"]}, name="fontconfig")])
    fonts = installed_set()  # after open(): its start-up clean-up removes generated fonts nothing refers to
    svc.controller._generator = lambda _c: FontBuildResult("built", fonts, None)
    monkeypatch.setattr(generator, "installed_sets", lambda: [fonts])
    yield svc
    svc.close()


def test_get_style_names_the_font_key_and_styles_once_kept(service) -> None:  # type: ignore[no-untyped-def]
    assert service.handle({"type": "get_style"})["active"] is False
    service.keep(service.apply(BUILTINS[0]).tx)
    reply = service.handle({"type": "get_style"})
    assert reply["active"] is True and reply["font_key"] == KEY and reply["styles"] == ["Regular", "Bold"]


def test_get_font_serves_the_active_styles_web_copy_within_the_message_limit(service) -> None:  # type: ignore[no-untyped-def]
    service.keep(service.apply(BUILTINS[0]).tx)
    reply = service.handle({"type": "get_font", "key": KEY, "style": "Bold"})
    assert reply["ok"] is True and reply["key"] == KEY and reply["style"] == "Bold" and reply["family"] == FAMILY
    data = base64.b64decode(reply["data"], validate=True)
    assert data == webfont.web_font(generator.installed_sets()[0], "Bold")
    assert len(native_host.encode_frame({"v": 1, **reply})) < MAX_MESSAGE_BYTES


@pytest.mark.parametrize("request_", [
    {"type": "get_font", "key": "ffffffffffff", "style": "Regular"},  # not the active key
    {"type": "get_font", "key": KEY, "style": "Italic"},  # not installed for this key
])
def test_get_font_refuses_anything_but_the_active_set(service, request_: dict) -> None:  # type: ignore[no-untyped-def]
    service.keep(service.apply(BUILTINS[0]).tx)
    with pytest.raises(ScribeSenseError) as exc:
        service.handle(request_)
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE


def test_get_font_before_any_keep_is_unavailable_and_bad_fields_are_bad_message(service) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ScribeSenseError) as exc:
        service.handle({"type": "get_font", "key": KEY, "style": "Regular"})
    assert exc.value.code is ErrorCode.FONT_UNAVAILABLE  # nothing confirmed yet (only the confirmed style is served)
    with pytest.raises(ScribeSenseError) as exc:
        service.handle({"type": "get_font", "key": 5, "style": "Regular"})
    assert exc.value.code is ErrorCode.BAD_MESSAGE


# ---------------------------------------------------------------- the native host (safety/native_host.py)

def test_the_host_forwards_a_well_formed_get_font_and_drops_extra_fields() -> None:
    raw = b'{"v": 1, "type": "get_font", "key": "0123456789ab", "style": "BoldItalic", "path": "/etc/passwd"}'
    assert native_host.decode_request(raw) == {"type": "get_font", "key": KEY, "style": "BoldItalic"}


@pytest.mark.parametrize("raw", [
    b'{"v": 1, "type": "get_font", "key": "0123456789AB", "style": "Regular"}',  # uppercase hex
    b'{"v": 1, "type": "get_font", "key": "../../x", "style": "Regular"}',
    b'{"v": 1, "type": "get_font", "key": "0123456789ab", "style": "Black"}',
    b'{"v": 1, "type": "get_font", "key": "0123456789ab"}',
    b'{"v": 1, "type": "get_font", "style": "Regular"}',
])
def test_the_host_refuses_malformed_get_font(raw: bytes) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        native_host.decode_request(raw)
    assert exc.value.code is ErrorCode.BAD_MESSAGE
