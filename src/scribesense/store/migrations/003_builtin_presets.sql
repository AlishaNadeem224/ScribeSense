-- 003_builtin_presets — O3, K9 addition (draft amendment to K2, pending O1 sign-off; see
-- docs/K1_K2_preset_adjustment_draft.md). Research-backed accessibility preset catalogue, stored in SQL
-- as a documented, scoped EXCEPTION to decision D3 ("built-ins are NOT stored, so they cannot drift"):
-- D3 itself is not reversed for the existing 3 built-ins (`config.presets.BUILTINS` stays the source of
-- truth for `default`/`reading`/`high-separation`, untouched by this migration) — these 7 rows are a
-- SEPARATE catalogue of additional accessibility presets added by this session, which owns `store/` but
-- not `config/` (AGENTS.md ownership: O3 may not edit O1's package to extend BUILTINS in code instead).
-- Immutable reference data: no UPDATE/DELETE path exists anywhere in this package (read-only repo, below).

CREATE TABLE built_in_presets (
  preset_id        TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  base_family_rule TEXT NOT NULL,   -- token ('platform_sans' | 'eligible_sans'); resolving to a concrete
                                     -- font pick is an open O1 design question, not decided by this migration
  letter_em        REAL NOT NULL,
  word_spacing_em  REAL NOT NULL,
  reading_line     REAL NOT NULL,
  ui_line          REAL NOT NULL,
  text_scale       REAL NOT NULL
);

INSERT INTO built_in_presets
  (preset_id, name, base_family_rule, letter_em, word_spacing_em, reading_line, ui_line, text_scale)
VALUES
  ('neutral-baseline',          'Neutral Baseline',        'platform_sans', 0.00, 0.00, 1.40, 1.40, 1.00),
  ('wcag-text-spacing',         'WCAG Text Spacing',       'eligible_sans', 0.12, 0.16, 1.50, 1.40, 1.00),
  ('high-separation-crowding',  'High Separation',         'eligible_sans', 0.18, 0.24, 1.60, 1.50, 1.00),
  ('word-spacing-first',        'Word Spacing First',      'eligible_sans', 0.00, 0.27, 1.40, 1.40, 1.00),
  ('low-vision-magnification',  'Low Vision Magnification','eligible_sans', 0.12, 0.16, 1.50, 1.40, 1.60),
  ('older-adult-comfort',       'Visual Comfort Ceiling',  'eligible_sans', 0.00, 0.04, 1.20, 1.20, 1.00),
  ('read-along-support',        'Read-Along Support',      'eligible_sans', 0.12, 0.16, 1.50, 1.40, 1.00);
