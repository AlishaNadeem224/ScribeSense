### K2 — Preset (owner O1) — FROZEN 2026-10-09

A preset stores the user's **intent**; `resolve()` turns it into one concrete `Configuration` (K1).
Only the resolved Configuration is used for building fonts — no second authoritative value. [D A9, A10]

```python
@dataclass(frozen=True)
class WordSpacing:
    mode: Literal["target_em", "multiplier"]   # target_em: recalc when base font changes
    value: float                               # em added (target_em) or × space width (multiplier)

@dataclass(frozen=True)
class PresetValues:            # all six fields, always present [D A10]
    base_family: str
    letter_em: float
    word: WordSpacing
    reading_line: float
    ui_line: float
    text_scale: float

@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    values: PresetValues
    kind: Literal["builtin", "user", "custom"]

def resolve(values: PresetValues) -> Configuration
    # target_em → word_scale = 1 + value / space_width_em(Regular face of base_family),
    # rounded to the 0.25 step, then round_to_steps() + validate()          [D A9]

BUILTINS: tuple[Preset, ...]      # Default, Reading, High Separation   [D]
ORIGINAL = "original"             # sentinel: revert everything (not a preset) [D]
```

UI rule: changing the base font keeps `multiplier` values unchanged and recalculates `target_em` ones. A manual word-spacing edit switches that field to `multiplier` mode. [D A9]

| Built-in | base | letter | word | reading line | UI line | text scale |
|---|---|---|---|---|---|---|
| Default | Noto Sans | 0.12 em | target ≈ +0.16 em | 1.5× | 1.2× | 1.0 |
| Reading | Noto Sans | 0.12 em | 1.5× | 1.8× | 1.2× | 1.0 |
| High Separation | Noto Sans | 0.20 em | 2.5× | 1.6× | 1.2× | 1.0 |

Values are draft-accepted; base font, UI line and text scale per [D A10].

**Rules [D 2026-10-09]:**
1. **Preset IDs:** built-ins use fixed IDs `default`, `reading`, `high-separation`; user presets
   use `str(uuid4())`. The name is for display only (may repeat, may change).
2. **Base fonts are bundled in the repo**, never taken from the system:

```text
fonts/
  manifest.toml          # per family: files (Regular/Bold/Italic/BoldItalic), upstream version,
                         # sha256 per file, licence (SPDX), source URL
  <Family>/
    *.ttf / *.otf
    LICENSE / OFL.txt
```

```python
def base_font_files(family: str) -> dict[str, Path]   # style → bundled file; raises BUNDLED_FONT_MISSING
```

   - The font key (M1.5) already includes the sha256 of every base file, so a font update gives
     a new key automatically.
   - **Before bundling, O1 verifies each licence permits redistribution and modification**
     (SIL OFL expected for all four). A font that fails is removed from `SHORTLIST`.

---

**Open items handed to K2 by M1.1 [2026-10-14]** — no change to the interface above.

1. **`resolve()` must apply K1's rounding and validation, not its own.** Import
   `round_to_steps`, `validate`, `RANGES` and `SHORTLIST` from `config.settings`;
   never restate a bound, a step or a default value. K1 is now amended (see K1.md
   A1–A7) — most relevantly, `validate` can return the new `NOT_A_NUMBER` error.

2. **The word-spacing / letter-spacing coupling check belongs here.** Galliussi et al.
   2020 found that raising letter spacing *without* raising word spacing impaired
   reading speed, and WCAG 1.4.12 implies a 4:3 word-to-letter floor (0.16 ÷ 0.12 em).
   M1.1 cannot implement the check: `word_scale` is a multiplier, and converting the
   em-stated rule needs the Regular face's space width, which a pure configuration
   module must not load. `resolve()` already computes `space_width_em(...)` for
   `target_em` mode, so it is the natural home. Recommended as a **warning that does
   not block**, per Research2 B.2.

3. **Built-in preset values need re-deriving against K1's actual grids.** The values in
   the table above are marked draft-accepted, and Research2's recommended values were
   computed for a *different* vocabulary (additive word spacing in em, `letter_em`
   step 0.01). Two consequences to check when K2 is built:
   - Research2's High Separation letter value 0.18 em is on K1's 0.02 grid; its
     Reading value 0.12 em is too. But Galliussi's tested 0.07 em is **not**
     reachable on that grid (0.06 and 0.08 bracket it).
   - `word` values quoted in em (`+0.16`, `+0.24`) are not `word_scale` values and
     must go through `target_em` mode rather than being written in directly.

---

### K2 — amended M1.2 2026-10-14

Items 1 and 3 above are now **done**. Item 2 is **not** — see B4. No field, built-in
value, preset ID or `kind` changed.

**B1. `resolve()` takes an optional space-width provider.**

```python
def resolve(values: PresetValues, *,
            space_width_em: Callable[[str], float] | None = None) -> Configuration
```

*Why.* The frozen `resolve(values) -> Configuration` needs
`space_width_em(Regular face of base_family)` to convert a `target_em`, but the M1.2
card lists `Depends on: M1.1` and `Data it reads/writes: none`. Reading a bundled font
is neither. **This is a genuine missing dependency, not an oversight on either side** —
`base_font_files()` (above) is the designated reader and does not exist yet, nor does
the `fonts/` tree. Injection resolves it without M1.2 importing a font library: the
parameter is keyword-only with a default, so the frozen one-argument call still works
and the card's worked example `resolve(BUILTINS[0].values)` runs.

*Downstream:* when M1.3 lands, pass a real provider. No caller changes are forced.

**B2. `REFERENCE_SPACE_WIDTH_EM = 0.25` is a provisional fallback, not a measurement.**
`MEASURED_SPACE_WIDTH_EM` ships **empty**; every family currently falls back. Source
for 0.25: Research2 §0.2 D-01d reasons about a font "whose default space is ~=0.25 em"
and adds that it "depends entirely on the font".

⚠️ **The Default preset's resolved word spacing is not yet settled.** With
`word_scale = 1 + 0.16 / space_width` snapped to the 0.25 step, the result flips at a
space width of **0.256 em** — `> 0.256 → 1.5`, `≤ 0.256 → 1.75`. Real sans-serif space
widths sit on top of that boundary, so a 0.01 em measurement difference moves the
Default preset a whole step. Measure the bundled Regular faces before freezing.

**B3. `resolve()` raises `PresetResolutionError` on `error`-level issues**; `warning`
issues never raise. The return type is `Configuration` with no channel for issues, so
errors must either raise or be swallowed, and swallowing would defeat `validate()`.
The exception carries both the offending `Configuration` and the K1 issues so the UI
can point at the field. Nothing is clamped — an out-of-range value is reported, never
silently corrected. Callers wanting the advisory `UI_LINE_ABOVE_RECOMMENDED` run
`validate()` on the returned configuration.

**B4. The coupling warning (item 2) is still NOT implemented.** `resolve()` now holds
the space-width provider, so it has become the natural home, but K2's `resolve`
signature has no channel to return a non-blocking warning, and Research2 rates the
dependency low-confidence (matrix P-BF → P-WS: HYP/DIR, WARN, Conf. Low; rule R-07
*Provisional*; R-03 *Optional/advisory*). Implementing it needs a contract decision
about **where the warning surfaces**, so it is left to the owner rather than invented.

**B5. Reserved IDs, to enforce "must never let a user preset overwrite a built-in".**
`RESERVED_IDS = {"default", "reading", "high-separation", "custom", "original"}`.
`new_user_preset()` generates the `uuid4` itself so a user preset cannot be born with
a reserved ID, and `catalogue()` raises `BuiltinOverwriteError` on a reserved ID, a
`kind="builtin"` claim, or a duplicate ID. Shadowing is rejected rather than ignored:
a shadowed built-in would still appear in the list while resolving to someone else's
values. Display names may repeat; IDs may not.

**B6. `kind="custom"` is the transient working entry**, ID `"custom"`, distinct from
`"user"` (a saved, named preset). `custom_preset()` requires explicit values and has
no default set, per Research2 C.4: "Custom is not a fixed configuration and must not
be given default values."

**B7. Two unreconciled conflicts with Research2, reported not resolved.** K2's table
is frozen and the M1.2 card requires values to match it, so both stand as-is:

- **Default is not neutral.** Research2 C.1/C.3 and decision K-13 recommend
  Default = (platform sans, 0.00 em, neutral word, 1.4, 1.4, 1.0) — a preset that
  "changes nothing" — on the explicit grounds that the spacing evidence is contested
  and "a nonzero default would impose an unproven intervention on every user". K2's
  Default ships WCAG-minimum spacing on. Functionally it is close to Research2's
  *Reading*, and K2 has **no neutral preset at all**: `ORIGINAL` is the only
  unmodified path and it bypasses the pipeline rather than sitting alongside the
  others. This is a product decision for the owner.
- **Reading uses `reading_line` 1.8**, the value Rello et al. 2013 flagged as
  marginally impairing at the top of its tested 0.8–1.8 range (that study found no
  significant main effect of line spacing at all). Research2 capped its own strongest
  preset at 1.6 "deliberately not 1.8". K1 permits 1.8, so it resolves and validates.

**B8. No presentation fields were added.** Research2 Deliverable E recommends colour,
contrast, line length, alignment, paragraph spacing, rulers and highlighting live in a
**separate application-level object**, not in the six-field core, and explicitly not in
the font cache key — a font file cannot enforce a background colour or a wrap policy.
`PresetValues` therefore stays at six fields; a pinned test asserts none of those names
leaked in.

---

### K2 — amended M1.5 2026-10-14

No field, built-in value, preset ID, `kind` or signature changed.

**C1. `base_font_files()` is provisionally implemented in `fontgen/variants.py`.** Rule
2 above specifies it and B1 records that neither it nor the `fonts/` tree exists. M1.2
could work around the gap by injection; M1.5 cannot, because its card's interface is
`build_variants(config)` with no second argument, so a default reader had to exist
somewhere. The stand-in keeps K2's signature and K2's `BUNDLED_FONT_MISSING` exactly,
reads the `fonts/<Family>/` layout documented above, and infers the style of each file
from its name (`NotoSans-BoldItalic.ttf` → `BoldItalic`; a stem with no `-` →
`Regular`; `Oblique` folded into `Italic`; `SemiBold` and other weights ignored rather
than promoted). It raises on an unknown family, an absent directory, or a family with
no Regular.

**When K2's real reader lands it supersedes this one and the stand-in should be
deleted, not merged.** `manifest.toml` *declares* the style→file mapping, which is
strictly better than inferring it from a filename, and it additionally carries the
sha256, licence and upstream version that the inference cannot know. Two things to
preserve in the handover:

- The **manifest's per-file sha256 is not a substitute** for hashing the file at build
  time. M1.5 hashes the bytes it actually opens, so a corrupted or swapped file cannot
  inherit a stale manifest digest and a stale cache hit with it. Use the manifest's
  digest to *verify* the bundle, not to key it.
- Rule 2's promise — "a font update gives a new key automatically" — holds today and is
  tested (`tests/test_variants.py::TestDeterminism::test_a_changed_source_font_changes_the_key`).

**C2. The word/letter coupling warning (item 2, B4) is still not implemented**, and
M1.5 does not change that. It is a `resolve()`-level advisory and never affects font
bytes, so it would not enter the content key in any case.