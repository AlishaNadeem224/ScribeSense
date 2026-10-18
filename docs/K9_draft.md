# K9 store — working draft (O3)

**Status (updated 2026-10-18): SIGNED OFF by O3 on 2026-10-15 and BUILT.** The journal decisions D1, D2, D4, D5, D7 were accepted by O4 on 2026-10-15;
M3.1 (store), M3.2 (retention) and the SQLite journal on top of them (O4, M4.10) exist and were reviewed by O4 on 2026-10-16. The decisions live in `ARCHITECTURE.md`
(K9 section); this file keeps the reasoning. D8 was corrected on 2026-10-18 after O1's reply (§9). D3 was confirmed by O1 on 2026-10-18 after a clarifying question (§9). `contracts/store.py` is frozen text; its "DRAFT" docstring is O3's to update by hand
(AGENTS.md §4: agents never edit `contracts/`).

Written 2026-10-14 · Sources: ARCHITECTURE.md K4, K9, M3.1, M3.2, R2-1…R2-6; contracts `store.py`, `journal.py`, `adapter.py`, `presets.py`.

---

## 1. Decisions to make (recommendation first) — D1–D7 from the first draft, D8–D9 added 2026-10-15

| # | Question | Recommendation | Who must agree |
|---|---|---|---|
| D1 | `JournalItem.intent` is a `PlannedWrite` (has `payload`) but K9 stores only `intent_sha256`. | Store the payload (`TEXT` for keys, `BLOB` for files). No contract change. Recovery only needs the hash, but the K4 type promises the payload. Payloads are tool-config bytes (fontconfig / CSS / gsettings text), never reader text. | O4 (confirms nothing in K4 expects less) |
| D2 | `Target` has 4 fields; `running` is transient. | Persist `adapter`, `target_id`, `display_name` as three columns. On read set `running=False` (stale by definition; callers re-detect). Never store window titles. | O4 |
| D3 | **(revised 2026-10-15 after O1's handoff, see §8)** `Preset.kind` is `"builtin" \| "user" \| "custom"`; K9 only says "built-in → `PRESET_READ_ONLY`". | Built-ins are **not stored**: `PresetRepo.list()` merges `config.BUILTINS` (needs `store` → `config` import; allowed, the reverse is forbidden). Only `"user"` is persisted. `save()` of `"custom"` → `ValueError` (it is unsaved editing state). | **O1** (meaning of `"custom"`) |
| D4 | Retention wording differs: K9 "outside the newest `keep_last`" vs M3.2 "30 days / last 10". | A terminal tx is deleted only if **older than the cutoff AND outside the newest `keep_last` terminal txs AND not referenced** (i.e. kept if recent *or* among the last N). Plus a new rule: **always keep the newest `COMPLETED` apply** (see §5, finding F2). | O4 (they implement the journal API) |
| D5 | Who writes SQL for the journal tables? Import rule R3: **only `store/` imports `sqlite3`**, so `safety/` (O4's M4.10 journal) cannot. | `store/` provides the SQL layer for the journal tables (a private `store/journal_tables.py`, written by O3 to O4's K4 spec). O4's `safety/journal.py` calls it. Alternative: relax R3 for one file — rejected, it weakens the one rule that keeps SQLite in one place. | **O4** (this changes who writes what in M4.10) |
| D6 | The DB file name is not defined (K8 has `data_dir()` but no DB path). | `store.default_path() = data_dir() / "scribesense.db"`; `Store(path)` stays as in K9. No K8 change. | O3 only |
| D7 | `location` / `target_id` strings go into the store. AGENTS.md §6 forbids user file paths. | Policy: only **tool-config locations ScribeSense itself manages** (fontconfig rule file, gsettings `schema key`, browser pref file) — never user documents. Add a test that every stored `location` matches an adapter-declared location pattern. | O4 + team |
| D8 | **(from O1's handoff — §8; corrected 2026-10-18 by O1's reply — §9)** The K9 rule "same key, different config → `FONT_IDENTITY_CONFLICT`" would raise a false conflict, because the font key deliberately ignores `text_scale` — and `base_family`, which the key covers through the source hash. | Compare only the key's numeric inputs (`letter_em`, `word_scale`, `reading_line`, `ui_line`) plus the generated families, styles, files and hashes. `base_family` stays in the row for display; a different label with the same bytes is not a conflict (only noted in the log). **Built.** | O3 (O1 confirmed, 2026-10-18) |
| D9 | **(new, from O1's handoff — §8)** O1 proposes typed decimal columns; K9 says versioned JSON; SQLite has no decimal type. | Keep K9's JSON `{"v":1,…}` with fixed-decimal strings; validate with `validate()`; add only cheap CHECKs (`kind`, `word_mode`). Alternative: integers in hundredths. | O3 + O1 |

---

## 2. Schema as built (`store/migrations/001_initial.sql`)

This block is the migration file verbatim (2026-10-18). It differs from the first proposal in three places: `presets.kind` allows `'custom'` with the reserved id
(D3), `transactions.reverts_tx` is `ON DELETE SET NULL`, and the `coverage` key includes the adapter (O4's review note 3). The runner sets
`PRAGMA user_version` itself, in the same transaction as the migration. All timestamps are UTC ISO-8601 with offset; all JSON is `{"v":1,…}`. Per connection
(not in the SQL file): WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`.

Checked in a throwaway in-memory SQLite and by the store tests: the script parses and its CHECK / foreign-key constraints reject a hash without payload, two
payloads, `retained` without an applied hash, a second `active_config` row, `config` without `font_key`, deleting a referenced backup, a stored built-in preset, and
a `custom` kind with any other id.

```sql
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
  PRIMARY KEY (tx_id, target_adapter, target_id)   -- two adapters may reuse a target id
);
```

Not in the schema on purpose: no reader text, no window titles, no document paths, no `running` flag, no built-in presets.

---- preferences (deletable by delete_preferences) ----
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
  reverts_tx  INTEGER REFERENCES transactions(id),  -- K4 `reverts: TxId`
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

PRAGMA user_version = 1;
```

Not in the schema on purpose: no reader text, no window titles, no document paths, no `running` flag, no built-in presets.

---

## 3. K4 / K9 types → columns (sign-off checklist item "journal tables return everything")

| Contract type / field | Stored in | Notes |
|---|---|---|
| `JournalItem.item_id` | `journal_items.id` | |
| `.snapshot` (`SnapshotItem`) | `journal_items` target_*, location, kind, existed, old_value, old_sha256, file_mode, value_type | `target.running` = `False` on read (D2) |
| file bytes for `Journal.backup()` | `backups.bytes` via `backup_id` | sha256 checked on read |
| `.intent` (`PlannedWrite`) | `intent_sha256` + `intent_text` / `intent_blob` | D1; `target`/`location` come from the item |
| `.applied` (`AppliedItem`) | `applied_sha256` | `new_sha256`; target/location from the item |
| `.revert_outcome`, `.retained` | same-named columns | |
| `LocationRecord.baseline` / `.baseline_backup_ref` | `location_records` copy columns + `baseline_backup_id` | ref = backup row id as text |
| `.managed_sha256`, `.managed_font_key`, `.unresolved` | same-named columns | |
| `ActiveConfig` | `active_config` | `None` ⇔ `config IS NULL`; revision kept |
| `TxId` state / kind / `reverts` | `transactions` | `reverts: "all"` → `reverts_all=1` |
| `CoverageEntry` | `coverage` | `last_completed_apply()` = rows of the newest `COMPLETED` apply tx |
| `FontRegistryEntry`, `SETTINGS`, `Preset` | `font_registry`, `settings`, `presets` | tuples → JSON arrays; timestamps UTC |

---

## 4. Where the database is created and who uses it

* **Created by M3.1 only.** `Store.__init__(path, mode="normal")` creates the file if it does not exist, sets the connection pragmas
  and applies `store/migrations/NNN_*.sql` in order (one transaction per migration, `user_version` updated in the same one).
  `mode="recovery"` **never creates, never migrates**: missing file → nothing to recover; newer schema → `SCHEMA_UNSUPPORTED`.
  The file lives at `store.default_path()` = `data_dir()/scribesense.db` (D6).
* **M3.2 never creates or opens a database.** It receives the already-open store/journal (at app startup) and calls one pruning
  function. It touches `transactions`, `journal_items`, `backups` and (by cascade) `coverage`. It never touches
  `location_records`, their backups, `active_config`, presets, settings, fonts or samples.
* Other modules reach the DB **only through K9/K4**, never SQL:

| Table | Content owner (writes through) | Storage code |
|---|---|---|
| `presets`, `settings`, `recommendation_samples` | M2.5 / M2.7 / M1.8 via repos | M3.1 |
| `font_registry` | M1.8 (build/install) via `FontRegistryRepo` | M3.1 |
| `transactions`, `journal_items`, `backups`, `location_records`, `active_config`, `coverage` | M4.10 journal (O4) | **M3.1** (`store/journal_tables.py`, D5) |
| pruning of `transactions` / `journal_items` / `backups` | policy: M3.2 · rule text: K9 | M3.1 SQL, M3.2 caller |

---

## 5. Findings the draft surfaced

* **F1 — D5 above:** as written, M4.10's SQLite journal cannot exist without breaking import rule R3. Decide D5 before O4 starts M4.10.
* **F2 — retention can empty the coverage report.** Reverts and uninstalls are transactions too. After an apply, 10+ later
  txs can push the newest `COMPLETED` apply out of "the last 10", and a 30-day gap makes it eligible, so
  `CoverageRepo.last_completed_apply()` would silently return `[]`. Rule added in D4: never delete the newest `COMPLETED` apply.
* **F3 — `active_config.style_revision` must outlive `config`.** `set_original()` sets active to `None` and still increments the
  revision (K4). A schema that deleted the row for "Original" would reset the counter and break the extension's refresh (A16).
  The draft keeps a single permanent row with a nullable `config`.
* **F4 — `Store.transaction()` vs `Journal.keep()`.** K4 requires `keep()` to be one atomic transaction, but the journal calls
  go through the repos. `store/journal_tables.py` must expose a way to run several writes in one `Store.transaction()`
  (nested calls join, K9). Needs a test: a failure in the middle of `keep()` leaves tx state, `managed` and `active_config` unchanged.
* **F5 — test 7 (two processes) cannot be a thread test** and must run in the bubblewrap runner with a real file in the sandbox
  tmpfs (WAL needs a real filesystem; `:memory:` proves nothing). The sandbox has `--unshare-all`, so both processes must be
  children of the same sandbox.

---

## 6. Acceptance tests → where they live

| K9 test | File | Level |
|---|---|---|
| 1 round-trip, 2 built-in read-only, 3 settings validation, 4 identity conflict (incl. D8), 5 interrupted build, 6 `FONT_IN_USE`, 12 opt-out + canary sweep | `tests/unit/test_store_repos.py` | unit (temp file DB, real sqlite3) |
| 8 migrations (empty, each previous version, failure leaves `user_version`), 9 recovery mode, pragmas, transactions, error mapping, 11 `delete_recovery_data` refusal | `tests/unit/test_store_core.py` | unit |
| journal tables, parity with O4's in-memory journal, **F3, F4**, 10 prune (K9 rule, D4/**F2**) | `tests/unit/test_store_journal_tables.py`, `tests/unit/test_retention.py` | unit |
| 7 several processes writing at once | `tests/integration/test_store_concurrency.py` | bubblewrap |

Unit tests use `sqlite3` on a real temp file (no process is started, so the sandbox guard allows it); the import-rule test restricts `sqlite3` to `store/`.

---|---|---|
| 1 round-trip, 2 built-in read-only, 3 settings validation, 4 identity conflict, 5 interrupted build, 6 `FONT_IN_USE` | `tests/unit/test_store_repos.py` | unit (temp file DB, real sqlite3) |
| 8 migrations (empty, each previous version, failure leaves `user_version`), 9 recovery mode | `tests/unit/test_store_migrations.py` | unit |
| 10 prune, 11 `delete_recovery_data` refusal, **F2, F3, F4** | `tests/unit/test_store_journal_tables.py`, `test_retention.py` | unit |
| 12 opt-out + canary SELECT sweep (every table, every text column) | `tests/unit/test_store_privacy.py` | unit |
| 7 two processes | `tests/integration/test_store_concurrency.py` | bubblewrap |

Unit tests may use `sqlite3` on a real temp file (no process is started, so the sandbox guard allows it); the import-rule test
already restricts `sqlite3` to `store/`.

---

## 7. Sign-off checklist (K9, from ARCHITECTURE.md) — all ticked 2026-10-15

- [x] Column types (§2) — 2026-10-15
- [x] Matches the planned implementation — D3, D4, D6, D7, D8, D9 decided
- [x] The 12 acceptance tests accepted (plus F2, F3, F4)
- [x] Journal tables return everything in `JournalItem` / `LocationRecord` (§3) — O4 accepted D1, D2, D5, 2026-10-15


---

## 8. O1 preset handoff (received 2026-10-15) — review

Source: two files from O1 (`presets_handoff.json` and a prose handoff). **Not yet read from the O1 branch** (the local copy of `origin/O1` is
stale, last commit 2026-10-11); this review is of what was pasted. Scope: **only the preset / `Configuration` part** of the store.
It does not cover `font_registry`, `settings`, samples, or any journal table (K4, O4), so it is not "all the fields of the database".

**Checked against the frozen contracts:** field names, ranges, steps and the `ui_line` advisory (1.3) match `contracts/config.py` and K2. The
three built-in intents and their resolved values match the K2 table in `contracts/presets.py`.

**Answers O1 gives to my open decisions**
* **D3 (revised):** `custom` is a real, persisted row — the user's working set, same six fields, written only once the user has values, never seeded
  with defaults. `original` is never stored. Built-ins are code (`config.BUILTINS`). → `presets.kind CHECK (kind IN ('user','custom'))`; `custom` uses the
  reserved id `custom` (one row); `save()` of a built-in or of `original` is refused; user ids are `uuid4`. Reserved ids: `default`, `reading`,
  `high-separation`, `custom`, `original`.
* **Resolved configs next to presets:** do not persist them. Recompute with `resolve()` on read (O1's own regeneration rule, §8.2.5, then never applies).
  Stored resolved configs in `font_registry` / `active_config` are history and stay as written (K9 "Presets vs active").
* **Candidate presets (Table C):** keep out of the database; K2 is frozen at three built-ins.

**Conflicts / new findings**
* **F6 — real bug in the K9 rule "same key, different config → `FONT_IDENTITY_CONFLICT`".** The M1.5 key deliberately **excludes `text_scale`**
  (card acceptance test: "`text_scale` never changes the key"). Two registry entries with the same key but different `text_scale` would be a false conflict.
  Proposed **D8:** compare only the font-building fields (`base_family`, `letter_em`, `word_scale`, `reading_line`, `ui_line`) plus files and hashes; or store only
  those fields in `font_registry.config`.
* **Storage format:** O1 proposes typed columns with decimal types; K9 says versioned JSON. SQLite has **no decimal type** (NUMERIC stores binary floats), so
  "use decimal so equality works" cannot be honoured literally. Proposed **D9:** keep K9's JSON (`{"v":1,…}`) with fixed-decimal strings, validate with `validate()`
  (O1's rule 7), and add only cheap CHECKs (`kind`, `word_mode`). Alternative: integers in hundredths — your call.
* **`word` mode + value** must be stored together (confirmed; already inside the intent JSON in my draft).
* **Unreproduced contract differences (check on her branch):** she lists error code `NOT_A_NUMBER`, which is **not** in `contracts/config.py` `IssueCode`
  (contract has `NON_FINITE`, `OUT_OF_RANGE`, `UNKNOWN_FONT`, `NOT_ON_STEP`, `UI_LINE_ABOVE_RECOMMENDED`); and she names `config.settings.Configuration` /
  `config.presets.resolve`, while the contract types live in `scribesense.contracts.config` / `contracts.presets`. If she defined a second `Configuration`
  or added a code, that is a contract change (AGENTS.md §4) and needs a decision entry.
* **Provisional input:** `target_em` presets resolve through a provisional 0.25 em space width (Default flips between 1.5 and 1.75 near 0.256 em).
  Nothing stored in the store may depend on that being final; another reason not to persist resolved configs beside presets.

---

## 9. O1's reply to D3 and D8 (2026-10-18; O1 states both as confirmed; checked by O3 against `origin/O1`)

* **D8 — O1: "confirmed, but drop base_family". ADOPTED and built (2026-10-18).** O1's `UNKEYED_FIELDS = ("text_scale", "base_family")`: `base_family` is covered by the
  source hash, so renaming a shortlist entry with identical bytes would have raised a false `FONT_IDENTITY_CONFLICT`. The registry now compares `letter_em`, `word_scale`,
  `reading_line`, `ui_line` plus families, styles, files and hashes; `base_family` stays in the row for display and a changed label is noted in the log (code-only).
  O1 also suggests comparing `generator_version` and `source_sha`; both are inside the key, and K9's `FontRegistryEntry` has neither field, so equal keys already imply them.
  A preset still counts as referencing a font only when its `base_family` matches too (a different family selects different bytes).
* **D3 — CONFIRMED by O1 (2026-10-18), after the question "is `custom` stored as one row, and does `list()` include it?".** Answer: **yes, one row; no, `list()` does not include it** ("stored ≠ listed"; O1's
  `config/presets.py` states it twice, and it matches K9's own "built-ins first, then user presets by name"). The earlier reply that described `custom` as unsaved was the first D3 draft, not O1's position. **O1's trap:** the font
  clean-up (K7 rule 3: "a saved preset" includes Custom) needs every stored preset; a caller that pipes `list()` into it would delete the font of the user's own current configuration. M3.1 adds `PresetRepo.list_all()` for that
  (built-ins, custom, user presets; an addition to the frozen K9 protocol — to be written into the contract text by the K9 owner) and uses it itself (regression test). `list()` was changed in two steps: step 1 `list_all()`; step 2 (done 2026-10-18, after O4 moved the clean-up in `controller/service.py` to `list_all`) `list()` drops the custom row — read it with `get("custom")`.
* The reply also suggests persisting "the user's working configuration" as its own row. K9 already has `active_config` (the confirmed, applied configuration); the
  unapplied `custom` working set is a different thing — to be settled together with D3.
* Other points from the reply: uninstall removes fonts only through `fontgen.cache` (`reconcile()`, then `installed_keys()` + `remove(key)`); `ReferenceSource` is not an O1 task
  (O4 has since built it); bundled fonts will change every content key once (re-resolve rows, do not migrate keys).
