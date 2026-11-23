# K1 / K2 preset-adjustment amendment — working draft (O3)

**Status: NOT SIGNED OFF.** Built (migrations 003/004, repos, tests) so the store-layer API exists for
O1/O2 to integrate against, but this is a draft proposal to amend frozen contracts K1 (Configuration /
adjustment ranges) and K2 (Preset), per AGENTS.md §4: a contract change needs the contract owner (**O1**)
plus one other owner, and a decision entry in `PLAN_v2.md`, before `contracts/` or `ARCHITECTURE.md` are
actually edited. This session (O3) has edited neither.

Written 2026-11-23 · Source: a task requesting a research-backed accessibility-preset catalogue and
per-field slider limits/warnings.

---

## 1. What's proposed, additively

- **K2 amendment:** 7 new built-in accessibility presets (`neutral-baseline`, `wcag-text-spacing`,
  `high-separation-crowding`, `word-spacing-first`, `low-vision-magnification`, `older-adult-comfort`,
  `read-along-support`), alongside the existing 3 (`default`, `reading`, `high-separation`) — nothing
  existing removed or changed.
- **K1 amendment:** a new field, `word_spacing_em` (an em-value spacing field), alongside the existing
  5 (`letter_em`, `word_scale`, `reading_line`, `ui_line`, `text_scale`). `word_scale` (a multiplier) is
  untouched — `word_spacing_em` is a different unit/semantics, not a rename or replacement.
- **New concept, not in K1/K9 today:** per-field slider *advisory warning* thresholds (`warn_below`,
  `warn_above`) with a human-readable rationale (`why_that_stop`), distinct from the existing hard
  `RANGES` bounds.

## 2. A scoped, documented exception to decision D3

D3 (`ARCHITECTURE.md` K9, confirmed by O1 2026-10-18): built-in presets are **not stored in SQLite** —
they live in `config.presets.BUILTINS` "so they cannot drift." This session (O3) stores the 7 *new*
presets in a new SQL table, `built_in_presets` (migration `003_builtin_presets.sql`), **while leaving the
existing 3 built-ins exactly where D3 put them, untouched.**

This is not a re-litigation of D3 on the merits — it's a consequence of ownership (AGENTS.md §3): O3 owns
`store/`, not `config/`. Appending to `config.presets.BUILTINS` would mean an O3 session editing O1's
package. Putting the new presets in O3's own `store/` migration, as a second and explicitly-flagged
builtin source, was the only path available to this session that doesn't cross that boundary. **O1
should decide** whether to (a) accept this split permanently, (b) have an O1 session move the new 7 into
code instead (fully honoring D3), or (c) move all 10 into SQL (reversing D3 entirely). Nothing downstream
depends on which is chosen — `BuiltInPresetRepo`'s read API is the same shape either way.

## 3. What was built (O3, store-layer only)

- `store/migrations/003_builtin_presets.sql` — `built_in_presets` table, 7 seeded rows, exact values.
- `store/migrations/004_preset_adjustment_limit.sql` — `preset_adjustment_limit` table, 5 confirmed rows.
- `store/repos.py` — `BuiltInPresetRepo` (`get`, `list_all`), `PresetAdjustmentLimitRepo` (`get`,
  `list_all`, `word_spacing_warn_below`), both read-only (no write path — these are immutable reference
  data). Wired into `Store.__init__` as `self.built_in_presets` / `self.preset_adjustment_limit`, same
  "not yet in frozen `contracts.store.Store`" status as `user_fonts`.
- `tests/unit/test_builtin_presets.py` — 19 tests: exact seeded values (both tables), NULL handling,
  idempotent reopening, PK constraints, and all 7 per-preset `word_spacing_warn_below()` values.

## 4. The "8 fields" claim — only 6 verified

The task that prompted this claims 8 adjustable fields. Checked against `contracts/config.py`'s `RANGES`
(the sole source of truth) and `config/settings.py`: only 5 existing + this amendment's new
`word_spacing_em` = **6** are backed by any value anywhere in this repo or the supplied reference. The
other 2 are **not implemented** — no row inserted, nothing invented. If a human can point to where the
remaining 2 are defined (a different reference doc, an external spec), `004_preset_adjustment_limit.sql`
can be extended; until then `preset_adjustment_limit` has exactly 5 rows, deliberately.

## 5. Phase 3 — the per-preset `word_spacing_em.warn_below` exception

`preset_adjustment_limit.warn_below` for `word_spacing_em` is `NULL` — a single static number can't
represent "the selected preset's own original value." `PresetAdjustmentLimitRepo.word_spacing_warn_below
(preset_id)` is the one, centralized place this is computed (joins `built_in_presets.word_spacing_em` for
the given preset). Any future UI warning code should call this one function rather than re-deriving the
per-preset threshold itself — that's what keeps the rule in one place instead of duplicated per call site,
per the task's own requirement.

## 6. Explicitly NOT done here (needs O1/O2, separate cards)

- Wiring sliders to `min_allowed`/`max_allowed`/`step`, displaying advisory warnings, applying a selected
  preset to the live `Configuration` — all `controller/`/`ui/` (O2) or `config/` (O1) work.
- Resolving `base_family_rule` tokens (`platform_sans`, `eligible_sans`) into a concrete font pick — not
  defined anywhere in the existing model; a new O1 design question.
- Reconciling `preset_adjustment_limit`'s bounds with the existing, frozen `RANGES` (different `letter_em`
  bounds/step) — two parallel limit sources until O1 decides whether/how to unify them.

## 7. Sign-off needed

- **O1** (contract owner, K1/K2; content owner of presets per `repos.py`'s own docstring) — accept or
  revise the D3-exception split (§2), the new `word_spacing_em` field, the 7 new preset ids/values, and
  the `base_family_rule` open question.
- **One other owner** (O2 or O4, per AGENTS.md §4's "contract owner + one other owner") to co-sign before
  anyone edits `contracts/config.py`, `contracts/presets.py`, or `ARCHITECTURE.md`.
- A decision entry in `PLAN_v2.md` once signed off (a "proposed, pending sign-off" entry already records
  this build — see the 2026-11-23 entry).
