"""M1.8 -- the generator API (owner O1). The one entry point K7 describes.

    build(config) -> FontBuildResult            # validated + installed, for Apply
    build_preview(config) -> PreviewFonts       # temporary, private; never installed
    remove_unused() -> list[str]                # keys removed

Everything here is coordination. The configuration grid is K1's, preset resolution is
K2's, eligibility is M1.3's, the patch is M1.4's, the key and the variants are M1.5's,
validation is M1.6's and installation is M1.7's -- this module owns none of it and
re-implements none of it. What it owns is the *order*, the four statuses, and the
promise attached to each one.

The pipeline, and who answers each step
---------------------------------------
======  ==========================================  =====================================
Step    Module                                      Refusal / failure
======  ==========================================  =====================================
1       K1 ``round_to_steps`` + ``validate``         ``refused`` -- the field's error code
2       M1.5 `plan` -> M1.3 ``is_eligible``          ``refused`` -- M1.3's code, or
                                                     ``BUNDLED_FONT_MISSING``
3       M1.5 `content_key` (inside the plan)         --
4       M1.7 ``is_installed`` (integrity-checked)    hit -> ``cached``, no rebuild
5       M1.5 ``build_variant_set`` -> M1.4 ``patch`` ``failed`` -- ``PATCH_UNSUPPORTED``
6       M1.6 ``validate_font``                       ``failed`` -- the validation code
7       M1.7 ``install_set``                         ``failed`` -- ``INSTALL_FAILED``
8       M3.1 registry row                            ``built`` + a warning if it fails
======  ==========================================  =====================================

The order is not arbitrary. Every step that can refuse without side effects runs before
the first step that has any, so a refusal is Research2 I.1's "None -- no artifacts, no
cache mutation" by construction rather than by cleanup. And the key is known at step 3,
*before* anything is patched, which is what makes the card's example ("previously built
config -> cached, instantly") true: a cache hit costs an eligibility pass and a digest,
not eight patched fonts thrown away. That is why M1.5 grew `plan` (K7 H1).

What the statuses promise
-------------------------
``built`` and ``cached`` mean the fonts are installed and validated -- the card's
postcondition -- and nothing else may return them. The card's must-never ("return
``built`` for an unvalidated font") is enforced three ways, not by care:

1. `FontBuildResult` rejects ``built``/``cached`` without a `FontSet` and with a
   ``reason_code``, and rejects ``failed``/``refused`` *with* a `FontSet`. A failure
   cannot be spelled as a success.
2. ``built`` is constructed at exactly one place, after `install_set` returned.
3. M1.7 is handed M1.6's `ValidationResult` and refuses the install itself if it is not
   ``ok`` (K7 G5), so the gate holds even if this module's ordering were wrong.

``failed`` is "we tried and it did not complete"; ``refused`` is "we declined before
attempting" (Research2 I.1). The distinction is in the status, never in a new one: a
finer diagnosis belongs in ``reason_code``, which is passed through from whichever
module produced it -- M1.3's ``BASE_IS_MONOSPACE``, M1.6's ``VALIDATION_*``, M1.7's
``INSTALL_FAILED`` -- unchanged, because re-coding it would lose the diagnosis the user
needs.

What is missing, and what stands in for it
------------------------------------------
**M3.1 does not exist.** The card lists it as a dependency and names this module the
owner of its rows; there is no registry module in the repository. `FontRegistry` is the
seam it must satisfy and `NullRegistry` is the default, so nothing here depends on it
for correctness -- deliberately, because Research2 I.1 requires a ``cached`` return to
rest on "a successful integrity check on the existing artifact, not merely the existence
of a registry row". The filesystem is the authority; the registry is a record.
`installed_sets` exists so that M3.1 can reconcile its rows against it on startup.

**Nothing tracks which fonts are in use.** K7 rule 3 lists five sources -- the active
configuration, saved presets, unfinished transactions, ``LocationRecord``s, live target
settings -- and none of them exists yet. `remove_unused` therefore defaults to removing
**nothing**, which is rule 3's own instruction ("if usage can't be determined, the font
is kept") rather than a stub. `ReferenceSource` is the seam M3 fills.

Platform. Pure coordination: no display server, no global desktop setting, no font
rendering. Wayland and X11 are equally irrelevant here -- the only subprocesses in the
pipeline are M1.6's ``pango-view``/``fc-match`` and M1.7's ``fc-cache``, which are
headless and belong to those modules. The one platform fact this module encodes is that
UI-level reading preferences are not font-building inputs: ``text_scale`` changes
nothing in the key, so changing it returns ``cached`` (Research2 I.4), and the rendering
layer applies it.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol

from scribesense.config.presets import Preset, resolve
from scribesense.config.settings import Configuration, round_to_steps, validate
from scribesense.contracts import fonts as k7
from scribesense.contracts.errors import ErrorCode, ScribeSenseError
from scribesense.contracts.fonts import FontSet
from scribesense.fontgen import cache as install_cache
from scribesense.fontgen.naming import GENERATOR_VERSION
from scribesense.fontgen.patcher import PatchUnsupported
from scribesense.fontgen.validate import ValidationResult, validate_font
from scribesense.fontgen.variants import (
    GENERATOR_ERROR,
    BaseFontIneligible,
    BuildPlan,
    BundledFontMissing,
    build_variant_set,
    error_code_for,
    plan as plan_build,
)

__all__ = [
    "GENERATOR_ERROR",
    "STATUSES",
    "W_BUILD_TEMP_LEAK",
    "W_PREVIEW_TEMP_LEAK",
    "W_REGISTRY_WRITE_FAILED",
    "FontBuildResult",
    "FontRegistry",
    "FontSet",
    "NullRegistry",
    "PreviewFonts",
    "PreviewUnavailable",
    "ReferenceSource",
    "References",
    "build",
    "build_preview",
    "installed_sets",
    "preset_keys",
    "remove_unused",
]


# --------------------------------------------------------------------------- #
# Statuses and codes
# --------------------------------------------------------------------------- #

Status = Literal["built", "cached", "refused", "failed"]

STATUSES: tuple[str, ...] = ("built", "cached", "refused", "failed")
"""K7's four statuses, and there will not be a fifth.

Research2 I.1: "No new statuses are introduced. If a finer distinction ... is later
needed, it belongs in the reason_code, not in a new status."
"""

SUCCESS_STATUSES: tuple[str, ...] = ("built", "cached")
"""The two that promise an installed, validated font set."""

# GENERATOR_ERROR (defined in `variants`, beside `error_code_for`): orchestration itself
# raised. The one outcome `build` may never have is an exception escaping to the UI,
# which would leave the caller in neither the "installed" nor the "refused" state; a bug
# is reported as a failure, never as a pass. Proposed as an ErrorCode (O1 area).

W_REGISTRY_WRITE_FAILED = "W_REGISTRY_WRITE_FAILED"
"""The fonts are installed and valid, but the registry row was not written.

Not a failure: the postcondition is about the fonts, and throwing away a correct
installation because a record of it could not be stored would be the wrong repair.
Research2 H.3 anticipates exactly this -- "crash between install and registry write ->
startup reconciliation detects and repairs" -- which is what `installed_sets` is for.
"""

W_BUILD_TEMP_LEAK = "W_BUILD_TEMP_LEAK"
"""The build's temporary directory could not be removed. Mirrors M1.6's temp-leak
warning, and for the same reason: a leaked directory must not turn a good build bad."""

W_PREVIEW_TEMP_LEAK = "W_PREVIEW_TEMP_LEAK"
"""`PreviewFonts.close` could not remove the preview directory."""


# --------------------------------------------------------------------------- #
# K7's result types
# --------------------------------------------------------------------------- #


# FontSet is K7's (`scribesense.contracts.fonts.FontSet`), re-exported unchanged.


@dataclass(frozen=True)
class FontBuildResult(k7.FontBuildResult):
    """The outcome of one `build` -- K7's `FontBuildResult`, plus diagnostics.

    A subclass, so every caller of the K7 type sees exactly K7's three fields; the two
    added ones have defaults and are for logs, tests and the developer:

    * ``warnings`` -- advisory codes (K7 H2), e.g. ``Italic:VARIABLE_FONT``.
    * ``detail`` -- the pipeline's own fine-grained code (``VALIDATION_ADVANCE_MISMATCH``,
      ``INSTALL_IO_ERROR``, ...) when ``reason_code`` had to fold it into an `ErrorCode`
      (`error_code_for`). ``None`` when the two are the same.

    The invariants in ``__post_init__`` are the card's must-never in executable form.
    They are checked on construction rather than asserted by the caller because this
    object is the only thing the UI sees: if a wrong status could be *spelled*, every
    caller would have to re-derive whether it was trustworthy.

    * ``built``/``cached`` require a `FontSet` and forbid a ``reason_code`` -- a success
      that cannot say what it installed is not a success.
    * ``failed``/``refused`` require a ``reason_code`` and forbid a `FontSet` -- "nothing
      activated" (the card) and "no artifacts" (Research2 I.1), with no object a caller
      could mistake for an installed font.
    """

    warnings: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status: {self.status!r}")
        if self.status in SUCCESS_STATUSES:
            if self.fonts is None:
                raise ValueError(f"{self.status} must carry a FontSet")
            if self.reason_code is not None:
                raise ValueError(f"{self.status} must not carry a reason_code")
        else:
            if not self.reason_code:
                raise ValueError(f"{self.status} must carry a reason_code")
            if self.fonts is not None:
                raise ValueError(f"{self.status} must not carry a FontSet")

    @property
    def ok(self) -> bool:
        """Whether fonts are installed and validated -- ``built`` or ``cached``."""
        return self.status in SUCCESS_STATUSES


class PreviewUnavailable(ScribeSenseError):
    """`build_preview` could not produce a preview.

    **Contract addition, M1.8 (K7 H3).** K7 gives ``build_preview`` the return type
    ``PreviewFonts`` and no error channel at all, yet every refusal `build` can hit --
    an invalid configuration, an ineligible or unpatchable font -- applies to a preview
    too. Returning an empty `PreviewFonts` would make the failure easy to miss at the
    one call site that is in the user's face; a `FontBuildResult` would promise statuses
    (``cached``, ``built``) that a preview can never have. So it raises, carrying the
    same ``reason_code`` vocabulary `build` reports, and the UI shows the same message
    it would for a refused Apply.

    ``code`` is ``PREVIEW_UNAVAILABLE`` (errors contract); ``reason_code`` is what `build`
    would have reported for the same configuration.
    """

    def __init__(self, reason_code: str, detail: str = "") -> None:
        super().__init__(ErrorCode.PREVIEW_UNAVAILABLE)
        self.reason_code = error_code_for(reason_code)
        self.detail = f"{reason_code}: {detail}" if detail else reason_code


@dataclass(frozen=True)
class PreviewFonts(k7.PreviewFonts):
    """Temporary, private font files for the preview -- K7's `PreviewFonts`, K7 rule 2.

    Exactly K7's fields (``reading_family``, ``ui_family``, ``files``, ``styles``,
    ``temp_dir``); the subclass only adds a way to honour the contract's cleanup rule.
    The files are in a directory of their own, outside `fonts_dir()`, and **nothing
    installs them**: they are loaded privately by the preview (K7 rule 2, A5 [V]).

    K7: the caller owns ``temp_dir`` and deletes it when the preview closes. `close` does
    that, and the object is a context manager -- ``with build_preview(config) as p:`` is
    the shape that cannot leak. There is deliberately **no** ``__del__``: a preview whose
    files are still loaded must not lose them because the Python object was collected.
    """

    def close(self) -> bool:
        """Delete ``temp_dir``. Idempotent, never raises; ``False`` if it is still there
        (`W_PREVIEW_TEMP_LEAK`)."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        return not os.path.exists(self.temp_dir)

    def __enter__(self) -> PreviewFonts:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# The seams for modules that do not exist yet
# --------------------------------------------------------------------------- #


class FontRegistry(Protocol):
    """M3.1's font registry, as this module needs it. **M3.1 does not exist yet.**

    The card says M1.8 "owns font registry entries" and reads and writes them "via
    M3.1". This is the interface that implies, kept to the two operations the generator
    actually performs, so that M3.1 can be written against something concrete and this
    module does not have to guess at a schema it does not own.

    Research2 H.3's rule is the important one and it is enforced here, not there:
    **the row is written only after a successful install**, never before, so a row can
    never describe a font that is not on disk. The reverse -- a font on disk with no row
    -- is expected after a crash and is repaired by scanning (`installed_sets`).
    """

    def record(self, entry: Mapping[str, Any]) -> None:
        """Store (or replace) the row for ``entry["key"]``. JSON-serialisable."""

    def forget(self, key: str) -> None:
        """Drop the row for ``key``. Called after a successful removal."""


class NullRegistry:
    """The default `FontRegistry`: remembers nothing, breaks nothing.

    Deliberately not an in-memory stand-in. A fake registry that answered lookups would
    invite exactly the mistake Research2 I.1 forbids -- treating a row as evidence that
    an artifact exists -- and would be dead weight the day M3.1 lands. Nothing in the
    pipeline reads the registry back, so recording nothing changes no behaviour.
    """

    def record(self, entry: Mapping[str, Any]) -> None:
        return None

    def forget(self, key: str) -> None:
        return None


@dataclass(frozen=True)
class References:
    """Which keys are in use, and whether that answer is complete.

    ``complete=False`` means at least one of K7 rule 3's sources could not be consulted,
    and `remove_unused` then removes **nothing**: rule 3's "if usage can't be determined,
    the font is kept" is about the whole answer, not about one source, because an
    unreadable source could be referencing any key at all.
    """

    keys: frozenset[str] = frozenset()
    complete: bool = True


class ReferenceSource(Protocol):
    """Where "is this font still in use?" is answered. **Nothing implements it yet.**

    K7 rule 3 names five sources -- the active configuration (``Journal.active()``), a
    saved preset, an unfinished transaction, any ``LocationRecord`` with
    ``managed_font_key`` or ``unresolved`` set, and any live setting found by inspecting
    the supported font fields of each target. All five live in M3 and in the
    application layer; none exists in the repository today, and importing them from here
    would invert the dependency even once they do.

    "A saved preset" means **every** stored preset, the Custom working set included
    (K9 D3): Custom is persisted, so the font it resolves to is referenced by exactly
    the same argument as a named preset's. Missing it would be the nastiest version of
    this bug to diagnose -- the user's own current configuration would be the one font
    the sweep deleted, and the symptom would appear one launch later. `preset_keys` is
    provided so an implementation does not have to re-derive the key to include them.

    An implementation folds all five into one `References`, and reports an open
    transaction separately, because rule 3 also says the sweep "runs only when no tx is
    active" -- a different condition from a reference and one that blocks the whole pass
    rather than one key.
    """

    def transaction_active(self) -> bool:
        """Whether any transaction is open. ``True`` blocks the whole sweep."""

    def references(self) -> References:
        """Every key referenced by any of K7 rule 3's sources."""


def preset_keys(
    presets: Iterable[Preset],
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
) -> References:
    """The content keys a set of stored presets refers to -- K7 rule 3's preset source.

    Hand it everything the store holds: the saved user presets **and** the saved Custom
    row. It exists so that a `ReferenceSource` implementation (M3, another package) can
    answer "which fonts do the user's presets need?" without re-implementing
    ``resolve -> plan -> key``. That chain is this package's, and a second copy of it in
    the store would eventually disagree -- at which point the sweep would delete a font
    something still points at, which is the one outcome rule 3 exists to prevent.

    A preset that cannot be turned into a key -- it no longer resolves, its base family
    is not bundled, its sources are unreadable -- sets ``complete=False`` rather than
    being skipped. Skipping it would quietly convert "I cannot tell what this preset
    needs" into "this preset needs nothing", and `remove_unused` would then act on an
    answer that is short by exactly the entries it could not read.

    Not cheap: `plan` opens and hashes each base font per distinct configuration, so
    call it once per sweep, not per key. It performs no I/O to the install directory and
    writes nothing.
    """
    keys: set[str] = set()
    complete = True
    for preset in presets:
        try:
            keys.add(plan_build(resolve(preset.values), files=files).key)
        except Exception:  # noqa: BLE001 -- any failure here means "cannot determine"
            complete = False
    return References(frozenset(keys), complete)


# --------------------------------------------------------------------------- #
# Single flight (K7 rule 1)
# --------------------------------------------------------------------------- #

_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()
_install_lock = threading.RLock()
"""K7 rule 1: "single-flight per key (same key waits, different keys may build in
parallel); the install + ``fc-cache`` step is serialized".

Two locks, because those are two different requirements. The per-key lock means a second
caller asking for a key that is mid-build waits and then finds the finished install --
it returns ``cached`` rather than patching the same fonts a second time into the same
destination. The install lock is global and much narrower: ``fc-cache`` rebuilds the
index for the whole application directory, so two installs overlapping there would race
over one index even though their keys differ.

These are in-process locks and nothing more. They do not coordinate with a second
instance of the application; M1.7 refuses a key whose staging directory already exists,
which is the cross-process half and is as far as this design goes (K7 G7).
"""


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------- #
# K7's interface
# --------------------------------------------------------------------------- #


def build(
    config: Configuration,
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
    registry: FontRegistry | None = None,
    renderer: Any | None = None,
    tools: Any | None = None,
) -> FontBuildResult:
    """Configuration in, installed `FontSet` out, or a clear refusal -- K7's `build`.

    Synchronous, as K7 rule 1 requires; it patches, renders and shells out, so callers
    run it off the UI thread. Single-flight per key.

    ``config``
        A K1 `Configuration`. It is put through `round_to_steps` first -- canonicalising
        onto K1's own grid, which is what K2's `resolve` already does, not clamping: an
        out-of-range or non-finite value is **refused**, never corrected (K2 B3). A
        value that is merely off-grid by float noise would otherwise be refused with
        ``NOT_ON_STEP`` for no reason a user could act on, and would key differently
        from the identical configuration that came through `resolve`.
    ``files``
        A ``style -> path`` map overriding K2's `base_font_files`, which does not exist
        yet (K7 E5). Keyword-only with a default, so K7's ``build(config)`` is
        unchanged. Every test uses it; so will the application until the bundled
        ``fonts/`` tree lands.
    ``registry``
        M3.1's `FontRegistry`. Defaults to `NullRegistry`.
    ``renderer``, ``tools``
        M1.6's rendering seam and M1.7's ``fc-cache``/``fc-match`` seam, passed straight
        through. They exist so that the one entry point can be exercised without pango
        and fontconfig on a development machine that has neither; neither can turn a
        failure into a success, because M1.6 asks any renderer for `missing_tools` first
        (K7 F2) and M1.7 consults its tools only after the files are already installed
        (K7 G4).

    Returns a `FontBuildResult` and **never raises** for anything that is a property of
    the configuration, the fonts or the environment. The temporary build directory is
    removed on every exit path, including an exception.
    """
    warnings: list[str] = []
    try:
        config = round_to_steps(config)
        issues = validate(config)
        for issue in issues:
            if issue.level == "warning":
                warnings.append(issue.code)
        errors = [issue for issue in issues if issue.level == "error"]
        if errors:
            # Step 1. Research2 I.2: "Invalid: return refused with the field-level error
            # code. No side effects." The first error is reported; `validate` has a
            # fixed field order, so the code is stable for a given configuration.
            return _refused(errors[0].code, warnings)

        try:
            prepared = plan_build(config, files=files)
        except BundledFontMissing as exc:
            return _refused(exc.code, warnings)
        except BaseFontIneligible as exc:
            # M1.3's own code -- BASE_IS_MONOSPACE, NO_LATIN_COVERAGE, ... -- kept as the
            # detail; reason_code is its ErrorCode (K7 E3, `error_code_for`).
            return _refused(exc.reason_code, warnings)

        warnings.extend(f"{style}:{code}" for style, code in prepared.warnings)
        warnings.extend(f"{style}:{code}" for style, code in prepared.skipped)

        with _lock_for(prepared.key):
            return _build_one(
                config, prepared, warnings, registry, renderer, tools
            )
    except Exception as exc:  # noqa: BLE001 -- see GENERATOR_ERROR
        return _refused(GENERATOR_ERROR, warnings, status="failed", detail=repr(exc))


def _build_one(
    config: Configuration,
    prepared: BuildPlan,
    warnings: list[str],
    registry: FontRegistry | None,
    renderer: Any | None,
    tools: Any | None,
) -> FontBuildResult:
    """Steps 4-8, under this key's lock."""
    key = prepared.key

    # Step 4. The lookup is M1.7's, and it is an integrity check, not a directory
    # listing: every installed artifact is re-hashed against the manifest, so a
    # truncated or tampered file is a miss and the build goes ahead (Research2 I.1,
    # "a cached return requires a successful integrity check ... not merely the
    # existence of a registry row"). Nothing has been patched at this point.
    cached = _installed_set(key)
    if cached is not None:
        return FontBuildResult("cached", cached, None, tuple(warnings))

    # One temporary directory per build, owned here and deleted on every path. The
    # leak warning is applied to the finished result rather than appended inside a
    # `finally` -- a `finally` runs after the return value has been built, so the
    # warning would be written to a list nobody reads.
    workdir = Path(tempfile.mkdtemp(prefix=f"scribesense-build-{key}-"))
    try:
        result = _build_artifacts(
            config, prepared, warnings, registry, renderer, tools, workdir
        )
    finally:
        leaked = not _discard(workdir)
    if leaked:
        return replace(result, warnings=(*result.warnings, W_BUILD_TEMP_LEAK))
    return result


def _build_artifacts(
    config: Configuration,
    prepared: BuildPlan,
    warnings: list[str],
    registry: FontRegistry | None,
    renderer: Any | None,
    tools: Any | None,
    workdir: Path,
) -> FontBuildResult:
    """Steps 5-8: patch, validate, install, record. Writes only inside ``workdir``."""
    key = prepared.key

    # Step 5. M1.5 is explicit that the caller owns the output directory, and M1.6's
    # precondition is that validation happens while the artifacts are still in it.
    try:
        variants = build_variant_set(config, out_dir=workdir, plan=prepared)
    except PatchUnsupported as exc:
        # Discovered mid-patch, so `failed` rather than `refused`: everything knowable
        # in advance was already decided by eligibility at step 2 (Research2 I.1's
        # split, and K7 C3).
        return _refused(exc.code, warnings, status="failed")

    # Step 6. `sources` is required in substance (K7 F2) and M1.8 is the module that
    # holds the mapping: the expected advance is source + delta and cannot be predicted
    # from the configuration alone.
    validation = validate_font(
        variants.files, config, sources=prepared.sources, renderer=renderer
    )
    warnings.extend(validation.warnings)
    if not validation.ok:
        # "No activation" -- nothing has left the temporary directory.
        return _refused(
            validation.reason_code or GENERATOR_ERROR, warnings, status="failed"
        )

    # Step 7. Serialised, because fc-cache rebuilds one shared index. The
    # ValidationResult goes with it: M1.7 refuses to install an artifact whose
    # validation did not pass, so the gate does not depend on this module's ordering.
    try:
        with _install_lock:
            installed = install_cache.install_set(
                key, variants.files, validation=validation, tools=tools
            )
    except install_cache.InstallFailed as exc:
        # M1.7 guarantees the filesystem is as it was: either the previous installation
        # of this key or none.
        return _refused(exc.cause_code, warnings, status="failed")

    warnings.extend(installed.warnings)
    fonts = FontSet(
        key=key,
        reading_family=installed.reading_family,
        ui_family=installed.ui_family,
        files=tuple(str(path) for path in installed.files),
        styles=installed.styles,
    )

    # Step 8. After the install and only after it (Research2 H.3).
    if not _record(registry, fonts, config, prepared, validation, installed):
        warnings.append(W_REGISTRY_WRITE_FAILED)

    return FontBuildResult("built", fonts, None, tuple(warnings))


def build_preview(
    config: Configuration,
    *,
    files: Mapping[str, str | os.PathLike[str]] | None = None,
) -> PreviewFonts:
    """Build both variants into a private temporary directory. Never installs.

    K7 rule 2: "``build_preview()`` uses the same generation code, writes to a temp dir
    and loads the files privately for the preview only. Files are deleted when the
    preview closes." The same generation code is literal -- it is `build_variant_set`,
    the same call `build` makes, with a different destination and no step 7.

    **Not validated, and that is deliberate.** M1.6 gates *installation* because an
    installed artifact becomes visible to every application through fontconfig
    (Research2 G.6); a preview is loaded privately, is never activated and cannot shadow
    anything, so the risk validation exists to prevent does not arise. Validating anyway
    would make previewing impossible on a host without ``pango-view`` (K7 F4) and would
    put two subprocess renders in front of a control the user is dragging. The cost is
    stated rather than hidden: a preview shows the artifact as built, not as verified,
    and only `build` promises correctness.

    No cache lookup either: an installed set would be visible to fontconfig by name, and
    reusing it would make the preview a different thing from what the next Apply builds.

    Raises `PreviewUnavailable` with the same reason codes `build` reports.
    """
    config = round_to_steps(config)
    errors = [issue for issue in validate(config) if issue.level == "error"]
    if errors:
        raise PreviewUnavailable(errors[0].code, f"{errors[0].field}")

    try:
        prepared = plan_build(config, files=files)
    except BundledFontMissing as exc:
        raise PreviewUnavailable(exc.code.value, exc.detail) from exc
    except BaseFontIneligible as exc:
        raise PreviewUnavailable(exc.reason_code, exc.style) from exc

    directory = Path(tempfile.mkdtemp(prefix=f"scribesense-preview-{prepared.key}-"))
    try:
        variants = build_variant_set(config, out_dir=directory, plan=prepared)
    except PatchUnsupported as exc:
        _discard(directory)
        raise PreviewUnavailable(exc.code.value, exc.detail) from exc
    except Exception as exc:
        _discard(directory)
        raise PreviewUnavailable(GENERATOR_ERROR, repr(exc)) from exc

    return PreviewFonts(
        reading_family=variants.reading_family,
        ui_family=variants.ui_family,
        files=tuple(str(path) for path in variants.files),
        styles=tuple(_style_of(path) for path in variants.files),
        temp_dir=str(directory),
    )


def remove_unused(
    *,
    references: ReferenceSource | None = None,
    registry: FontRegistry | None = None,
    tools: Any | None = None,
) -> list[str]:
    """Remove installed font sets nothing refers to. Returns the keys removed.

    K7 rule 3 in full, and the answer to "what if we cannot tell":

    * **With no ``references``, nothing is removed.** That is not a stub standing in for
      a missing module -- it is rule 3's own instruction, "if usage can't be determined,
      the font is kept", applied to the situation where *no* source can be consulted
      because none exists yet (see `ReferenceSource`). An empty list is the correct and
      safe answer, and it is what the application gets until M3 lands.
    * **`References.complete` false -> nothing is removed**, for the same reason: a
      source that could not be read might reference any key.
    * **An open transaction -> nothing is removed.** Rule 3: "runs only when no tx is
      active". Checked before anything is inspected, not per key.

    What is removed goes through M1.7's `remove`, which applies its own protections on
    top of these -- path confinement, a refusal for any key whose install is in progress
    -- and which deletes only that key's directory. A key M1.7 refuses is simply kept
    and not reported as removed; a refusal here is never an error, because the whole
    operation is housekeeping and the safe outcome is to do less of it.

    The registry row is dropped only after the files are gone, the mirror of the install
    ordering: a row for a font that is still on disk is recoverable by scanning, a
    deleted font with a live row is not.
    """
    if references is None:
        return []
    try:
        if references.transaction_active():
            return []
        in_use = references.references()
    except Exception:  # noqa: BLE001 -- an unreadable source means "cannot determine"
        return []
    if not in_use.complete:
        return []

    removed: list[str] = []
    for key in install_cache.installed_keys():
        if key in in_use.keys:
            continue
        try:
            with _install_lock:
                gone = install_cache.remove(key, tools=tools)
        except install_cache.RemoveRefused:
            continue
        if gone:
            removed.append(key)
            try:
                (registry or NullRegistry()).forget(key)
            except Exception:  # noqa: BLE001 -- the files are already gone
                pass
    return removed


def installed_sets() -> list[FontSet]:
    """Every complete, verified installation on disk, as `FontSet`s.

    The filesystem is the authority on what is installed, which is why this scans rather
    than reading a registry. Research2 H.3 asks for reconciliation "by scanning on
    startup" and I.3 for recovery from "a crash between install and registry write";
    this is what M3.1 reconciles its rows against. Keys whose integrity check fails are
    left out -- they are not installed, by M1.7's definition -- and left alone.
    """
    found = []
    for key in install_cache.installed_keys():
        fonts = _installed_set(key)
        if fonts is not None:
            found.append(fonts)
    return found


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _installed_set(key: str) -> FontSet | None:
    """`FontSet` for an installed key, or ``None`` -- M1.7 decides which.

    The families and styles come from the installed manifest rather than from the plan,
    because what matters on a cache hit is what is *on disk*: a set installed when the
    family shipped an Italic must not be reported as having one if it no longer does.
    """
    if not install_cache.is_installed(key):
        return None
    manifest = install_cache.read_manifest(key) or {}
    paths = install_cache.installed_files(key)
    return FontSet(
        key=key,
        reading_family=manifest["reading_family"],
        ui_family=manifest["ui_family"],
        files=tuple(str(path) for path in paths),
        styles=tuple(manifest["styles"]),
    )


def _refused(
    reason_code: str,
    warnings: Iterable[str],
    *,
    status: Status = "refused",
    detail: str = "",
) -> FontBuildResult:
    """A `FontBuildResult` with no `FontSet` -- the only way a failure is spelled.

    ``reason_code`` may be one of the pipeline's fine-grained codes; the result carries
    its `ErrorCode` (`error_code_for`) and keeps the original in ``detail``.

    ``detail`` (the argument) is appended to ``warnings`` as ``"<code>:<detail>"``. It is
    used for one thing -- the exception text behind a `GENERATOR_ERROR` -- and a free-text
    field on K7's result would invite callers to parse prose; the code is what they
    branch on, and the text is there so a bug report says what actually happened.
    """
    collected = list(warnings)
    if detail:
        collected.append(f"{reason_code}:{detail}")
    code = error_code_for(reason_code)
    return FontBuildResult(
        status, None, code, tuple(collected), None if code == reason_code else reason_code
    )


def _style_of(path: Path) -> str:
    """The style of one built file: names are ``<family>-<style><suffix>`` (M1.5)."""
    return path.stem.rsplit("-", 1)[1]


def _record(
    registry: FontRegistry | None,
    fonts: FontSet,
    config: Configuration,
    prepared: BuildPlan,
    validation: ValidationResult,
    installed: Any,
) -> bool:
    """Write the registry row. ``False`` if it could not be written.

    The row is JSON-serialisable end to end -- M1.6's ``measurements`` is (K7 F5), and
    everything added here is a string, a number or a list of them -- because M3.1 stores
    it and M3.1 is a database. The contents are the build's whole identity: what was
    asked for, what was produced, and the validation trail that justified installing it.
    """
    entry = {
        "key": fonts.key,
        "status": "built",
        "generator_version": GENERATOR_VERSION,
        "reading_family": fonts.reading_family,
        "ui_family": fonts.ui_family,
        "styles": list(fonts.styles),
        "files": list(fonts.files),
        "directory": str(installed.directory),
        "source_sha": prepared.source_sha,
        "sources": {style: str(path) for style, path in prepared.sources.items()},
        "config": {
            "base_family": config.base_family,
            "letter_em": config.letter_em,
            "word_scale": config.word_scale,
            "reading_line": config.reading_line,
            "ui_line": config.ui_line,
            "text_scale": config.text_scale,
        },
        "measurements": validation.measurements,
        "install": {
            "status": installed.status,
            "cache_refreshed": installed.cache_refreshed,
            "resolved": installed.resolved,
            "warnings": list(installed.warnings),
        },
        "warnings": [
            *(f"{style}:{code}" for style, code in prepared.warnings),
            *(f"{style}:{code}" for style, code in prepared.skipped),
            *validation.warnings,
            *installed.warnings,
        ],
    }
    try:
        json.dumps(entry)
    except (TypeError, ValueError):
        # A row M3.1 cannot store is a row that would fail at the database boundary
        # instead of here, where it can be reported as a warning on a good build.
        return False
    try:
        (registry or NullRegistry()).record(entry)
    except Exception:  # noqa: BLE001 -- see W_REGISTRY_WRITE_FAILED
        return False
    return True


def _discard(directory: Path) -> bool:
    """Remove a temporary directory. ``False`` if anything survived."""
    shutil.rmtree(directory, ignore_errors=True)
    return not directory.exists()
