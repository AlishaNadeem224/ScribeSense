# K1 / K2 font-import amendment — working draft (O2)

**Status: NOT SIGNED OFF.** Proposal to amend frozen contracts K1 (Configuration) and K2 (Preset) so that
`base_family` may name a user-imported font family, looked up from the ScribeSense font store, alongside
the bundled families. Per AGENTS.md §4: contract owner (O1) + one other owner (O3 or O4), plus a
`PLAN_v2.md` entry, before `contracts/` or `ARCHITECTURE.md` are edited.

Written 2026-11-24 · Source: remaining-work W1 (font import). Independent of O3's
`K1_K2_preset_adjustment_draft.md` — the two amendments touch different fields and must be signed off
separately.

---

## 1. What's proposed, additively

- **K1 amendment:** `base_family` may be (a) one of the four bundled families in `SHORTLIST`, or
  (b) the display name of an **imported** family stored in the font store. No existing bundled family is
  removed or renamed.
- **K2 rule 2 amendment:** "Base fonts are bundled in the repo, never taken from the system" becomes
  **"Base fonts are bundled in the repo or imported into the ScribeSense font store — never read in place
  from the system."** The "never from the system" half is unchanged; only "bundled only" loosens.
- **New error codes:** `IMPORT_FAILED`, `IMPORT_NOT_ELIGIBLE`, `IMPORTED_FONT_IN_USE`, added to
  `contracts/errors.py` (none of the three exist today; `errors.py` has `BUNDLED_FONT_MISSING`,
  `FONT_UNREADABLE`, `BASE_IS_MONOSPACE`, `BASE_IS_ICON_FONT` — the import codes are distinct events).

## 2. Why this is needed now

`contracts/config.py:29` today: `base_family: str  # one of SHORTLIST`.
`config/settings.py:509–510` today: `if c.base_family not in SHORTLIST: issues.append(Issue("error",
"base_family", UNKNOWN_FONT))`.

The font-import UI (O2.6) cannot be built until `base_family` may legally name an imported family.
Without the amendment, the only legal values are the four bundled families; every imported family is
`UNKNOWN_FONT`. Adding the UI without the contract change would mean the UI offers choices the
validator rejects — the exact failure mode AGENTS.md §4 exists to prevent.

## 3. Scope of the code changes once signed off (for sign-off visibility)

None of this is built yet; the list exists so reviewers know the blast radius before approving.

- `contracts/config.py:29` — comment becomes "one of SHORTLIST, or a stored imported family".
- `contracts/errors.py` — add the three import codes (a new "Fonts — import" block after the existing
  Fonts block).
- `config/settings.py` — the `base_family not in SHORTLIST` check becomes
  `base_family not in SHORTLIST and not imported(base_family)`.
- `config/presets.py` — `base_font_files(family)` and `space_width_em(base_family)` must consult the
  font store for imported families, with the repo `fonts/` tree as the development fallback (the
  fonts-location blocker, W2.1).
- `safety/font_references.py` — counts imported sources; `FONT_IN_USE` extends to imported fonts still
  referenced by a kept preset.
- `store/` — `user_fonts` table (K9 addition) with content-addressed copies at
  `data_dir()/user-fonts/<sha256>/`.

## 4. What this amendment does NOT do

- Does not touch `word_spacing_em` or preset count — those are O3's amendment, a separate sign-off.
- Does not define how an imported font is parsed or validated — that is card W1 work (safe parsing in a
  separate process with a timeout, 32 MB cap, required tables, not icon/monospace, Latin coverage) and
  follows the contract, not precedes it.
- Does not change `BUNDLED_FONT_MISSING` — a missing bundled file is still an error; importing a font
  does not relax that.

## 5. Sign-off needed

- **O1** (Sukhmani, contract owner K1/K2) — accept or revise the `base_family` wording and the K2 rule 2
  loosening.
- **One other owner** (O3 or O4) — co-sign before `contracts/config.py`, `contracts/errors.py`,
  `ARCHITECTURE.md` are edited.
- **A `PLAN_v2.md` entry** once signed off.

**Until signed off:** no code change. O2.6 remains blocked.