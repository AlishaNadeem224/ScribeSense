-- 001_initial — K9 schema, version 1 (O3, M3.1). Applied by store.core.apply_migrations inside ONE transaction;
-- the runner sets PRAGMA user_version itself. Migrations are never edited after release: add 002_*, 003_* ...
-- Journal tables are reached only through store/journal_tables.py (D5); journal-table changes stay backward compatible.

-- ---- preferences (deletable by delete_preferences) ----
CREATE TABLE presets (
  id         TEXT PRIMARY KEY,                      -- str(uuid4()); built-ins are NOT stored (D3)
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('user','custom')),   -- D3: 'custom' = the one working-set row
  data       TEXT NOT NULL,                         -- Preset values, JSON v1
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((kind = 'custom') = (id = 'custom'))        -- the custom row has the reserved id; nothing else may
);
CREATE TABLE settings (
  name  TEXT PRIMARY KEY,                           -- must be a key of SETTINGS (checked in code)
  value TEXT NOT NULL                               -- JSON scalar; exact type re-checked on read
);
CREATE TABLE recommendation_samples (
  id         INTEGER PRIMARY KEY,
  config     TEXT NOT NULL,                         -- Configuration JSON only — no text, no app names
  created_at TEXT NOT NULL
);

-- ---- font registry (content owner: M1.8) ----
CREATE TABLE font_registry (
  key            TEXT PRIMARY KEY,
  config         TEXT NOT NULL,                     -- Configuration JSON v1 (D8: create() compares only the font-building fields, never text_scale)
  reading_family TEXT NOT NULL,
  ui_family      TEXT NOT NULL,
  styles         TEXT NOT NULL,                     -- JSON array
  files          TEXT NOT NULL,                     -- JSON array (paths under fonts_dir())
  file_sha256    TEXT NOT NULL,                     -- JSON array, same order as files
  status         TEXT NOT NULL CHECK (status IN ('building','validated','failed','removed')),
  reason_code    TEXT,
  created_at     TEXT NOT NULL,
  last_used_at   TEXT NOT NULL
);

-- ---- journal tables (reached only through K4; recovery data, never deleted by delete_preferences) ----
CREATE TABLE transactions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('apply','revert','uninstall')),
  state       TEXT NOT NULL CHECK (state IN ('CREATED','SNAPSHOTTED','APPLYING','VERIFYING',
                'AWAITING_CONFIRMATION','REVERTING','COMPLETED','REVERTED','FAILED_CLEAN','REVERT_FAILED')),
  preset_id   TEXT,                                 -- informational; NO foreign key (a preset may be deleted)
  reverts_tx  INTEGER REFERENCES transactions(id) ON DELETE SET NULL,  -- K4 `reverts: TxId`
  reverts_all INTEGER NOT NULL DEFAULT 0 CHECK (reverts_all IN (0,1)),   -- K4 `reverts == "all"`
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  CHECK (NOT (reverts_tx IS NOT NULL AND reverts_all = 1))
);
CREATE INDEX transactions_state ON transactions(state);

CREATE TABLE backups (
  id     INTEGER PRIMARY KEY,
  sha256 TEXT NOT NULL,                             -- verified on read (BACKUP_CORRUPT)
  bytes  BLOB NOT NULL
);

CREATE TABLE journal_items (
  id                  INTEGER PRIMARY KEY,
  tx_id               INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  -- SnapshotItem
  location            TEXT NOT NULL,
  target_adapter      TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  target_display_name TEXT NOT NULL,                -- D2: `running` is not stored
  kind                TEXT NOT NULL CHECK (kind IN ('file','key')),
  existed             INTEGER NOT NULL CHECK (existed IN (0,1)),
  old_value           TEXT,                         -- keys: GVariant text; files: NULL
  old_sha256          TEXT NOT NULL,
  file_mode           INTEGER,
  value_type          TEXT,
  backup_id           INTEGER REFERENCES backups(id) ON DELETE RESTRICT,
  -- PlannedWrite (D1) — NULL intent_sha256 means "no write was ever planned"
  intent_sha256       TEXT,
  intent_text         TEXT,
  intent_blob         BLOB,
  -- AppliedItem
  applied_sha256      TEXT,                         -- NULL = write not confirmed
  retained            INTEGER NOT NULL DEFAULT 0 CHECK (retained IN (0,1)),
  revert_outcome      TEXT CHECK (revert_outcome IN ('reverted','skipped_drift','failed')),
  CHECK ((intent_sha256 IS NULL AND intent_text IS NULL AND intent_blob IS NULL)
      OR (intent_sha256 IS NOT NULL AND ((intent_text IS NULL) <> (intent_blob IS NULL)))),
  CHECK (retained = 0 OR applied_sha256 IS NOT NULL)
);
CREATE INDEX journal_items_tx ON journal_items(tx_id);

CREATE TABLE location_records (                     -- PERMANENT; one per location (R2-1)
  location            TEXT PRIMARY KEY,
  target_adapter      TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  target_display_name TEXT NOT NULL,
  -- own copy of the baseline SnapshotItem, so pruning journal_items never loses an original
  kind                TEXT NOT NULL CHECK (kind IN ('file','key')),
  existed             INTEGER NOT NULL CHECK (existed IN (0,1)),
  old_value           TEXT,
  old_sha256          TEXT NOT NULL,
  file_mode           INTEGER,
  value_type          TEXT,
  baseline_backup_id  INTEGER REFERENCES backups(id) ON DELETE RESTRICT,
  managed_sha256      TEXT,                         -- NULL = not managed
  managed_font_key    TEXT,                         -- no FK: registry rows may be 'removed'
  unresolved          TEXT                          -- ErrorCode value or NULL
);

CREATE TABLE active_config (                        -- exactly one row
  id              INTEGER PRIMARY KEY CHECK (id = 1),
  config          TEXT,                             -- NULL = Original (K4 active() is None)
  font_key        TEXT,
  source_preset_id TEXT,                            -- informational; no FK
  style_revision  INTEGER NOT NULL DEFAULT 0,       -- must survive set_original(): kept even when config IS NULL
  CHECK ((config IS NULL) = (font_key IS NULL))
);
INSERT INTO active_config (id) VALUES (1);

CREATE TABLE coverage (
  tx_id               INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  target_adapter      TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  target_display_name TEXT NOT NULL,
  status              TEXT NOT NULL,                -- CoverageStatus value
  detail_code         TEXT NOT NULL,                -- ErrorCode value or str (K6)
  PRIMARY KEY (tx_id, target_id)
);

