-- 002_user_fonts — W1 user font import (O3, K9 addition). Applied by store.core.apply_migrations.
-- Content-addressed storage for imported font source files: one row per unique file, stored at
-- user_fonts_dir()/<sha256>/<file_name> (proposed K8 accessor; see PLAN_v2.md decision this date).
-- "In use" refusal on delete currently raises the existing FONT_IN_USE code, not a dedicated
-- IMPORTED_FONT_IN_USE (that code is part of W1 Part A's still-unsigned error-code proposal).

CREATE TABLE user_fonts (
  sha256      TEXT PRIMARY KEY,                     -- content-addressed; re-import of identical bytes is a no-op
  family      TEXT NOT NULL,                        -- as read from the font's own name table
  style       TEXT NOT NULL CHECK (style IN ('Regular','Bold','Italic','BoldItalic')),
  file_name   TEXT NOT NULL,                        -- original name; stored at user_fonts_dir()/<sha256>/<file_name>
  size        INTEGER NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('file','folder','installed')),
  imported_at TEXT NOT NULL,                        -- UTC ISO-8601, same convention as every other timestamp
  UNIQUE (family, style)                             -- one stored file per family+style; re-import replaces it
);
CREATE INDEX user_fonts_family ON user_fonts(family);
