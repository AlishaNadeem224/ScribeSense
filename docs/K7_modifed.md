### K7 — Font generator API (owner O1) — FROZEN 2026-10-09 · amended R2 2026-10-09 · amended M1.3 2026-10-14

```python
@dataclass(frozen=True)
class FontSet:
    key: str                      # content key (M1.5)
    reading_family: str           # "AccessSans-<key>"
    ui_family: str                # "AccessSansUI-<key>"
    files: tuple[str, ...]        # installed file paths
    styles: tuple[str, ...]       # styles actually built, e.g. ("Regular", "Bold") — partial allowed [D R2]

@dataclass(frozen=True)
class FontBuildResult:
    status: Literal["built", "cached", "refused", "failed"]
    fonts: FontSet | None         # None unless built/cached
    reason_code: str | None       # e.g. "BASE_IS_MONOSPACE", "VALIDATION_FAILED"

def build(config: Configuration) -> FontBuildResult              # validated + installed, for Apply
def build_preview(config: Configuration) -> PreviewFonts         # temporary, private; never installed
def remove_unused() -> list[str]                                 # keys removed
```

**Rules [D]:**
1. `build()` is **synchronous**; callers run it off the UI thread. Single-flight per key
   (same key waits, different keys may build in parallel); the install + `fc-cache` step is serialized.
2. **Preview never installs:** `build_preview()` uses the same generation code, writes to a temp
   dir and loads the files privately for the preview only (A5 [V]; image fallback if unreliable).
   Files are deleted when the preview closes.
3. **`remove_unused()` keeps** a font if it is referenced by [D R2]:
   - the active configuration (`Journal.active()`);
   - a saved preset (keeps switching fast);
   - an unfinished tx;
   - any `LocationRecord` with `managed_font_key` or `unresolved` set;
   - any live setting found by **inspecting the supported font fields** of each target
     (e.g. the family names in a browser `Preferences` file — not its whole-file hash).
   If usage **can't be determined**, the font is kept. Runs only when no tx is active.
4. **Partial styles [D R2]:** a base font may lack styles (e.g. no italic). Only available styles are
   built and reported in `FontSet.styles`; ScribeSense **does not promise** how other apps synthesize
   a missing style. [V] check each bundled family's actual files.

---

### Amended M1.3 2026-10-14 — the eligibility reason-code vocabulary

No signature in K7 changed. This records the `reason_code` values M1.3 now produces,
because `FontBuildResult.reason_code` is where they surface and K7 already quotes one
of them (`"BASE_IS_MONOSPACE"`) as an example.

**C1. Refusal codes owned by M1.3** (`config/rules.py`). When `is_eligible()` returns
`ok=False`, M1.8 maps it to `status="refused"` and passes the code through unchanged:

| Code | Meaning |
|---|---|
| `FONT_UNREADABLE` | missing, not a font, or unparseable (includes corruption inside tables) |
| `FONT_TOO_LARGE` | exceeds the 32 MB pre-parse budget; never parsed |
| `MISSING_TABLE` | one of `head, hhea, hmtx, maxp, cmap, name` absent |
| `NO_UPM` | `head.unitsPerEm` absent or ≤ 0 — every em conversion divides by it |
| `BASE_IS_ICON_FONT` | icon/symbol font |
| `BASE_IS_MONOSPACE` | monospaced (already named in K7 above) |
| `NO_LATIN_COVERAGE` | below half the 52 core ASCII letters, including non-Latin-only fonts |

Exactly one refusal is reported — the first that applies, in the order listed. The
order is contractual so the code is stable for a given file.

**C2. `Eligibility` gains a third field, `warnings: tuple[str, ...] = ()`.** The M1.3
card specifies `Eligibility(ok, reason_code)`; the field is appended with a default so
that two-argument positional construction is unchanged and no caller is forced to
adapt. It was needed because three conditions are "True + warning" rows in Research2
D.4 and there was nowhere to put them: `PARTIAL_LATIN_COVERAGE`, `ITALIC_ONLY`,
`VARIABLE_FONT` (always in that order). Research2 D.3 requires coverage gaps to be
"reported, not silently rendered"; refusing these fonts would contradict the research,
and dropping the observations would silently ship a font with known gaps.

**C3. `VARIABLE_FONT` is a warning, not a refusal — M1.4 decides.** Whether a variable
font can be flattened is only knowable by attempting instantiation at a target weight,
which M1.3 cannot do. Per rule 4 above and Research2 E.4/K-30, M1.4 flattens per weight
and returns `PATCH_UNSUPPORTED` with **no output artifact** if instantiation fails.
M1.3 reports the fact and refuses to guess.

**C4. Open question for M1.8:** should `FontBuildResult` carry eligibility warnings?
Today they are visible only to the direct caller of `is_eligible()`, so a user whose
font has partial Latin coverage gets a successful build with no indication of the gap.
Surfacing them needs a K7 field (e.g. `warnings: tuple[str, ...]`), which is M1.8's
contract decision and is **not** made here.

**C5. Latin spacing scope, for M1.4.** `latin_glyphs(font)` returns **glyph names**
(the `hmtx` key), and excludes any glyph also reachable from a non-Latin or
script-neutral codepoint — homoglyph sharing between Latin `A`, Greek `Alpha` and
Cyrillic `A` is real, and widening a shared glyph would break Research2 D.5's hard
invariant that non-Latin advances are untouched.

⚠️ **Consequence M1.4 must resolve:** digits, ASCII punctuation and the space are
Unicode script *Common*, not Latin, so they are **not** in the set. Tracking applied to
letters alone leaves punctuation and digits unspaced. The space glyph is handled
separately by `word_scale`; digits and punctuation are not handled at all. Whether to
widen the spacing set is a contract decision for M1.4, not a fix to make in M1.3.

---

### Amended M1.4 2026-10-14 — the patcher

`patch(source_path, config, line, out_path) -> None` is implemented in
`fontgen/patcher.py`. The four positional parameters are unchanged.

**D1. Two keyword-only parameters added, both defaulted.**

```python
def patch(source_path, config, line, out_path, *,
          family_name: str | None = None,
          instance: Mapping[str, float] | None = None) -> None
```

Each exists because a card acceptance test is otherwise unimplementable:

- **`instance`** — "variable fonts flattened per weight", but the frozen signature
  carries no weight. Without it a variable family could only ever produce its `fvar`
  default instance. Defaults to the font's own axis defaults.
- **`family_name`** — the card's must-never is "keep the original family name", and
  K7 names the real scheme `AccessSans-<key>`. `patch` is given neither the content
  key nor whether it is building the reading or the UI variant, so it cannot compute
  that name. M1.5 passes it; the fallback is `"<source family> Spaced <digest>"`,
  a BLAKE2b-4 digest over family + letter_em + word_scale + line + axis location,
  which is deterministic and can never equal the source family.

Existing four-argument callers are unaffected.

**D2. `PATCH_UNSUPPORTED` is raised as `PatchUnsupported`,** carrying `.code ==
"PATCH_UNSUPPORTED"` for `FontBuildResult.reason_code`. Raised for: unreadable or
unparseable source; missing `head/hhea/hmtx/cmap/name`; no `glyf`/`CFF `/`CFF2`
outlines; unusable `unitsPerEm`; a variable font that declares `fvar` but carries no
`gvar`/`CFF2` deltas, or whose instancing fails or leaves `fvar` present; an unknown
or out-of-range axis; no Latin glyphs; **no space glyph** (word spacing is a required
transformation, so skipping it silently would breach the contract); and any exception
raised mid-patch.

`ValueError` — *not* `PatchUnsupported` — is raised for caller errors: an unvalidated
`Configuration`, a non-finite or non-positive `line`, or a `family_name` equal to the
source family. Conflating them would let a bad configuration masquerade as a bad font.

**D3. `hdmx` and `LTSH` are deleted when present.** Not in the card's permitted list,
but a consequence of the edit the card requires rather than an independent change:
both cache per-size advance widths derived from `hmtx`, so leaving them yields a font
that contradicts itself and renders at the old widths at some ppem sizes. They are
optional caches, so deletion is the correct repair. Research2 E.1 anticipates this,
naming "hdmx/hinting data" among what a patcher may touch. TrueType *instructions* are
**not** stripped — that would change `glyf` and cross the "nothing else changed" line —
so Shamir 2003's hinting-override risk at small ppem is handed to M1.6.

**D4. Ligature removal — a direct conflict with Research2 E.5, resolved for the card.**
E.5 says "Do not remove or rewrite ligature lookups in GSUB — that would alter the
font's shaping behaviour and could break script-specific rendering (Nishikimi et al.
2007)" and recommends only *reporting* ligatures. The card requires removal and tests
for "no `liga` feature". The card governs, and it actually fixes the problem E.5 opens
with: a ligature glyph has no cmap entry, so it is not in `latin_glyphs` and receives
no tracking, leaving `fi` visibly tighter than its neighbours at any nonzero spacing.

Mitigation: **only `liga`** is removed. `rlig` (Required Ligatures) and the Indic and
Arabic shaping features are untouched — those are what Nishikimi et al. are about.
`dlig`/`clig` are left alone too. Feature indices are fully re-mapped across
`ScriptList`, `LangSys`, `ReqFeatureIndex` and `FeatureVariations`; deleting a
`FeatureRecord` without re-indexing would silently apply the *wrong* features, which is
worse than leaving `liga` in place.

⚠️ **Residual risk:** a Latin-plus-Indic font routing conjuncts through `liga` rather
than the script-specific features would lose them. Scoping removal to the `latn`/`DFLT`
scripts is the finer-grained alternative if this ever bites.

**D5. Line metrics are a fallback, and cannot shrink a line box.** `line` sets
`hhea.lineGap` and `OS/2.sTypoLineGap` to `round(line × upm) − (ascender − descender)`
per Research2 E.2.4 (`L = lh − (A + D)`), each metric set kept consistent against its
own ascender/descender, with `USE_TYPO_METRICS` set so the `sTypo*` edit is honoured.
`usWinAscent/usWinDescent` are untouched (clipping bounds — shrinking them clips
accents), and ascender/descender are never moved.

Because a line gap cannot be negative, **a requested `line` below the font's intrinsic
`A + D` is a no-op in the file** — and many text faces sit at 1.2–1.4 em, so `line`
≈ 1.0 typically changes nothing. This is by design: Research2 K-28 and E.9 hold that
line height is an application property and "metric editing alone [is] insufficient".
The rendering layer stays authoritative; the font metrics only help renderers that
compute line height from the font (e.g. `line-height: normal`).

**D6. CFF caveat for M1.6.** Advances are written to `hmtx`, which is authoritative for
OpenType and is what FreeType and HarfBuzz read. CFF charstrings may carry their own
legacy width operands, which are **not** rewritten. Verified that fontTools round-trips
the modified `hmtx` for CFF correctly, but a real CFF font with explicit charstring
widths should be checked by M1.6's render-back step.

**D7. `recalcTimestamp` is disabled before compiling — added for M1.5, 2026-10-14.**
fontTools defaults it to `True`, which makes `head.compile` overwrite `head.modified`
with the current time on every save. Patching one source twice, a second apart,
therefore produced two files differing in those four bytes — harmless to render, fatal
to M1.5, whose must-never is "produce two different outputs for the same inputs" and
whose acceptance test is "same inputs → identical key and bytes". It would equally
defeat M1.6's artifact comparison and M1.7's cache. With it off, `head.modified` is
carried over from the source font, which is itself hashed into the key, so the
timestamp becomes a property of the inputs rather than of the clock. M1.4's existing
`test_patching_is_deterministic` did **not** catch this — both patches ran inside the
same second — so a direct assertion on `head.modified` was added beside it.

---

### Amended M1.5 2026-10-14 — variants, naming and the content key

`content_key(config, source_sha, generator_version)` and `build_variants(config)` are
implemented in `fontgen/naming.py` and `fontgen/variants.py`. Both card signatures are
unchanged, as are the fields of `FontSet` and `FontBuildResult`.

**E1. One key per `FontSet`, covering *both* line values — Research2 F.2 is overruled.**
Research2 proposes a *per-variant* key: `variant_kind` hashed in, and only that
variant's line value included, so that "the Reading key is stable when `ui_line`
changes". `FontSet` has a single `key` field shared by `reading_family` and
`ui_family`, and the M1.5 card's acceptance test requires that changing `ui_line`
**alone** yields a new key. Both force one key per *set*, so `reading_line` and
`ui_line` are both hashed. Research2's open item D-08 — whether the line values enter
the key at all — is closed by D5 above: the patcher does write vertical metrics, so the
two artifacts genuinely differ in bytes.

⚠️ **Consequence, stated rather than hidden.** Changing `ui_line` alone gives the
*reading* artifact a new key although none of its advances or line metrics move. Its
bytes change with it, because the key is embedded in the family name — so the artifact
stays honestly self-identifying — but the rebuild and the reinstall under a new family
name are avoidable work, and every live reference to the old reading family has to be
re-pointed (rule 3 above). The error is in the safe direction: redundant work, never a
wrong cache hit. A two-key scheme would fix it and is not available without reopening
`FontSet`.

**E2. The key formula.** `sha256` over a newline-separated, *named* payload, truncated
to 12 hex characters:

```text
scribesense/font-content-key/1
generator=<generator_version>
source=<sha256 over "<style>=<sha256 of file>" lines, styles sorted>
letter_em=0.12
word_scale=1.50
reading_line=1.5
ui_line=1.2
ligatures=liga
```

Named terms rather than concatenation: `1.2`+`1.5` and `1.25`+`.1` would otherwise
collide. Numbers are fixed-decimal at the precision of their K1 step, so `1.5` and
`1.50` are one value and float noise cannot reach the hash. `ligatures` is M1.4's
`LIGATURE_FEATURES`, imported rather than restated — not a user setting, but a
font-building input, and D4 leaves the removal set open, so widening it invalidates
every cached font automatically. `generator_version` is `naming.GENERATOR_VERSION`,
bumped by hand for any change that can alter output bytes, a fontTools upgrade included.

**In the key:** `letter_em`, `word_scale`, `reading_line`, `ui_line`, the bytes of every
style actually built, the style names, the ligature policy, the generator version.
**Not in the key:** `text_scale` (application-level; M1.4 never reads it — K1,
Research2 F.3/F.6) and `base_family` (already present through the source hash, which
hashes the bytes the family selects rather than the label; renaming a shortlist entry
must not invalidate a cached font). A test asserts that these two lists account for all
six K1 fields, so a seventh cannot be added without someone classifying it.

The key is computed over the styles that **survive eligibility**, not over everything
offered. An ineligible Italic is not in the output, so it must not be in the key —
otherwise one build would get two keys depending on whether a file it never used
happened to be present.

**E3. Two new refusal codes reach `FontBuildResult.reason_code` through M1.5.**

| Raised | Code | When |
|---|---|---|
| `BundledFontMissing` | `BUNDLED_FONT_MISSING` | family not in `SHORTLIST`, no bundled directory, or no Regular |
| `BaseFontIneligible` | *M1.3's own code*, unchanged | the Regular style is present but `is_eligible()` refuses it |

`BaseFontIneligible.code` carries `BASE_IS_MONOSPACE`, `NO_LATIN_COVERAGE` and the rest
of C1 verbatim, so M1.8 passes it through exactly as it would a direct `is_eligible()`
refusal; re-coding it would lose the diagnosis the user needs. An ineligible *optional*
style is not an error (rule 4, and the card): it is dropped and reported.

**E4. `VariantSet`, and two defaulted keyword-only parameters.**

```python
def build_variants(config, *, files=None, out_dir=None) -> list[Path]
def build_variant_set(config, *, files=None, out_dir=None) -> VariantSet
```

`VariantSet` carries `key`, `reading_family`, `ui_family`, `styles`, `files`,
`directory`, `source_sha`, `skipped` and `warnings`. It exists because `FontSet` needs
the first four and M1.8 must not re-derive them — re-deriving the key means a second
implementation of E2, and the two would eventually disagree. `build_variants` is a thin
wrapper returning `list(result.files)`, so the card's interface is intact.

`files` injects a `style -> path` map (needed while `base_font_files()` does not exist —
see E6 — and used by every test); `out_dir` chooses the destination, which
`build_preview` needs for its private temp dir and M1.7 needs in order to stage an
install. The default is a fresh `tempfile.mkdtemp()`; **the caller owns its removal.**

`skipped` (`(style, refusal_code)`) and `warnings` (`(style, M1.3 warning)`) answer C4
with data rather than a decision: the information now survives the build, and whether
`FontBuildResult` grows a field to surface it is still M1.8's call.

**E5. K2's `base_font_files()` is provisionally implemented inside `fontgen/variants.py`.**
K2 rule 2 specifies `base_font_files(family) -> dict[str, Path]` raising
`BUNDLED_FONT_MISSING`, and K2 B1 records that neither it nor the `fonts/` tree exists
yet. M1.2 resolved the same gap by injection; M1.5 cannot, because the card's interface
is `build_variants(config)` with no second argument, so a default reader has to exist.
The implementation reads K2's documented layout and infers styles from file names
(`NotoSans-BoldItalic.ttf` → `BoldItalic`; a stem with no `-` → `Regular`; `Oblique`
folded into `Italic`; `SemiBold` and friends ignored rather than promoted). **When K2's
own reader and `manifest.toml` land, the manifest's declared mapping supersedes this
inference and the body here should be deleted** — the signature and the failure code are
already K2's.

**E6. Limitation: a variable family bundled as one file yields Regular only.** M1.5 maps
one style to one file. A family shipping a single variable file could in principle give
Bold by instancing `wght=700` through D1's `instance` parameter, but every style would
then inherit the source's subfamily name and two artifacts would collide on one full
name — `patch` takes a `family_name` and no subfamily. Fixing it means another `patch`
parameter, which is M1.4's contract decision, not M1.5's. Static per-style files are
what K2's manifest describes, so nothing is blocked today.

**E7. Nothing is ever synthesised.** Research2 F.5 requires that a generator "must not
fabricate" a missing style, because algorithmic emboldening and slanting alter glyph
shapes and have no evidence base here. A style that is absent stays absent, and
`FontSet.styles` says so.

---

### Amended M1.6 2026-10-14 — generated-font validation

`validate_font(paths, config) -> ValidationResult(ok, measurements, reason_code)` is
implemented in `fontgen/validate.py`. The card's signature is unchanged. No field of
`FontSet` or `FontBuildResult` changed.

**F1. `ValidationResult` gains `warnings: tuple[str, ...] = ()`.** Appended with a
default, so the card's three-argument positional construction still works and no caller
adapts — the same resolution M1.3 used for `Eligibility` (C2), for the same reason:
Research2 G.5 has two "True + warning" rows (`W_VALIDATION_KERNING_CHANGED`,
`W_VALIDATION_TEMP_LEAK`) and there is otherwise nowhere to put them. Folding a warning
into `ok=False` would refuse fonts the research says to accept; dropping it would hide
that kerning was lost. `ok` and `reason_code` cannot disagree — the dataclass rejects
`ok=True` with a code and `ok=False` without one, so a failure can never be recorded as
a pass.

**F2. Two keyword-only parameters, both defaulted.**

```python
def validate_font(paths, config, *, sources=None, renderer=None) -> ValidationResult
```

`sources` is the `style -> path` map of the base fonts. **It is required in substance:**
Research2 G.1 steps 6-7 compare the artifact against its source, and the expected Latin
advance is `source advance + delta`, which cannot be predicted from the configuration
alone. The default is `base_font_files(config.base_family)` so the card's two-argument
call works; **M1.8 already holds the mapping and must pass it**, because the default
fails with `VALIDATION_SOURCE_UNAVAILABLE` until K2's `fonts/` tree exists (E5).
`renderer` is the external rendering stack, defaulting to `PangoRenderer`. It is a
testing and portability seam, not an escape hatch: any renderer is asked for
`missing_tools()` first, so substituting one cannot turn a skipped check into a pass.

**F3. The reason-code vocabulary.** `reason_code` carries the *specific* code; M1.8
passes it into `FontBuildResult.reason_code` unchanged, as it does M1.3's. All but two
come from Research2 G.5:

| Code | When |
|---|---|
| `VALIDATION_MISSING_FILE` | a listed path is absent |
| `VALIDATION_PARSE_FAILED` | an artifact does not parse |
| `VALIDATION_UNKNOWN_STYLE` | a file name carries no style this pipeline builds |
| `VALIDATION_KEY_MISMATCH` | the paths come from two different builds |
| `VALIDATION_FAMILY_COLLISION` | an artifact is not a generated family, or still carries the source family / nameID 16 |
| `VALIDATION_MISSING_REGULAR` | a family has no Regular |
| `VALIDATION_SOURCE_UNAVAILABLE` | the base font cannot be read, so no comparison is possible |
| `VALIDATION_ADVANCE_MISMATCH` | a Latin or space advance is not exactly as computed |
| `VALIDATION_NON_LATIN_MODIFIED` | a non-Latin advance moved, structurally or in rendering |
| `VALIDATION_LINE_METRIC_MISMATCH` | the line gap is not this variant's, or `USE_TYPO_METRICS` is unset |
| `VALIDATION_LIGATURES_PRESENT` | `liga` survived the patch |
| `VALIDATION_TOOL_MISSING` | `pango-view` or `fc-match` is not installed |
| `VALIDATION_RESOLUTION_MISMATCH` | the family resolves to another family or another file |
| `VALIDATION_RENDER_FAILED` | a rendering tool failed, timed out, or wrote no PNG |
| **`VALIDATION_FAILED`** | **a rendered measurement is outside tolerance — the card's named code** |
| `VALIDATION_REFLOW_FAILED` | content lost at 200% (WCAG 1.4.4) |
| `VALIDATION_ERROR` | validation itself raised; never reported as a pass |

The card's row is "out of tolerance → `VALIDATION_FAILED`", and that is exactly the
rendered-measurement case; G.5 has no code for it because G.5 enumerates the structural
failures. The two sets compose without overlapping. Three codes are additions, argued
in the module: `VALIDATION_LINE_METRIC_MISMATCH` (G.5 has no code for the field that
*distinguishes the two variants* — without it the reading/UI distinction is unvalidated),
`VALIDATION_TOOL_MISSING` and `VALIDATION_SOURCE_UNAVAILABLE` (G.5 assumes both are
present). The order in the table is the order the checks run, and the **first** failure
is the one reported — contractual, like M1.3's refusal order, because several conditions
co-occur and an unpatched font would otherwise report whichever check happened to run
first.

**F4. A missing tool is a failure, not a skip.** `pango-view` and `fc-match` answer two
questions fontTools cannot: does this family resolve to *this file* rather than a system
font, and does the shaped text come out the predicted width. When they are absent the
result is `ok=False` with `VALIDATION_TOOL_MISSING`, and the structural findings are
still in `measurements` so a developer can see how far it got. **Consequence for M1.8
and for packaging:** on a host without `pango-view`, nothing validates and therefore
nothing installs. That is the intended reading of rule 2 and Research2 G.6 — "the
generator gates installation on a complete validation pass, with no shortcut path" — and
it makes `pango-view` a hard runtime dependency that the Fedora package must declare.

**F5. `measurements` is JSON-serialisable, because the card's postcondition puts it on
the registry entry** (M1.8 → M3.1). Keys: `config`, `key`, `families`, `styles`,
`source_files`, `checks` (one entry per check *including the ones that passed*, so a
registry entry can answer "was the kerning diff ever looked at"), `artifacts` (per file:
expected and observed advances, both line gaps, kerning pair counts), `render` (tool
availability, every extent measured, expected and measured width ratios, the tolerance
used) and `temp_dir_removed`. Research2 left the contents TBD and proposed the G.4
table; this is that table plus the pass/fail trail. A test pins the round-trip through
`json.dumps`.

**F6. Isolation is verified at runtime, not assumed.** The private configuration names
only the directories holding the artifacts under test, with no `<include>` of
`/etc/fonts` or `~/.config/fontconfig`, and redirects `FONTCONFIG_FILE`,
`FONTCONFIG_PATH` and `XDG_CACHE_HOME` so fontconfig can neither read the user's
configuration nor write a cache into their home. That is the card's must-never and
Research2 G.3.

⚠️ **But isolation can fail silently, and that was observed.** The only `fc-match`
available in development — MiKTeX's Windows build — **ignores `FONTCONFIG_FILE`
entirely**: given a nonexistent configuration it neither errors nor changes behaviour,
and it resolved a generated family to `C:/Windows/Fonts/verdana.ttf`. That is a property
of that build and says nothing about Fedora, but it proves the failure mode is real. The
resolution check therefore compares **both** the returned family and the returned file
path against what was expected, so a leaky stack produces
`VALIDATION_RESOLUTION_MISMATCH` rather than a validation of someone else's font. Do not
replace that check with trust in the configuration.

**F7. What has *not* been verified, and what is owed before release.**

1. **No rendering check has ever been executed.** `pango-view` cannot be installed on
   the Windows development machine, so every rendering test runs against an injected
   renderer that models pango analytically. `TestRealRenderingStack` in
   `tests/test_font_validation.py` is written and **skips** in development; running it on
   Fedora is M1.6's remaining acceptance step.
2. **The rendering tolerance is a derived bound, not a measurement.**
   `RENDER_TOLERANCE_PX_PER_GLYPH = 1.0` px per glyph comes from integer-pixel advance
   rounding across two renders, not from the observed spread Research2 G.4 prescribes
   ("none is derivable from the sources reviewed and inventing one would be
   fabrication"). It is deliberately loose — it can only admit a bad font, never reject a
   good one — and the failure it must catch is about four times larger, which a test
   pins. **Re-derive it on Fedora** by rendering the reference string N times and taking
   the spread, and again whenever pango, freetype, fontconfig or the DPI changes.
3. **The 200% check is a proxy.** Research2 G.1 step 8 asks for clipping and overlap
   inspection; what is implemented is that the rendered extent grows in proportion, which
   catches content that was dropped or truncated but not glyphs that overlap at the same
   total width. Full inspection needs pixel analysis and is deferred.
4. **Rendering covers the Regular face of each family only.** A Pango font description
   is family plus size, and style keywords in that string are ambiguous; every style is
   covered by the structural checks, which are the ones that admit no tolerance.
5. **Which Fedora package provides `pango-view` was not determined** from this
   environment and must be confirmed before it is added to the dependency list.

**F8. M1.4 gained a third public formula and one public constant.** `line_gap_units()`
and `USE_TYPO_METRICS` are now exported, and `_apply_line_metrics` calls the former.
M1.6 has to predict exactly what a correct artifact contains, and the only way its
expectation and M1.4's edit can stay in agreement is for both to call one function —
the same reason `letter_delta_units` and `space_advance` were already public. No
behaviour changed; the patcher suite still passes unmodified.

**F9. M1.5 gained `variant_of(family) -> (kind, key) | None`**, the inverse of
`variant_family`. M1.6 is handed a list of files and must work out which variant each
one is — and therefore which line height it should carry — from its family name;
parsing that string inside the validator would be a second copy of the naming scheme
M1.5 owns.

---

### Amended M1.7 2026-10-14 — installation, cache lookup and removal

`install(key, paths) -> list[Path]`, `is_installed(key) -> bool` and `remove(key) -> bool`
are implemented in `fontgen/cache.py`. All three card signatures are unchanged. **No
field of `FontSet` or `FontBuildResult` changed, and the content key (E2) is untouched** —
everything below is installation metadata or cache state, which is where it belongs: a
value that does not change the generated bytes must never enter the key, or two
identical builds would cache separately.

**G1. The installation manifest — a new on-disk artifact, `<key>/manifest.json`.**
`is_installed` cannot be answered without it. A directory listing says what is present;
it cannot say what is *missing*, because the expected file set depends on which styles
the base family shipped (rule 4) and that is not recoverable from the key. Research2 H.2
requires "the key directory, the expected artifact file(s), and the manifest … present
and consistent", and Research2 I.1 requires a `status="cached"` return to rest on "a
successful integrity check on the existing artifact, not merely the existence of a
registry row".

```json
{ "manifest_version": 1, "key": "...", "generator_version": "1.0",
  "reading_family": "AccessSans-<key>", "ui_family": "AccessSansUI-<key>",
  "styles": ["Regular", "Bold"],
  "files": [{"name": "...", "size": 12345, "sha256": "..."}] }
```

It carries **no timestamp, no source path and no hostname**: Research2 H.3's idempotence
row is "install twice; assert identical bytes", and anything that moved on its own would
make two identical installs differ and defeat the no-op path. Checking it costs one
SHA-256 over at most eight files per lookup, which is deliberate — a cache hit that skips
verification is exactly the shortcut the research rules out.

**G2. `InstallResult`, and `install_set`** — the same shape and the same reason as M1.5's
`VariantSet` (E4). The card's `install` returns `list[Path]` and still does;
`install_set` additionally reports `status` (`"installed"` / `"unchanged"`),
`reading_family`, `ui_family`, `styles`, `cache_refreshed`, `resolved`, `warnings` and
`detail`. M1.8 needs the families for `FontSet` and must not re-derive them, and the
degraded-install warnings have nowhere else to go.

**G3. `INSTALL_FAILED` is the only code M1.8 sees; the class of failure is beside it.**
`InstallFailed.code` is always `INSTALL_FAILED` (fixed by the architecture, per Research2
§0), and `.cause_code` carries one of `INSTALL_INVALID_KEY`, `INSTALL_PATH_ESCAPE`,
`INSTALL_NOT_VALIDATED`, `INSTALL_NO_FILES`, `INSTALL_SOURCE_MISSING`,
`INSTALL_SOURCE_NOT_REGULAR`, `INSTALL_FOREIGN_ARTIFACT`, `INSTALL_KEY_MISMATCH`,
`INSTALL_DUPLICATE_FILE`, `INSTALL_IO_ERROR`, `INSTALL_PERMISSION_DENIED`,
`INSTALL_INCOMPLETE`. Research2 H.3 wants "distinct machine-readable codes per failure
class"; K7 has one field for a reason code, so the distinction lives on the exception
rather than in a second user-visible vocabulary. Refused removals raise `RemoveRefused`
with `REMOVE_INVALID_KEY`, `REMOVE_PATH_ESCAPE`, `REMOVE_IN_PROGRESS`,
`REMOVE_REFERENCED` or `REMOVE_IO_ERROR` — a different exception because a refused
removal usually means "leave it alone", not "the build failed".

**G4. A failing `fc-cache` is degraded, not failed.** The card's postcondition is
"`fc-cache` run after every write" and it is, scoped to `fonts_dir()`. But the files are
already correct when it runs, and fontconfig rescans a directory whose mtime moved, so a
missing or failing tool yields `W_INSTALL_CACHE_REFRESH_FAILED` and a successful install,
per Research2 H.3's degraded-mode row. **The install additionally asks `fc-match` whether
each installed family resolves to an installed file** — the card's acceptance test, and
the answer to "a successful copy does not prove discoverability". A family that resolves
elsewhere gives `W_INSTALL_NOT_RESOLVED`; when the refresh failed, one retry is made
before reporting (Research2's "resolution re-verified"). `InstallResult.resolved is None`
means `fc-match` was absent and the question was not asked.

⚠️ **This is the one place in O1 that deliberately uses the user's real fontconfig.**
M1.6's must-never (F6) is about validating an artifact in isolation; M1.7 is asking
whether the user's own system can see it, which a private configuration cannot answer.

**G5. Two defaulted keyword-only parameters on `install`/`install_set`.**

```python
def install(key, paths, *, validation: ValidationResult | None = None,
            tools: FontconfigTool | None = None) -> list[Path]
```

`validation` is **required in substance** — the card's precondition is "validation ok"
and Research2 G.6 gates installation on a complete pass "with no shortcut path" — but
defaulted so the card's two-argument call works. When passed with `ok=False` the install
is refused with `INSTALL_NOT_VALIDATED`; when omitted, the caller is asserting it. M1.8
holds the result and **must** pass it. Nothing is re-validated in the cache module.
`tools` is the `fc-cache`/`fc-match` seam, for testing and portability, and can only add
warnings — the files are on disk before it is consulted.

**G6. `remove(key, *, is_referenced=None)` — where rule 3 is enforced, and where it is
not.** M1.7 refuses to remove a key whose install is in progress (its staging directory
is the open-transaction marker Research2 H.3 asks for) and a key the caller's
`is_referenced` predicate claims. It does **not** implement rule 3: `Journal.active()`,
saved presets, `LocationRecord` and live target settings are M1.8's and M3's, none of
them exist in the repository yet, and importing them here would invert the dependency.
**`remove_unused()` (M1.8) is where rule 3's "if usage can't be determined, keep it" is
decided**; `remove(key)` is the mechanism it drives. Removing an absent key is a success
that removed nothing.

Two further additions support that caller: `installed_files(key)` (K7 `FontSet.files` on
a cache hit, where there is no `InstallResult`) and `installed_keys()` (every key on
disk, which `remove_unused` must enumerate). `reconcile()` sweeps staging directories
abandoned by a crashed install and **reports — never deletes —** key directories that
fail the integrity check.

**G7. What is *not* verified, and what is owed on Fedora.**

1. **No `fc-cache` or `fc-match` has ever been run by this module.** Neither exists on
   the Windows development machine, so every test drives a `FakeFontconfig` that models
   them (it indexes real font files and substitutes when it cannot resolve).
   `TestRealFontconfig` in `tests/test_font_installation.py` is written and **skips** in
   development; running it on Fedora is M1.7's remaining acceptance step, and it is the
   card's acceptance test.
2. **Durability is only as good as the platform.** `fsync` of a directory does not exist
   on Windows, so the temp-write → fsync → rename → fsync-parent ordering Research2 H.4
   prescribes is only fully executed on Linux. Research2 H.4 is explicit that the result
   must not be described as unconditionally atomic even there; what is guaranteed is that
   **no partially populated key directory is ever published**, which is tested.
3. **Staging relies on fontconfig ignoring dot-directories.** `fonts_dir()/.staging`
   holds in-flight copies; if any fontconfig build indexes dotted directories, a crashed
   install could leave an indexable duplicate under the same family name. The window is
   bounded (a sweep runs before every install, and the staging directory is gone before
   `fc-cache` is invoked), but **confirm the behaviour on Fedora**.
4. **Which package provides `fc-cache` is not stated here.** `fontconfig` is a hard
   runtime dependency of this module as `pango-view` is of M1.6 (F4); both belong in the
   Fedora package's requirements.
5. **No file locking.** Rule 1 says the install step is serialised by the caller, and
   that is relied on: two concurrent installs of the same key are refused
   (`INSTALL_INCOMPLETE`) rather than ordered.

**G8. M1.5 gained `is_content_key(key) -> bool`.** A key becomes a directory name, so
M1.7 must reject `"../.."`, a separator, a space, a control character and an overlong
string *before* touching the filesystem (Research2 H.3). The predicate lives with the
format it tests, in `fontgen/naming.py`, so that no second regex can drift away from
`KEY_LENGTH` and start accepting names `content_key` cannot produce. `_checked` now calls
it; no behaviour changed.

**G9. `fonts_dir()` is K8's, implemented here provisionally.** The card lists `K8` as a
dependency; there is no K8 document and no paths module in the repository. `fonts_dir()`
returns `$XDG_DATA_HOME/fonts/scribesense` (`~/.local/share` when unset, and when set to
a relative path, which the XDG specification says to ignore) — the card's path exactly.
**When K8 lands this must delegate to it**, not be duplicated: the whole safety argument
of the module is that one function names the one directory it may write to.

---

### Amended M1.8 2026-10-14 — the generator API

`build`, `build_preview` and `remove_unused` are implemented in `fontgen/generator.py`.
**All three signatures above are unchanged, and so are `FontSet`'s five fields.** One
field is added to `FontBuildResult` (H2) and two types K7 names but never defined are
defined (H3). The content key (E2) is untouched: M1.8 computes nothing, it orders.

The pipeline, and who answers each step:

| Step | Module | Refusal / failure |
|---|---|---|
| 1 | K1 `round_to_steps` + `validate` | `refused` — the field's error code |
| 2 | M1.5 `plan` → M1.3 `is_eligible` | `refused` — M1.3's code, or `BUNDLED_FONT_MISSING` |
| 3 | M1.5 `content_key` (inside the plan) | — |
| 4 | M1.7 `is_installed` (integrity-checked) | hit → `cached`, nothing rebuilt |
| 5 | M1.5 `build_variant_set` → M1.4 `patch` | `failed` — `PATCH_UNSUPPORTED` |
| 6 | M1.6 `validate_font` | `failed` — the validation code |
| 7 | M1.7 `install_set` | `failed` — `INSTALL_FAILED` |
| 8 | M3.1 registry row | `built` + `W_REGISTRY_WRITE_FAILED` if it fails |

Every step that can refuse without side effects runs before the first step that has
any, so Research2 I.1's "refused ⇒ no artifacts, no cache mutation" holds by
construction rather than by rollback.

**H1. M1.5 gained `plan(config, *, files=None) -> BuildPlan`, and `build_variant_set`
takes it back.** The card's example is "previously built config → `cached` instantly",
and that is impossible if the key only exists after everything has been patched: the
cache lookup is *by key*. The alternatives were to patch eight fonts and throw them away
on every cache hit, or to re-derive the key inside M1.8 — a second implementation of E2,
which is exactly what E4 refused to allow for the same reason. `plan` is the first three
steps of `build_variant_set` (resolve sources → eligibility → `source_sha` → key) with
nothing written; passing the plan back means the eligibility pass and the source hashing
happen exactly once per build. `BuildPlan` carries `key`, `source_sha`, `sources`,
`styles`, `skipped`, `warnings`. No existing caller changes: `plan` is keyword-only and
defaulted, and `build_variants(config)` behaves as before.

**H2. `FontBuildResult` gains `warnings: tuple[str, ...] = ()` — this closes C4.** C4
left the question to M1.8 and this is the answer. Four modules already produce
non-blocking observations and every one of them died at this boundary: M1.3's
`PARTIAL_LATIN_COVERAGE` / `ITALIC_ONLY` / `VARIABLE_FONT` (C2), K1's
`UI_LINE_ABOVE_RECOMMENDED`, M1.6's `W_VALIDATION_KERNING_CHANGED` /
`W_VALIDATION_TEMP_LEAK` (F1), M1.7's `W_INSTALL_CACHE_REFRESH_FAILED` /
`W_INSTALL_NOT_RESOLVED` (G4). Research2 D.3 requires coverage gaps to be "reported, not
silently rendered", and a user whose Italic was dropped or whose font the system cannot
resolve has no way to learn it otherwise. Appended with a default, so three-argument
positional construction is unchanged and no caller adapts — the same resolution M1.3,
M1.6 and M1.7 each used.

Composition: K1 and M1.6/M1.7 codes appear verbatim; per-style ones are `"<style>:<code>"`
(`"Italic:FONT_UNREADABLE"`), covering both M1.3 warnings and styles dropped as
ineligible. M1.8 adds `W_REGISTRY_WRITE_FAILED` and `W_BUILD_TEMP_LEAK`.

`FontBuildResult` also now **rejects ill-formed results on construction**: `built` and
`cached` require a `FontSet` and forbid a `reason_code`; `failed` and `refused` require
a `reason_code` and forbid a `FontSet`. The card's must-never ("return `built` for an
unvalidated font") and its failure row ("nothing activated") become unspellable rather
than matters of care. This is the only change that could break an existing caller —
nothing in the repository constructed one before.

**H3. `PreviewFonts` is defined, and `PreviewUnavailable` is added.** K7 names
`PreviewFonts` as `build_preview`'s return type and never gives it fields. It carries
`key`, `reading_family`, `ui_family`, `styles`, `files`, `directory` and `warnings`, and
it is a **context manager** with an idempotent `close()`: rule 2 says "files are deleted
when the preview closes", so the type whose job is cleanup should make the correct usage
the short one. K7 also gives `build_preview` no error channel, yet every refusal `build`
can hit applies to a preview too — hence `PreviewUnavailable`, carrying the same
`reason_code` vocabulary. Returning an empty `PreviewFonts` would hide the failure at the
one call site that is in the user's face, and a `FontBuildResult` would promise statuses
a preview can never have.

⚠️ **A preview is not validated.** M1.6 gates *installation*, because an installed
artifact becomes visible system-wide through fontconfig (Research2 G.6); a preview is
loaded privately, never activated, and cannot shadow anything. Validating anyway would
make previewing impossible on a host without `pango-view` (F4) and would put two
subprocess renders in front of a control the user is dragging. Stated rather than
hidden: **a preview shows the artifact as built, not as verified.** No cache lookup
either — reusing an installed set would make the preview a different artifact from the
one the next Apply builds.

**H4. Statuses mean what Research2 I.1 says, and there is no fifth.** `refused` is
"declined before attempting", `failed` is "attempted and did not complete". The split
decides one case that was open: `PATCH_UNSUPPORTED` is **`failed`**, not `refused`,
because everything knowable in advance was already decided by eligibility at step 2, so
a raise from M1.4 is by definition mid-patch (C3, Research2 I.1). `GENERATOR_ERROR` is
the one code this module owns, for an unexpected exception in orchestration itself — the
same reasoning as M1.6's `VALIDATION_ERROR`: a bug must surface as a failure, never as a
pass, and `build` never raises.

**H5. Two defaulted keyword-only parameters on `build`, plus `files`.**

```python
def build(config, *, files=None, registry=None, renderer=None, tools=None) -> FontBuildResult
```

`files` is K2's `base_font_files` stand-in (E5), as on M1.5 and M1.6. `registry` is
M3.1's. `renderer` and `tools` are M1.6's and M1.7's existing seams, threaded through so
the single entry point can be exercised without pango and fontconfig; neither can turn a
failure into a success (F2, G4). `build_preview` takes `files`; `remove_unused` takes
`references`, `registry` and `tools`. K7's bare `build(config)` /
`build_preview(config)` / `remove_unused()` calls are all unchanged.

**H6. `FontRegistry` is the interface M3.1 must satisfy. M3.1 does not exist.** The card
makes M1.8 the owner of registry rows and names M3.1 as the writer; there is no registry
module in the repository. `FontRegistry` is a two-method Protocol (`record(entry)`,
`forget(key)`) and `NullRegistry` is the default, so **nothing in the pipeline depends on
it for correctness** — deliberately, because Research2 I.1 requires a `cached` return to
rest on "a successful integrity check on the existing artifact, not merely the existence
of a registry row". The filesystem is the authority. The row is written **only after a
successful install** (Research2 H.3) and is JSON-serialisable end to end, carrying the
config, the key, the families, the files, the source digests and M1.6's `measurements`
(F5). A registry failure yields `built` with `W_REGISTRY_WRITE_FAILED`, never a discarded
install.

`installed_sets() -> list[FontSet]` is added for the reverse direction: it scans the
filesystem through M1.7 so M3.1 can reconcile its rows on startup, which is Research2
H.3's and I.3's answer to "crash between install and registry write".

**H7. `remove_unused()` removes nothing until M3 exists, and that is rule 3, not a
stub.** Rule 3's five sources — `Journal.active()`, saved presets, unfinished
transactions, `LocationRecord`s, live target settings — are all M3's or the application
layer's, and none of them exists. Rule 3 also says "if usage can't be determined, the
font is kept", so the correct behaviour with no source to consult is to keep everything;
`remove_unused()` returns `[]`. `ReferenceSource` is the seam M3 fills: `references() ->
References(keys, complete)` and `transaction_active()`. `complete=False` keeps
everything (an unreadable source could reference any key); an open transaction blocks the
whole sweep ("runs only when no tx is active"); a source that raises is treated as
"cannot determine". Removal itself is M1.7's `remove`, whose own refusals (path
confinement, an install in progress) are treated as "keep", never as an error — the whole
operation is housekeeping and the safe outcome is to do less of it.

**H8. K7 rule 1's single flight is implemented with two in-process locks.** A per-key
lock (a second caller for the same key waits, then returns `cached` rather than patching
into the same destination) and one global install lock (`fc-cache` rebuilds a single
shared index, so installs serialise even across different keys). **These do not
coordinate across processes.** A second instance of the application is bounded only by
M1.7 refusing a key whose staging directory already exists (G7, item 5).

**H9. What is *not* verified, and what is owed.**

1. **Nothing has run on Fedora.** Every test substitutes M1.6's renderer and M1.7's
   fontconfig tools, because neither `pango-view` nor `fc-cache` exists on the Windows
   development machine. The generator's own logic is fully exercised; the end-to-end
   claim is only as good as F7 and G7, both of which are still open. On a Fedora host
   with no `pango-view` the honest consequence is visible and tested: **every build
   fails with `VALIDATION_TOOL_MISSING` and nothing installs** (F4).
2. **M3.1 and M3's reference tracking are missing**, as above. Until they land, the
   application gets no registry rows and no font cleanup.
3. **K2's `fonts/` tree is still missing** (E5), so every caller must pass `files=`.
   `build(config)` with no files raises nothing — it returns `refused` with
   `BUNDLED_FONT_MISSING`, which is the correct refusal but will be every user's
   experience until the bundle exists.
4. **No `build` is interruptible.** Rule 1 says callers run it off the UI thread; there
   is no cancellation token, and a patch of eight faces plus two renders is seconds, not
   milliseconds.
