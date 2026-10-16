### K1 — Configuration (owner O1) — FROZEN 2026-10-09 · amended M1.1 2026-10-14

```python
@dataclass(frozen=True)
class Configuration:          # always resolved and canonical when used (K2 resolve → round → validate)
    base_family: str          # one of SHORTLIST
    letter_em: float          # 0.00–0.30, step 0.02             [D]
    word_scale: float         # 1.0–3.0 × space width, step 0.25  [D]
    reading_line: float       # 1.0–2.0 ×, step 0.1               [D]
    ui_line: float            # 1.0–2.0 ×, step 0.1; warn above 1.3  [D A12]
    text_scale: float         # 1.0–2.0, step 0.05                [D A11]

@dataclass(frozen=True)
class Range:                  # no default here — the Default preset (K2) is the only source of defaults
    min: float
    max: float
    step: float

SHORTLIST = ("Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans")   # [D]
RANGES: dict[str, Range]      # single source of truth for UI controls, validation and tests

@dataclass(frozen=True)
class Issue:
    level: Literal["error", "warning"]
    field: str
    code: str

def round_to_steps(c: Configuration) -> Configuration
def validate(c: Configuration) -> list[Issue]
```

**Rules [D 2026-10-09]:**
1. Values are floats. `round_to_steps()` makes them canonical:
   `round(min + round((v - min) / step) * step, decimals_of(step))`.
   It **never clamps** — an out-of-range value stays out of range so `validate()` reports it.
   Non-finite values (NaN, ±inf) pass through unchanged.
2. Call order is always `round_to_steps()` → `validate()`.
3. No exact float equality: on-step checks use a tolerance of `1e-9`.
4. Issue codes:

| Code | Level | When |
|---|---|---|
| `NON_FINITE` | error | value is NaN or ±inf |
| `OUT_OF_RANGE` | error | outside `RANGES[field]` |
| `UNKNOWN_FONT` | error | `base_family` not in `SHORTLIST` |
| `NOT_ON_STEP` | error | **guard only** — value was not rounded first; never occurs in the normal flow |
| `NOT_A_NUMBER` | error | value is not a real number (`None`, `str`, `bool`, …) — **amended M1.1 2026-10-14** |
| `UI_LINE_ABOVE_RECOMMENDED` | warning | `ui_line` > 1.3 |

5. The UI may tell the user a value was rounded; that is UI feedback, not an `Issue`.

---

**Amendments [M1.1 2026-10-14]** — no field, range, step or existing code changed.

**A1. New error code `NOT_A_NUMBER`.** `Configuration` is a plain dataclass, so its
`float` annotations are not enforced at runtime and a configuration restored from a
store (M2.x) may carry `null` or a string. Two concrete failures motivated the code:
`letter_em="0.12"` raised `TypeError` out of `round_to_steps`, and — because `bool`
subclasses `int` — `text_scale=True` read as `1.0`, i.e. a **valid** value, which is
exactly the "silently making an invalid value valid" failure the contract must avoid.
`round_to_steps` now passes non-numeric values through untouched and `validate`
reports `NOT_A_NUMBER`.
*Downstream:* callers that branch on `level == "error"` are unaffected; callers that
switch on specific codes must add this one. No caller exists yet (M1.2, M1.4–M1.8,
M2.1, M2.5, M3.1, M4.x are unbuilt).

**A2. Tie-breaking in rule 1 is half away from zero.** Rule 1 writes `round()` without
naming a tie rule; Python's built-in `round` is half-to-even, which would send
`letter_em` 0.01 *down* to 0.00 while 0.03 goes *up* to 0.04. Ties now always move
away from the range minimum. No range, step or bound changes, and the card's worked
example is unaffected (`0.131 → 0.14` under either rule).

**A3. `round_to_steps` normalises `-0.0` to `0.0`.** Numerically a no-op (they compare
equal), but their `repr()` differs and M1.5 derives the content key from the canonical
decimal text — without this a configuration could flip between `"-0.0"` and `"0.0"`
and silently invalidate a cached font. Required by Research2 B.5 and F.2.

**A4. Precedence, where several codes could apply to one field.** At most one *error*
per numeric field, in the order `NOT_A_NUMBER` > `NON_FINITE` > `OUT_OF_RANGE` >
`NOT_ON_STEP`. This is load-bearing, not cosmetic: every comparison against NaN is
`False`, so a range check reached before the non-finite check would quietly accept
NaN as in range. The `ui_line` warning is independent and is emitted for any finite
real value above 1.3, including one that is also out of range.

**A5. Scope of the M1.1 card's postcondition.** The card states "every value returned
by `round_to_steps` lies on a step inside its range", which cannot hold alongside
rule 1's no-clamping requirement. Resolved in favour of rule 1: rounded values always
lie on the step lattice, and lie *inside the range* only when the input rounds into
range. Out-of-range inputs stay out and are reported by `validate`.

**A6. `word_scale` semantics confirmed as multiplicative** (`1.0` = unchanged),
resolving Research2's open discrepancy **D-01b**. Consequences, both outside M1.1:
M1.4 must use `new_space = round(original_space × word_scale)`, not the additive
formula; and WCAG 1.4.12's word-spacing floor (0.16 × font size) is **not expressible**
as a `word_scale` without the base face's space width, so M1.1 cannot check it —
K2's `WordSpacing.target_em` mode is the designated conversion point.

**A7. Research2's proposed `W_WORD_LETTER_COUPLING` warning is NOT adopted here.** Its
test, `word_scale < 1.33 × letter_em`, is stated in additive em and is vacuous against
a multiplier that starts at 1.0 (`1.33 × letter_em` never exceeds 0.4). The underlying
finding is real — Galliussi et al. 2020 found that raising letter spacing without
raising word spacing impaired reading speed — so the check is **recommended for K2
`resolve()` or M1.4**, where the font's space width is known. Pinned as a deliberate
absence by `tests/test_config.py::TestResearchInvariants::test_no_coupling_warning_is_emitted`.

---

**Amendment [M1.5 2026-10-14]** — no field, range, step or code changed.

**A8. Which fields reach the font, settled.** K1's comment on `Configuration` left one
question open — "`reading_line`, `ui_line` … enter the key only if M1.4 writes vertical
metrics". M1.4 does (K7 D5), so the answer is **yes**: `letter_em`, `word_scale`,
`reading_line` and `ui_line` are all font-building inputs and all four are hashed into
M1.5's content key. `base_family` is in the key too, but *indirectly* — through the hash
of the bundled file it selects, not as a label, so renaming a shortlist entry does not
invalidate a cached font. `text_scale` stays out, exactly as K1 has always said.

*Downstream:* a **seventh K1 field cannot be added without classifying it** as
font-building or application-level. `fontgen/naming.py` holds the two lists
(`KEYED_FIELDS`, `UNKEYED_FIELDS`) and a test asserts their union is exactly the fields
of `Configuration`, so adding a field to K1 without deciding which side it falls on
fails that test rather than silently defaulting to "not in the key".
