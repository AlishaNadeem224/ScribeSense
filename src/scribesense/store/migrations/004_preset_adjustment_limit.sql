-- 004_preset_adjustment_limit — O3, K9 addition (draft amendment to K1, pending O1 sign-off; see
-- docs/K1_K2_preset_adjustment_draft.md). Slider hard-bounds and advisory-warning thresholds, driven by
-- data rather than duplicated per-call-site conditions (task requirement: "warning thresholds and
-- explanations must be driven by database records"). These bounds are a NEW, separate limit source for
-- the `built_in_presets` catalogue (003) — they do not replace or alter the existing, frozen `RANGES` in
-- contracts/config.py, which keeps governing the live `Configuration` elsewhere in the app (reconciling
-- the two is an open O1 decision, not made here).
--
-- Only 5 of the claimed 8 adjustable fields are backed by a verified, research-cited value anywhere in
-- this codebase or the supplied reference: letter_em, word_spacing_em, reading_line, ui_line, text_scale.
-- No trace of 3 further fields exists in contracts/config.py, config/settings.py, or elsewhere — they are
-- NOT inserted here, per the task's own instruction not to invent values.
--
-- word_spacing_em.warn_below is intentionally NULL: Phase 3 of the task makes it a PER-PRESET threshold
-- (the selected preset's own original word_spacing_em from built_in_presets, never its current, possibly
-- modified value), not a single constant. See PresetAdjustmentLimitRepo.word_spacing_warn_below() in
-- store/repos.py for the one, centralized place this is computed.

CREATE TABLE preset_adjustment_limit (
  field_name    TEXT PRIMARY KEY,
  min_allowed   REAL NOT NULL,
  max_allowed   REAL NOT NULL,
  step          REAL NOT NULL,
  warn_below    REAL,
  warn_above    REAL,
  why_that_stop TEXT NOT NULL
);

INSERT INTO preset_adjustment_limit
  (field_name, min_allowed, max_allowed, step, warn_below, warn_above, why_that_stop)
VALUES
  ('letter_em', 0.00, 0.25, 0.01, NULL, 0.18,
   'Max sits below the 0.4 em point where speed/comprehension drop off; warns past the largest tested magnitude.'),
  ('word_spacing_em', 0.00, 0.32, 0.01, NULL, 0.27,
   'Warns past the tested increase; lower warning is the coupling floor.'),
  ('reading_line', 1.20, 2.00, 0.05, 1.50, 1.75,
   'Warns below the WCAG floor, and at 1.80 which was flagged as impairing.'),
  ('ui_line', 1.20, 1.80, 0.05, NULL, NULL,
   'No evidence in either direction, so nothing to warn against.'),
  ('text_scale', 0.50, 2.00, 0.05, NULL, 1.60,
   '1.6x is where horizontal scrolling and overlap begin — the display-breaking point.');
