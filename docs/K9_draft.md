# K9 store — working draft (O3)

**Status: PROPOSAL. Not signed off. Nothing here is built.**
`contracts/store.py` is frozen text and is **not** edited by this draft (AGENTS.md §4). `store/` is **not** built (K9 is a draft
until O3 signs off). When you sign off, the accepted decisions go into `ARCHITECTURE.md` (K9 section) and `PLAN_v2.md`; any item
marked **needs O4 / O1** needs that owner before it can be accepted.

Date: 2026-10-14 · Sources: ARCHITECTURE.md K4, K9, M3.1, M3.2, R2-1…R2-6; contracts `store.py`, `journal.py`, `adapter.py`, `presets.py`.

---

## 1. Decisions to make (recommendation first)

| # | Question | Recommendation | Who must agree |
|---|---|---|---|
| D1 | `JournalItem.intent` is a `PlannedWrite` (has `payload`) but K9 stores only `intent_sha256`. | Store the payload (`TEXT` for keys, `BLOB` for files). No contract change. Recovery only needs the hash, but the K4 type promises the payload. Payloads are tool-config bytes (fontconfig / CSS / gsettings text), never reader text. | O4 (confirms nothing in K4 expects less) |
| D2 | `Target` has 4 fields; `running` is transient. | Persist `adapter`, `target_id`, `display_name` as three columns. On read set `running=False` (stale by definition; callers re-detect). Never store window titles. | O4 |
| D3 | `Preset.kind` is `"builtin" \| "user" \| "custom"`; K9 only says "built-in → `PRESET_READ_ONLY`". | Built-ins are **not stored**: `PresetRepo.list()` merges `config.BUILTINS` (needs `store` → `config` import; allowed, the reverse is forbidden). Only `"user"` is persisted. `save()` of `"custom"` → `ValueError` (it is unsaved editing state). | **O1** (meaning of `"custom"`) |
| D4 | Retention wording differs: K9 "outside the newest `keep_last`" vs M3.2 "30 days / last 10". | A terminal tx is deleted only if **older than the cutoff AND outside the newest `keep_last` terminal txs AND not referenced** (i.e. kept if recent *or* among the last N). Plus a new rule: **always keep the newest `COMPLETED` apply** (see §5, finding F2). | O4 (they implement the journal API) |
| D5 | Who writes SQL for the journal tables? Import rule R3: **only `store/` imports `sqlite3`**, so `safety/` (O4's M4.10 journal) cannot. | `store/` provides the SQL layer for the journal tables (a private `store/journal_tables.py`, written by O3 to O4's K4 spec). O4's `safety/journal.py` calls it. Alternative: relax R3 for one file — rejected, it weakens the one rule that keeps SQLite in one place. | **O4** (this changes who writes what in M4.10) |
| D6 | The DB file name is not defined (K8 has `data_dir()` but no DB path). | `store.default_path() = data_dir() / "scribesense.db"`; `Store(path)` stays as in K9. No K8 change. | O3 only |
| D7 | `location` / `target_id` strings go into the store. AGENTS.md §6 forbids user file paths. | Policy: only **tool-config locations ScribeSense itself manages** (fontconfig rule file, gsettings `schema key`, browser pref file) — never user documents. Add a test that every stored `location` matches an adapter-declared location pattern. | O4 + team |

---

## 2. Proposed schema (migration `001_initial.sql`) — draft

Checked 2026-10-14 in a throwaway in-memory SQLite: the script parses; the CHECK / FK constraints reject a hash without payload, two payloads, `retained` without an applied hash, a second `active_config` row, `config` without `font_key`, deleting a referenced backup, and a stored built-in preset. It is **not** a migration file yet and has no repository code behind it.

`PRAGMA user_version = 1` is set in the same transaction. All timestamps UTC ISO-8601 with offset. All JSON is `{"v":1,…}`.
Per connection (not in the SQL file): WAL, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`.

```sql
-- ---- preferences (deletable by delete_preferences) ----
CREATE TABLE presets (
  id         TEXT PRIMARY KEY,                      -- str(uuid4()); built-ins are NOT stored (D3)
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind = 'user'),
  data       TEXT NOT NULL,                         -- Preset values, JSON v1
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
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
  config         TEXT NOT NULL,                     -- Configuration JSON v1
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

## 6. Acceptance tests → where they would live (once signed off)

| K9 test | Proposed file | Level |
|---|---|---|
| 1 round-trip, 2 built-in read-only, 3 settings validation, 4 identity conflict, 5 interrupted build, 6 `FONT_IN_USE` | `tests/unit/test_store_repos.py` | unit (temp file DB, real sqlite3) |
| 8 migrations (empty, each previous version, failure leaves `user_version`), 9 recovery mode | `tests/unit/test_store_migrations.py` | unit |
| 10 prune, 11 `delete_recovery_data` refusal, **F2, F3, F4** | `tests/unit/test_store_journal_tables.py`, `test_retention.py` | unit |
| 12 opt-out + canary SELECT sweep (every table, every text column) | `tests/unit/test_store_privacy.py` | unit |
| 7 two processes | `tests/integration/test_store_concurrency.py` | bubblewrap |

Unit tests may use `sqlite3` on a real temp file (no process is started, so the sandbox guard allows it); the import-rule test
already restricts `sqlite3` to `store/`.

---

## 7. Sign-off checklist (K9, from ARCHITECTURE.md) — all open

- [ ] Column types (§2 reviewed)
- [ ] Matches the planned implementation (D1–D7 decided)
- [ ] The 12 acceptance tests accepted (plus F2, F3, F4)
- [ ] Journal tables return everything in `JournalItem` / `LocationRecord` (§3, with O4's answer to D1/D2/D5)
