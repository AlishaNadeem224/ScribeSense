"""M4.13 — Verify helpers (adapters/verify.py) and the Pango probe (adapters/pango_probe.py).

Unit tests use fake runners and a fake `gi` module; no real process, fontconfig or Pango is touched.
The one real-probe test is @pytest.mark.integration (bubblewrap runner only, skipped elsewhere).
"""

from __future__ import annotations

import ast
import errno
import json
import runpy
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scribesense.adapters import pango_probe as probe
from scribesense.adapters import verify as vf
from scribesense.contracts.adapter import DEFAULT_TIMEOUTS, Target
from scribesense.contracts.errors import ErrorCode, ScribeSenseError

HOST = Target("fontconfig", "fontconfig:user", "Font rules (fontconfig)", False)
GTK = Target("gtk", "gtk:default", "GTK", False)
READING = "AccessSans-abc123"
ORIGINAL = "DejaVu Sans"
ADAPTERS_DIR = Path(vf.__file__).resolve().parent


def flatpak(app_id: str) -> Target:
    return Target("flatpak", f"flatpak:{app_id}", "Some app", False)


@dataclass
class Proc:
    stdout: bytes
    stderr: bytes = b""
    returncode: int = 0


@dataclass
class Runner:
    """Returns `stdout` (or raises `error`) for every command; records argv and timeout."""

    stdout: bytes = b""
    error: BaseException | None = None
    calls: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(self, argv: list[str], timeout: float) -> Proc:
        self.calls.append((list(argv), timeout))
        if self.error is not None:
            raise self.error
        return Proc(self.stdout)


def probe_out(*families: str) -> bytes:
    return json.dumps({"v": 1, "families": list(families)}).encode()


# ---------------------------------------------------------------- acceptance: patched vs original

def test_chosen_family_distinguishes_patched_from_original() -> None:
    assert vf.chosen_family(HOST, "sans-serif", runner=Runner(ORIGINAL.encode())) == ORIGINAL
    assert vf.chosen_family(HOST, "sans-serif", runner=Runner(READING.encode())) == READING


# drawn_family_pango only PARSES the probe's report. The "every run is drawn in the reading family"
# decision (and so the drawn half of "distinguishes patched vs original") is FontconfigAdapter.verify,
# accepted in test_fontconfig_adapter.py (test_verify_drawn_mismatch_fails_even_when_fc_match_agrees and
# the integration test test_real_fc_match_resolves_to_the_generated_families_and_back).
@pytest.mark.parametrize("families", [
    (READING,),               # patched
    (ORIGINAL,),              # original
    (READING, ORIGINAL),      # a mix (fallback for some runs), order kept
    (ORIGINAL, READING),
])
def test_drawn_family_pango_returns_the_reported_families_in_order(families: tuple[str, ...]) -> None:
    assert vf.drawn_family_pango("sans-serif", runner=Runner(probe_out(*families))) == families


def test_drawn_families_are_distinct_in_first_use_order() -> None:
    drawn = vf.drawn_family_pango("sans-serif", runner=Runner(probe_out(READING, ORIGINAL, READING)))
    assert drawn == (READING, ORIGINAL)


# ---------------------------------------------------------------- command shapes

@pytest.mark.parametrize(("query", "pattern"), [
    ("sans-serif", "sans\\-serif"),
    ("Foo:Bar", "Foo\\:Bar"),
    ("A,B", "A\\,B"),
    ("back\\slash", "back\\\\slash"),
    ("Fira Sans", "Fira Sans"),
    ("-leading", "\\-leading"),  # never starts with '-': cannot be read as an option
])
def test_host_and_flatpak_argv_with_escaping(query: str, pattern: str) -> None:
    host = Runner(b"X")
    vf.chosen_family(HOST, query, runner=host)
    assert host.calls == [(["fc-match", "-f", "%{family[0]}", pattern], DEFAULT_TIMEOUTS["fc-match"])]

    fp = Runner(b"X")
    vf.chosen_family(flatpak("org.gnome.gedit"), query, runner=fp)
    assert fp.calls == [(["flatpak", "run", "--command=fc-match", "org.gnome.gedit", "-f", "%{family[0]}", pattern],
                         DEFAULT_TIMEOUTS["flatpak-fc-match"])]


def test_other_host_adapters_use_host_fc_match() -> None:
    r = Runner(b"X")
    vf.chosen_family(GTK, "serif", runner=r)
    assert r.calls[0][0][0] == "fc-match"


def test_timeout_override() -> None:
    r = Runner(b"X")
    vf.chosen_family(HOST, "serif", runner=r, timeout=3.0)
    vf.drawn_family_pango("serif", runner=Runner(probe_out("X"), calls=r.calls), timeout=4.0)
    assert [t for _, t in r.calls] == [3.0, 4.0]


def test_escape_round_trips_through_fontconfig_unescaping() -> None:
    import re

    for name in ("sans-serif", "Foo:Bar", "A,B", "back\\slash", "a-:,\\b"):
        assert re.sub(r"\\(.)", r"\1", vf.escape_fc_pattern(name)) == name


@pytest.mark.parametrize("app_id", ["org.gnome.gedit", "app.zen_browser.zen", "com.brave.Browser", "a.b",
                                    "org.example.My-App", "io._private.App"])
def test_valid_flatpak_app_ids(app_id: str) -> None:
    assert vf.flatpak_app_id(flatpak(app_id)) == app_id


@pytest.mark.parametrize("bad", ["--filesystem=host", "a", "../x", "a b", "", ".a.b", "a..b", "a.b.", "-a.b",
                                 "1a.b", "org.gnome/gedit", "org.gnome.gedit\n", "org.gnome.ged;it", "a." + "b" * 255])
def test_invalid_flatpak_app_ids_are_refused_and_nothing_runs(bad: str) -> None:
    r = Runner(b"X")
    with pytest.raises(ScribeSenseError) as exc:
        vf.chosen_family(flatpak(bad), "sans-serif", runner=r)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR
    assert r.calls == []


def test_flatpak_target_without_prefix_is_refused() -> None:
    r = Runner(b"X")
    with pytest.raises(ScribeSenseError) as exc:
        vf.chosen_family(Target("flatpak", "org.gnome.gedit", "x", False), "serif", runner=r)
    assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR and r.calls == []


def test_probe_command_is_a_fresh_python_with_the_fixed_sample() -> None:
    r = Runner(probe_out(READING))
    vf.drawn_family_pango("sans-serif", runner=r)
    [(argv, timeout)] = r.calls
    assert argv == [sys.executable, "-P", "-m", "scribesense.adapters.pango_probe", "sans-serif", vf.SAMPLE]
    assert timeout == DEFAULT_TIMEOUTS["other"]


def test_sample_is_a_fixed_latin_constant() -> None:
    assert vf.SAMPLE == probe.SAMPLE and probe.SAMPLES == (probe.SAMPLE,)
    assert vf.SAMPLE.isascii() and vf.SAMPLE.isprintable()


def test_probe_is_never_given_other_text() -> None:
    r = Runner(probe_out(READING))
    with pytest.raises(ValueError):
        vf.drawn_family_pango("sans-serif", "My private document text", runner=r)
    assert r.calls == []


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "a\x00b", "tab\there", "x" * 201, " ", "a\ud800"])
def test_invalid_queries_are_refused_and_nothing_runs(bad: str) -> None:
    r = Runner(b"X")
    for call in (lambda: vf.chosen_family(HOST, bad, runner=r), lambda: vf.drawn_family_pango(bad, runner=r)):
        with pytest.raises(ScribeSenseError) as exc:
            call()
        assert exc.value.code is ErrorCode.UNEXPECTED_ADAPTER_ERROR
    assert r.calls == []


def test_query_of_max_length_is_accepted() -> None:
    assert vf.chosen_family(HOST, "x" * vf.MAX_QUERY_LEN, runner=Runner(b"X")) == "X"


# ---------------------------------------------------------------- error mapping

@pytest.mark.parametrize("code", [ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.ADAPTER_TIMEOUT,
                                  ErrorCode.ADAPTER_COMMAND_FAILED])
def test_scribesense_errors_pass_through_unchanged(code: ErrorCode) -> None:
    for call in (lambda r: vf.chosen_family(HOST, "serif", runner=r),
                 lambda r: vf.chosen_family(flatpak("org.gnome.gedit"), "serif", runner=r),
                 lambda r: vf.drawn_family_pango("serif", runner=r)):
        err = ScribeSenseError(code)
        with pytest.raises(ScribeSenseError) as exc:
            call(Runner(error=err))
        assert exc.value is err


@pytest.mark.parametrize("error", [PermissionError(errno.EACCES, "denied"), IsADirectoryError(errno.EISDIR, "dir"),
                                   OSError(errno.E2BIG, "too big")])
def test_launch_os_errors_are_check_unavailable(error: OSError) -> None:
    for call in (lambda r: vf.chosen_family(HOST, "serif", runner=r),
                 lambda r: vf.drawn_family_pango("serif", runner=r)):
        with pytest.raises(ScribeSenseError) as exc:
            call(Runner(error=error))
        assert exc.value.code is ErrorCode.CHECK_UNAVAILABLE


@pytest.mark.parametrize("stdout", [b"", b"   \n", None])
def test_empty_output_is_check_unavailable(stdout: bytes | None) -> None:
    for call in (lambda r: vf.chosen_family(HOST, "serif", runner=r),
                 lambda r: vf.drawn_family_pango("serif", runner=r)):
        with pytest.raises(ScribeSenseError) as exc:
            call(Runner(stdout))  # type: ignore[arg-type]
        assert exc.value.code is ErrorCode.CHECK_UNAVAILABLE


def test_fc_match_output_is_decoded_leniently_and_stripped() -> None:
    assert vf.chosen_family(HOST, "serif", runner=Runner(b"  Caf\xc3\xa9 Sans\n")) == "Café Sans"
    assert vf.chosen_family(HOST, "serif", runner=Runner(b"Bad\xff")) == "Bad�"


@pytest.mark.parametrize("stdout", [
    b"not json", b"[1, 2]", b'{"families": ["x"]}', b'{"v": 2, "families": ["x"]}', b'{"v": 1}',
    b'{"v": 1, "families": []}', b'{"v": 1, "families": "x"}', b'{"v": 1, "families": [1]}',
    b'{"v": 1, "families": [""]}', b'{"v": 1, "families": [null]}', b"\xff\xfe",
])
def test_bad_probe_output_is_check_unavailable(stdout: bytes) -> None:
    with pytest.raises(ScribeSenseError) as exc:
        vf.drawn_family_pango("serif", runner=Runner(stdout))
    assert exc.value.code is ErrorCode.CHECK_UNAVAILABLE


def test_default_runner_is_blocked_in_unit_tests() -> None:
    for call in (lambda: vf.chosen_family(HOST, "serif"), lambda: vf.drawn_family_pango("serif")):
        with pytest.raises(Exception) as exc:  # ProcessLaunchBlocked lives in conftest: match by name
            call()
        assert type(exc.value).__name__ == "ProcessLaunchBlocked"


# ---------------------------------------------------------------- the probe with a fake gi

class _FakeFontDescription:
    def __init__(self) -> None:
        self.family: str | None = None
        self.size: int | None = None

    def set_family(self, family: str) -> None:
        self.family = family

    def set_size(self, size: int) -> None:
        self.size = size

    def get_family(self) -> str | None:
        return self.family


@dataclass(frozen=True)
class Nameless:
    """A run whose font reports no usable family name (None or "")."""

    family: str | None = None


def _run(family: str | None) -> object:
    desc = _FakeFontDescription()
    desc.set_family(family)
    font = types.SimpleNamespace(describe=lambda: desc)
    return types.SimpleNamespace(item=types.SimpleNamespace(analysis=types.SimpleNamespace(font=font)))


class _FakeIter:
    def __init__(self, runs: list[object | None]) -> None:
        self.runs, self.i = runs, 0

    def get_run(self) -> object | None:
        return self.runs[self.i]

    def next_run(self) -> bool:
        self.i += 1
        return self.i < len(self.runs)


@dataclass
class FakePango:
    """Draws the sample as runs of `drawn` (None = end of line); records what the layout was given."""

    drawn: list[str | Nameless | None]
    seen: dict[str, object] = field(default_factory=dict)
    fail_on_layout: bool = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self
        required: list[tuple[str, str]] = []

        class Layout:
            @staticmethod
            def new(context: object) -> Layout:
                if fake.fail_on_layout:
                    raise RuntimeError(f"boom {probe.SAMPLE}")
                fake.seen["context"] = context
                return Layout()

            def set_font_description(self, desc: _FakeFontDescription) -> None:
                fake.seen["family"], fake.seen["size"] = desc.family, desc.size

            def set_text(self, text: str, length: int) -> None:
                fake.seen["text"] = text

            def get_iter(self) -> _FakeIter:
                return _FakeIter([None if f is None else _run(f.family if isinstance(f, Nameless) else f)
                                  for f in fake.drawn])

        fontmap = types.SimpleNamespace(create_context=lambda: "ctx")
        pango = types.SimpleNamespace(SCALE=1024, Layout=Layout, FontDescription=_FakeFontDescription)
        pangocairo = types.SimpleNamespace(FontMap=types.SimpleNamespace(get_default=lambda: fontmap))
        gi = types.ModuleType("gi")
        gi.require_version = lambda name, version: required.append((name, version))  # type: ignore[attr-defined]
        repo = types.ModuleType("gi.repository")
        repo.Pango = pango  # type: ignore[attr-defined]
        repo.PangoCairo = pangocairo  # type: ignore[attr-defined]
        gi.repository = repo  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "gi", gi)
        monkeypatch.setitem(sys.modules, "gi.repository", repo)
        self.seen["required"] = required


def test_probe_prints_families_of_the_runs(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    # A new family appears only AFTER a line end, and the runs end with None like real Pango: a probe
    # that stopped at the first line end would miss ORIGINAL.
    fake = FakePango([READING, None, ORIGINAL, None])
    fake.install(monkeypatch)
    assert probe.main(["sans-serif", probe.SAMPLE]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"v": 1, "families": [READING, ORIGINAL]} and err == ""
    assert fake.seen["family"] == "sans-serif" and fake.seen["size"] == 12 * 1024
    assert fake.seen["text"] == probe.SAMPLE and fake.seen["context"] == "ctx"
    assert ("Pango", "1.0") in fake.seen["required"] and ("PangoCairo", "1.0") in fake.seen["required"]  # type: ignore[operator]
    assert not any(name in ("Gtk", "Adw", "Gdk") for name, _ in fake.seen["required"])  # type: ignore[attr-defined]


def test_probe_output_round_trips_through_drawn_family_pango(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    FakePango([READING]).install(monkeypatch)

    def runner(argv: list[str], timeout: float) -> Proc:
        assert probe.main(argv[4:]) == 0  # argv[:4] = python -P -m module
        return Proc(capsys.readouterr().out.encode())

    assert vf.drawn_family_pango("sans-serif", runner=runner) == (READING,)


def test_probe_family_is_set_literally_not_parsed(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    fake = FakePango([READING])
    fake.install(monkeypatch)
    assert probe.main(["Foo Bold 2", probe.SAMPLE]) == 0
    assert fake.seen["family"] == "Foo Bold 2"


def _assert_silent_failure(rc: int, capsys) -> None:  # type: ignore[no-untyped-def]
    out, err = capsys.readouterr()
    assert rc != 0 and out == "" and err == ""


def test_probe_without_gi_exits_non_zero_silently(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setitem(sys.modules, "gi", None)  # import gi -> ImportError
    _assert_silent_failure(probe.main(["sans-serif", probe.SAMPLE]), capsys)


def test_probe_failure_while_drawing_prints_no_traceback(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    fake = FakePango([READING], fail_on_layout=True)  # the error message contains the sample
    fake.install(monkeypatch)
    _assert_silent_failure(probe.main(["sans-serif", probe.SAMPLE]), capsys)


def test_probe_with_no_runs_fails(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    FakePango([None]).install(monkeypatch)
    _assert_silent_failure(probe.main(["sans-serif", probe.SAMPLE]), capsys)


@pytest.mark.parametrize("drawn", [
    [READING, Nameless(None)], [READING, Nameless("")], [Nameless(None), READING], [READING, None, Nameless(None)],
])
def test_probe_fails_closed_on_a_run_without_a_family_name(monkeypatch, capsys, drawn) -> None:  # type: ignore[no-untyped-def]
    """An unidentified run must not let the rest pass as "every run is the reading family"."""
    FakePango(drawn).install(monkeypatch)
    _assert_silent_failure(probe.main(["sans-serif", probe.SAMPLE]), capsys)


@pytest.mark.parametrize("args", [[], ["sans-serif"], ["sans-serif", "my private text"], ["", probe.SAMPLE],
                                  ["a\nb", probe.SAMPLE], ["x" * 201, probe.SAMPLE],
                                  ["sans-serif", probe.SAMPLE, "extra"]])
def test_probe_refuses_bad_arguments_before_loading_pango(monkeypatch, capsys, args: list[str]) -> None:  # type: ignore[no-untyped-def]
    fake = FakePango([READING])
    fake.install(monkeypatch)
    _assert_silent_failure(probe.main(args), capsys)
    assert "text" not in fake.seen
    assert fake.seen["required"] == []  # gi / Pango never loaded


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")  # runpy: module already imported
def test_probe_runs_as_a_module(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    FakePango([READING]).install(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pango_probe", "sans-serif", probe.SAMPLE])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("scribesense.adapters.pango_probe", run_name="__main__")
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["families"] == [READING]


# ---------------------------------------------------------------- import rules (static)

def _imports(path: Path) -> tuple[set[str], set[str]]:
    """(imported module names, gi.require_version namespaces) anywhere in the file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    required: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names |= {base} | {f"{base}.{a.name}" for a in node.names}
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "require_version" and node.args and isinstance(node.args[0], ast.Constant)):
            required.add(node.args[0].value)
    return names, required


@pytest.mark.parametrize("name", ["verify.py", "pango_probe.py"])
def test_helpers_import_no_gui_safety_or_store(name: str) -> None:
    names, required = _imports(ADAPTERS_DIR / name)
    banned = ("gi.repository.Gtk", "gi.repository.Adw", "gi.repository.Gdk", "scribesense.safety",
              "scribesense.store", "scribesense.ui", "scribesense.controller", "scribesense.reader", "subprocess")
    assert not {n for n in names if any(n == b or n.startswith(b + ".") for b in banned)}
    assert required <= {"Pango", "PangoCairo"}


def test_probe_imports_only_stdlib_at_module_level() -> None:
    tree = ast.parse((ADAPTERS_DIR / "pango_probe.py").read_text())
    top: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    assert top <= set(sys.stdlib_module_names) | {"__future__"}


# ---------------------------------------------------------------- integration (bubblewrap only)

@pytest.mark.integration
def test_real_pango_probe_reports_drawn_families(sandbox) -> None:  # type: ignore[no-untyped-def]
    """The real probe in a fresh process: Pango lays out the fixed sample and names the drawn families."""
    drawn = vf.drawn_family_pango("sans-serif")
    assert drawn and all(isinstance(f, str) and f for f in drawn)
