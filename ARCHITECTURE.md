# ScribeSense — Architecture & Module Specification (C4 + C5)

Built only from decisions recorded in `PLAN_v2.md`. Replaces the earlier untrusted draft.
Language: **Python** for everything except the browser extension (**JavaScript**).

## 0. How to read this document

**Status tags (on every non-obvious statement):**

| Tag | Meaning |
|---|---|
| **[D]** | Decided by the team — source in `PLAN_v2.md` |
| **[DR]** | Derived — follows directly from a decision |
| **[P]** | Proposed, never discussed — used as the default until the team decides (see §3) |
| **[V]** | Must be verified by a quick test before relying on it |
| **[O]** | Open — must be decided before the affected card is built (see §3) |

**IDs:** contracts are `K1…K11`, modules are `M<owner>.<n>`, open items are `A1…A20`.

**Module card fields (16, decided):** Purpose · Owner / may be helped? · Built by / reviewed by ·
Depends on · Used by · Provides · Exact interface · Example · Data it reads/writes · Data
ownership · Preconditions · Postconditions · Failure cases + required behaviour · Must never ·
Acceptance tests · Done when.

**Interfaces** are written as Python-style signatures. They are **[P]** until the contract
session freezes them (§9 step 1). After the freeze, changes need O4 + one other owner's approval
for K3/K4, and the contract owner + one other owner for the rest.

---

## 1. Scope (decided)

- System-wide reading tool for **Hyprland on Linux**, **English only**, single user. [D]
- User chooses a **preset** and adjusts it; A/B calibration dropped. [D]
- Spacing is carried by **generated fonts**: letter + word spacing in the glyphs, line height in
  the font metrics; text size via `text-scaling-factor`. [D]
- Two variants per configuration: **reading** (full line height) and **UI** (own line-height
  setting, recommended 1.2–1.3×, warning above). [D]
- Never patched: **monospace**, **icon** fonts; only **Latin** glyphs get spacing. [D]
- Targets in the MVP: fontconfig, GTK, Flatpak (per app), Qt (only via an existing qt6ct
  session), Firefox family, Chromium family (+ our extension). [D]
- **Fallback reader** for anything not covered; four ways in. [D]
- Every change is **journaled and reversible**; 30 s keep-or-revert; auto-revert at login;
  recovery keybind; uninstall reverts everything. [D]
- **Later (not in MVP):** shadow mode, drift detection, Electron, GNOME, recommender, voice. [D]
- Deadline 20 October; mentor to be consulted on the pivot. [D]

---

## 2. System overview

### 2.1 Components and owners

| Area | Modules | Owner |
|---|---|---|
| Configuration & fonts | M1.1–M1.8 | O1 |
| Apply flow, UI, reader, CLI | M2.1–M2.11 | O2 |
| Store, tests, VM, doctor, install | M3.1–M3.9 | O3 |
| Adapters, browser layer, safety | M4.1–M4.15 | O4 (helpers on marked cards) |

### 2.2 The apply sequence (who does each step)

| # | Step | Module | Contract used |
|---|---|---|---|
| 1 | User picks / adjusts a preset, sees preview | M2.5 | K1, K2 |
| 2 | Configuration validated + rounded | M1.1 | K1 |
| 3 | Fonts built or taken from cache | M1.8 → M1.4–M1.7 | K7 |
| 4 | Preflight checks | M2.1 | K3, K4, K8 |
| 5 | Transaction opened (lock) | M4.10 | K4, K5 |
| 6 | Targets detected | M4.x adapters | K3 |
| 7 | Snapshot of every target recorded **before** writing | M4.x → M4.10 | K3, K4 |
| 8 | fontconfig applied first; failure → stop + revert | M4.2 | K3 |
| 9 | Other adapters applied; failure → that app NOT_COVERED | M4.3–M4.7 | K3, K6 |
| 10 | Verify each target; failed verify → revert that adapter only | M4.13 + adapters | K3 |
| 11 | Restart notice for running affected apps | M2.2 | K6 |
| 12 | ScribeSense relaunches its own window with new fonts; 30 s countdown | M2.3 | K5 |
| 13 | Keep → `Journal.keep()` (atomic) → COMPLETED · timeout/close → revert | M2.3 → M4.10 / M4.11 | K4, K5 |
| 14 | Coverage report shown | M2.6 | K6 |

Preset switching by keybind runs the **same sequence**. [D]
"Original" = revert everything. [D]

### 2.3 Outside the apply sequence

| Event | Module |
|---|---|
| Login with an unfinished transaction → auto-revert | M4.12 |
| Recovery keybind / `reset` command | M4.12 |
| Text captured → fallback reader | M2.10 → M2.9 |
| Uninstall | M3.8 → M4.11 |
| Retention pruning | M3.2 |

---

## 3. Architecture decisions A1–A20 — DECIDED (2026-10-08)

All twenty were reviewed and decided by the team (proposals + six amendments). Tagged **[D Ax]** below.

| # | Decision | Affects |
|---|---|---|
| A1 | **Single-instance background service.** Closing the window doesn't quit; explicit Quit (UI + `scribesense quit`) exits safely; further launches forward to the running instance — **except `confirm`** (A15). | M2.4, M2.9, M2.11 |
| A2 | **Recovery works without the GUI** and without loading the newly generated font. `reset`, `recover --login`, `uninstall`, `doctor` import no UI modules (CI-enforced, M3.9). | M4.12, M3.8, M3.9 |
| A3 | **Login recovery via Hyprland `exec-once` → `scribesense recover --login`.** Reverts only unfinished/unconfirmed transactions, never a confirmed configuration; idempotent; completes before any new Apply (journal lock). [V] test interrupted tx + startup ordering in the VM. | M4.12, M3.8 |
| A4 | **Libraries:** fontTools · PyGObject (GTK4 + libadwaita, from system packages, not pip) · sqlite3 (stdlib) · pytest. No other dependency without team approval. Extension: plain JavaScript, no libraries. | all |
| A5 | **Interactive preview before Apply:** shortlist dropdown, built-in sample passage, optional user-entered text (memory only, never logged), then adjust spacing. Options described as choices, not clinical recommendations. Approach: [V] load the generated font into the preview only; fallback: rendered image, with accessible text and controls kept. | M2.5 |
| A6 | **Manifest V3 + native messaging**, Chromium family only. Path *extension → native-messaging host → running instance* verified early. Unsupported browser / host missing / app stopped → clear failure. [V] Flatpak browsers: feasibility test, not assumed. | M4.9, M2.10, K11 |
| A7 | **O2 owns CLI dispatch** (K10). O4 provides the recovery and safe-writing operations the commands call. CLI and GUI share the same application logic. | M2.11 |
| A8 | **Tables of M3.1 adopted as the starting schema.** Relationships, validation, deletion rules and transaction boundaries defined before implementation. Recommendation tables optional — core works without them. | M3.1 |
| A9 | **Word spacing: preset stores intent, configuration stores the resolved value.** Default preset = target "+0.16 em", converted at resolve time using the base font's **Regular** space width, rounded to the 0.25 step, validated, labelled "≈". Modes: `target_em` (recalculate on font change) vs `multiplier` (keep the user's explicit value). See K2. | M1.1, M1.2, K2 |
| A10 | **Every resolved configuration has all six fields.** Built-ins define all six (base Noto Sans · UI line 1.2 · text scale 1.0, plus K2 table values). Edits keep untouched fields. "Original" (restore pre-ScribeSense state) is not a ScribeSense default. | M1.2, K2 |
| A11 | **`text_scale` 1.0–2.0, step 0.05**, one shared range (K1 `RANGES`). Apps that ignore it (e.g. Qt) are documented, not promised. | M1.1, M4.3 |
| A12 | **`ui_line` 1.0–2.0, step 0.1**, warning above 1.3×. Upper limit revisited only after clipping tests; any change updates `RANGES` + tests. | M1.1 |
| A13 | **Explicit routing, no guessing of text purpose:** identified UI settings (gsettings `font-name`, qt6ct general font, named UI fonts) → UI variant; generic families elsewhere → reading variant. A heuristic; exceptions tested and documented. | M4.2, M4.3, M4.5 |
| A14 | **Two verification levels:** configuration check (resolves to our family) in production; rendering check (actually drawn) in the test harness, plus Pango in production. A passing configuration check is never reported as proof that every running app draws the font. | M4.13, M3.4, M2.6 |
| A15 | **Independent watchdog until the transaction ends.** The background service applies, then launches a **fresh process** `scribesense confirm <tx>` (bypasses single-instance forwarding). The service stays watchdog until COMPLETED or REVERTED; competing rollbacks prevented by the journal lock; a crashed watchdog is covered by login recovery (A3). Details in M2.3. | M2.3, M4.11, K10 |
| A16 | **Chromium extension asks for the current *confirmed* style** and refreshes after Apply, Revert and Original. Preview values are never exposed. Firefox/Zen CSS is a file updated by the adapter on apply/revert (browser restart needed). | M4.9, M4.8, M4.6, K11 |
| A17 | **Reader limit 200 000 characters**, explicit rejection, never silent truncation; byte limits also enforced at transport boundaries (native messaging). Provisional — revisit after measuring. | M2.10 |
| A18 | **O4 owns the Hyprland keybind/config writer:** preserves unrelated config, no duplicates, safe restoration, Lua and classic formats. UI and CLI call it. | M4.14 |
| A19 | **Names `AccessSans-<key>` (reading) / `AccessSansUI-<key>` (UI).** Key includes **every input that changes the generated files** — see M1.5. | M1.5 |
| A20 | **Python ≥ 3.11 declared.** Core (non-GTK) modules tested on 3.11 and the dev version; desktop app validated on the system Python + installed GTK/PyGObject (3.14 on the dev machine). Full-app 3.11 support is **not** claimed. | M3.7, M3.9 |

### Browser support boundary [D A6, A16]

| Browser route | Page styling | "Open in ScribeSense" |
|---|---|---|
| Chromium family (Brave, Chrome, Chromium) | Preferences adapter + our extension | Extension right-click |
| Firefox / Zen | `userContent.css` via the Firefox adapter | Selection keybind (Super+Alt+R), where capture is verified |
| Any | — | Copy / paste into the reader always available |

### Recovery review R2 — decisions (2026-10-09)

| # | Decision | Where |
|---|---|---|
| R2-1 | **Permanent per-location record** (`LocationRecord`): baseline + restore metadata + backup ref, `managed` = last *kept* state. Recovery: finish unfinished txs first, then restore baselines. Successful reset clears `managed`. | K4, K9, M3.2 |
| R2-2 | **`CONFIRMED` removed.** Keep = one atomic `Journal.keep()`: tx COMPLETED + retained managed records + active config + style revision; acknowledged after commit. | K4, K5, M2.3 |
| R2-3 | **Lock owners named** (service / reset / recover / uninstall). Other recoveries are never interrupted: 60 s bounded wait → `LOCK_TIMEOUT`. Service takeover via pidfd + process group. | K4, K8 |
| R2-4 | **Enforced test isolation:** fake adapters for unit tests; bubblewrap for integration (no host sockets, fail closed); VM for desktop tests. | M3.3, §4 |
| R2-5 | **Extension:** keeps styling when disconnected, reconciles on reconnect, local Disable, revision on every reply, app supplies the CSS. | K11, M4.9 |
| R2-6 | **Durability** promise narrowed to recovery records on storage that honors sync; WAL + `synchronous=FULL`; fsync sequence. | §4, K9 |
| R2-7 | **Partial font styles allowed**; no promise about synthesized styles in other apps. | K7, M1.5 |
| R2-8 | **Reading settings [D 2026-10-12 — Gio]:** `read_state()` for keys uses **`Gio.Settings.get_user_value()`** (returns nothing when the key is unset; follows the active `GSETTINGS_BACKEND`, so the test sandbox and production behave the same). `gi.repository.Gio` is **allowed** in the recovery path — only `Gtk`/`Adw` are banned (A2, import rule R6). Writes use `gsettings set` (type-checked) and `gsettings reset` for unset. `dconf read` is **not** used. | K3, M4.3 |
| R2-9 | **Uninstall and fonts [D 2026-10-12 — yes; reverses PLAN F10]:** fonts still referenced by skipped/unresolved settings, or whose use can't be determined, are **kept by default**; `uninstall --remove-fonts-anyway` removes them. Wording: "these apps may fall back to another font and their appearance may change." | M3.8, K10 |

---

## 4. Conventions (apply to every module)

| Topic | Rule |
|---|---|
| Paths | Only from K8. Tests redirect them by environment variables, never by editing real files. [DR] |
| Data location | `~/.local/share/scribesense/` (store, backups) [D] · fonts in `~/.local/share/fonts/scribesense/<key>/` [D] |
| Text privacy | Reader/document text is never written to disk, logged or stored. [D] |
| Logs | Error **codes** and categories only (K8), never content, titles or user file paths. [D] |
| Network | None. No server, CDN or remote fonts. [D] |
| Other apps | Never closed or restarted by ScribeSense; the user is asked. [D] |
| Session | Never changed globally for coverage (e.g. no setting `QT_QPA_PLATFORMTHEME`). [D] |
| Writes | No write to any target without a journaled snapshot first. [D] |
| Revert | A value the user changed after we wrote it is skipped and reported, never overwritten. [D] |
| Testing | No test touches the real desktop — **enforced**, not by convention: unit tests use fake adapters with `subprocess` blocked; integration tests run inside a bubblewrap sandbox (M3.3); desktop/compositor/Flatpak tests run only in the VM (M3.5). [D R2] |
| Durability | "ScribeSense durably records recovery information before applying changes and supports recovery after process crashes or power loss on supported local storage that honors synchronization requests. Changes across external settings services are not one atomic transaction." SQLite WAL + `synchronous=FULL`; files: write temp → `fsync` file → `os.replace` → `fsync` directory (also for deletions and directory installs); a failed sync is an error (`WRITE_NOT_DURABLE`). Backups + intent are durable before a target is modified; generated files are durable before anything references them. [D R2] |
| Backups privacy | Backups may contain unrelated app configuration (e.g. a whole browser `Preferences` file). Stored only in `data_dir()` (mode `0700`/`0600`), never exported or logged, removed by "Delete all ScribeSense data". Separate from optional recommendation samples. [D R2] |

---

## 5. Shared contracts

### K1 — Configuration (owner O1) — FROZEN 2026-10-09

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
| `UI_LINE_ABOVE_RECOMMENDED` | warning | `ui_line` > 1.3 |

5. The UI may tell the user a value was rounded; that is UI feedback, not an `Issue`.

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

### K3 — Adapter (owner O4, O4-only) — FROZEN 2026-10-09 · amended R2 2026-10-09

```python
@dataclass(frozen=True)
class Target:
    adapter: str          # "fontconfig", "gtk", "flatpak", "qt", "firefox", "chromium"
    target_id: str        # e.g. "flatpak:app.zen_browser.zen", "brave:Default"
    display_name: str     # shown in the coverage report
    running: bool         # needed for must_be_closed and the restart notice

@dataclass(frozen=True)
class State:                  # canonical state of one location [D R2]
    kind: Literal["file", "key"]
    present: bool             # file: exists · key: explicitly set by the user (NOT "has a schema default")
    data: bytes | None        # file: exact bytes (b"" = present but empty) · key: GVariant text, e.g. b"'Noto Sans 11'"
    value_type: str | None    # key only: GVariant type string, e.g. "s", "d"
    sha256: str               # sha256(b"absent") if not present, else sha256(b"present\0" + data)

@dataclass(frozen=True)
class SnapshotItem:
    target: Target
    kind: Literal["file", "key"]
    location: str         # file path, or "<schema> <key>" for settings
    existed: bool         # = state.present
    old_value: str | None # key: GVariant text; files: None (bytes kept as backup, K4)
    old_sha256: str       # = state.sha256 (also defined when absent)
    file_mode: int | None # file permissions to restore (files only)
    value_type: str | None

@dataclass(frozen=True)
class PlannedWrite:           # computed before anything is written (write-ahead intent) [D]
    target: Target
    location: str
    payload: bytes | str      # exact file bytes, or the key value
    new_sha256: str           # hash of payload

@dataclass(frozen=True)
class AppliedItem:
    target: Target
    location: str
    new_sha256: str       # what we wrote — used later for the drift rule

@dataclass(frozen=True)
class VerifyResult:
    target: Target
    chosen_ok: bool       # the app/config resolves to our font
    drawn_ok: bool | None # None = no drawn check for this target [D A14]
    detail_code: str

class Adapter(Protocol):
    name: str
    requires_restart: bool
    must_be_closed: bool
    def detect(self) -> list[Target]: ...
    def read_state(self, location: str) -> State: ...                   # canonical, read-only [D R2]
    def snapshot(self, target: Target) -> list[SnapshotItem]: ...        # reads only
    def plan(self, target: Target, fonts: FontSet,
             config: Configuration) -> list[PlannedWrite]: ...        # read-only; exact new values
    def apply(self, planned: PlannedWrite) -> AppliedItem: ...           # writes exactly this, nothing else
    def verify(self, target: Target, fonts: FontSet) -> VerifyResult: ...
    def revert(self, item: SnapshotItem, backup: bytes | None) -> None: ...  # restore one item [D]
```

**Rules every adapter follows [D/DR]:**
1. `snapshot` writes nothing; its items are journaled **before** `apply` is called.
2. `plan` is read-only. `apply` writes exactly one `PlannedWrite`, only to a location that `snapshot` returned for that target. The controller journals the intent between `plan` and `apply`.
3. The drift check is **not** done by adapters — M4.11 compares the current value with
   `AppliedItem.new_sha256` before calling `revert`.
4. All paths come from K8.
5. Errors are raised as `ScribeSenseError(code)` (K8); adapters never show UI and never return
   `None` to signal failure. The **controller** maps errors to coverage (fontconfig failure → stop
   + revert all; other adapters per the M2.1 rules) — an adapter never decides coverage. [D]
6. **`detect()` and `snapshot()` are read-only.** [D]
7. **Revert restores the original state, never a new one** [D]:
   - file existed → write `backup` back (missing backup → `ScribeSenseError("BACKUP_MISSING")`);
   - file did not exist → delete the file ScribeSense created;
   - key → write `old_value`; key was unset → `gsettings reset` (or the API's equivalent unset).
8. **Atomic file writes** [D]: temp file in the same directory → copy the original's permissions
   → atomic replace (`os.replace`). Settings APIs (gsettings) use their own write call and are
   read back to confirm.
9. **Timeouts per operation** [D]: every external command runs through the shared helper
   `run_cmd(argv, timeout)` (K8) — never `subprocess` directly. Default deadlines, overridable in the
   adapter's `TIMEOUTS: dict[str, float]` on its card: `gsettings`/`dconf` 5 s · `fc-match` 10 s ·
   `flatpak run … fc-match` 20 s · `fc-cache` 60 s · other 10 s. On timeout: terminate → kill after
   2 s → reap → `ADAPTER_TIMEOUT`. Non-zero exit → `ADAPTER_COMMAND_FAILED`. [D R2]
10. **Location format** [D]: files = absolute path; settings = `"<schema> <key>"`, e.g.
    `"org.gnome.desktop.interface font-name"` — identical in snapshot, applied item and revert.
    **10b (amended 2026-10-12, decided by O4 — second owner sign-off pending):** a single value inside a JSON
    file that its application rewrites on every run (Chromium `Preferences`) is its own location of kind
    `"key"`: `"<absolute file path>#<dotted.key.path>"`, e.g. `".../Default/Preferences#webkit.webprefs.fonts.standard.Zyyy"`.
    `State` and every hash cover **only that value** (present = the key exists; data = its canonical JSON
    text), so the browser rewriting the rest of the file is **not** drift and reset/uninstall can still undo
    our change. Revert writes back the old value, or deletes the key if it was absent (never the whole file).
11. **Canonical state [D R2]:** every comparison (snapshot, intent, drift, revert) uses `State.sha256`
    from `read_state()` — never ad-hoc strings. For settings, "present" means **explicitly set by the
    user**, so an unset key is restored by unsetting it, never by writing its default.
    How a key is read is decision 8 (see §3 R2 table). The *effective* value an app will use is
    checked separately, in `verify()`.
12. **Changes by other programs [D R2]:** immediately before `apply`, the adapter re-reads the state;
    if it no longer equals the snapshot → `CHANGED_DURING_APPLY`, nothing written for that target.
    This is **best-effort race detection**, not an atomic compare-and-write; the remaining window is documented.
13. **One owner per location [D R2]:** each location belongs to exactly one target. The controller
    rejects a plan where two targets would write the same location (`PLAN_CONFLICT`).

### K4 — Journal (owner O4, O4-only) — FROZEN 2026-10-09 · amended R2 2026-10-09

```python
@dataclass(frozen=True)
class JournalItem:
    item_id: ItemId
    snapshot: SnapshotItem              # incl. existed / old_value / old_sha256
    intent: PlannedWrite | None         # None → no write was ever planned for this item
    applied: AppliedItem | None         # None → write not confirmed (may or may not have happened)
    revert_outcome: Literal["reverted", "skipped_drift", "failed"] | None
    retained: bool                      # True only if this write passed verify and is still applied

@dataclass(frozen=True)
class LocationRecord:                   # PERMANENT, one per location — not transaction history [D R2]
    location: str
    target: Target
    baseline: SnapshotItem              # state before ScribeSense first managed it (incl. file_mode, value_type)
    baseline_backup_ref: str | None     # reference to the stored original bytes (files)
    managed_sha256: str | None          # last successfully KEPT ScribeSense state; None = not managed
    managed_font_key: str | None        # generated font that managed value refers to
    unresolved: ErrorCode | None        # set when a reset/revert of this location failed or was drift-skipped

@dataclass(frozen=True)
class ActiveConfig:                     # what is actually confirmed — not a preset ID [D R2]
    config: Configuration
    font_key: str
    source_preset_id: str | None        # informational only
    style_revision: int

class Journal:
    def begin(self, kind: Literal["apply", "revert", "uninstall"],
              preset_id: str | None, reverts: TxId | Literal["all"] | None = None) -> TxId
        # takes the lock; raises APPLY_IN_PROGRESS if held. `reverts` = what a revert/uninstall tx undoes
    def set_state(self, tx: TxId, state: TxState) -> None        # only K5 transitions
    def record_snapshot(self, tx: TxId, item: SnapshotItem,
                        file_backup: bytes | None) -> ItemId      # stores baseline if first ever for location
    def record_intent(self, tx: TxId, item_id: ItemId, planned: PlannedWrite) -> None  # BEFORE the write
    def record_applied(self, tx: TxId, item_id: ItemId, item: AppliedItem) -> None      # after the write
    def record_revert(self, tx: TxId, item_id: ItemId,
                      outcome: Literal["reverted", "skipped_drift", "failed"]) -> None
    def record_coverage(self, tx: TxId, entry: CoverageEntry) -> None
    def state(self, tx: TxId) -> TxState
    def pending_confirmation(self) -> TxId | None                 # the tx in AWAITING_CONFIRMATION
    def unfinished(self) -> list[TxId]                            # every non-terminal state incl. REVERT_FAILED
    def items(self, tx: TxId) -> list[JournalItem]
    def backup(self, tx: TxId, item_id: ItemId) -> bytes | None
    def baseline(self, location: str) -> SnapshotItem | None
    def baseline_backup(self, location: str) -> bytes | None   # original bytes; raises BACKUP_MISSING / BACKUP_CORRUPT
    def location(self, location: str) -> LocationRecord | None
    def locations(self) -> list[LocationRecord]
    def mark_retained(self, tx: TxId, item_id: ItemId) -> None  # write passed verify and is still applied
    def keep(self, tx: TxId, active: ActiveConfig) -> int
        # ONE database transaction: tx AWAITING_CONFIRMATION → COMPLETED · managed_sha256/font_key updated
        # for RETAINED items only · active config saved · style_revision incremented. Returns the revision.
    def clear_managed(self, location: str) -> None              # after a successful reset of that location
    def set_unresolved(self, location: str, code: ErrorCode | None) -> None
    def active(self) -> ActiveConfig | None                     # None = Original
    def set_original(self) -> int                               # active = None, style_revision += 1
    def prune_history(self, before: datetime, keep_last: int) -> int   # [K9] see K9 pruning rule
    def no_obligations(self) -> NoRecoveryObligations | None
        # token only when nothing is unfinished, unresolved or managed; required by Store.delete_recovery_data()
    # record_coverage() is the ONLY writer of coverage (K9 CoverageRepo is read-only)
```

**Rules [D 2026-10-09]:**
1. **Lock [D R2]:** `lock_path()` (K8) via `fcntl.flock`. The lock file is **stable** — never unlinked or
   replaced while used. After acquiring, the holder writes an **owner record** into it:
   `{"role": "service"|"reset"|"recover"|"uninstall", "pid": int, "start_time": int}` — identification
   only; the flock is the lock. Held from `begin()` until the tx reaches a terminal state, **including the
   whole confirmation period** and every failure path. Released by the OS on crash.
   (The single-instance mechanism for the app window is a separate concept.)
2. **Durability:** every `record_*` commits before returning. Snapshot, backup **and intent**
   are committed before the adapter writes anything. Per item the order is:
   `snapshot → record_snapshot → plan → record_intent → apply → record_applied → verify`.
3. **Keep [D R2]:** the `confirm` window sends Keep/Revert to the service over IPC (rule 5). The
   service checks the tx ID and calls `keep()` — **one atomic database transaction** — and only after
   it commits does it acknowledge Keep to the window. Crash before the commit → the tx is still
   AWAITING_CONFIRMATION → recovery reverts it. `managed` changes **only for retained writes**;
   skipped, failed and rolled-back locations keep their previous `managed`. The active configuration is
   the confirmed configuration; coverage per app is reported separately (K6).
4. **Recovery order and comparison rules [D R2]** (all under the lock):
   **Step 1 — finish unfinished txs** (`unfinished()`), per item, comparing `read_state()`:
   - equals the tx snapshot (`old_sha256`) → `reverted`, write nothing;
   - `intent` is None → nothing planned → `reverted`;
   - equals `intent.new_sha256` (with or without `applied`) → ScribeSense wrote it → restore the snapshot;
   - else → `skipped_drift`.
   **Step 2 — only for reset / uninstall: restore baselines**, per `LocationRecord`:
   - equals the baseline → already original → `clear_managed`;
   - equals `managed_sha256` → ours → restore baseline (`baseline_backup`, delete, or unset) → `clear_managed`;
   - `managed_sha256` is None → not ours → leave it;
   - else → user change → `skipped_drift`, `set_unresolved(DRIFT_SKIPPED)`.
   - A failed item keeps all its recovery information and is reported unresolved.
   - Restore distinguishes: **originally absent** (`existed=False` → delete / unset) · **present but empty**
     (backup = `b""`, valid) · **backup missing or corrupt** → `BACKUP_MISSING` / `BACKUP_CORRUPT`
     (an error — never treated as "originally absent").
   - After a full reset: `set_original()`. The report lists every unresolved location, so "Original" never
     implies everything was restored.
5. **IPC [D 2026-10-09; location amended 2026-10-12]:** a Unix socket at `socket_path()` (K8) =
   `$XDG_RUNTIME_DIR/scribesense/service.sock`, stdlib only (no GTK), used by
   `confirm`, `reset` and the native-messaging host.
   - Same-user only: socket file mode `0600` in a `0700` directory; the service checks the peer
     UID (`SO_PEERCRED`) and drops other users.
   - One JSON object per line, each message ≤ 1 MiB; anything else → `BAD_MESSAGE`.
   - The socket is only for talking to a **running** service. When no service holds the lock,
     `reset` and `recover --login` take the lock and recover **directly** (A2) — they never need
     the socket.
6. **Reset when the lock is held [D R2]** — depends on the owner record:
   - **owner = service:** ask it over IPC to reset. The **5 s is a reply deadline**, not a rollback
     deadline: a responsive service acknowledges, then performs the bounded recovery itself. No reply
     → takeover: verify pid + start time, signal **the service process** via `pidfd`
     (`os.pidfd_open` + `signal.pidfd_send_signal`; a pidfd identifies one process only), then terminate
     its **process group** (the service is a session leader started with `setsid`; every child runs
     through `run_cmd()` in that group): SIGTERM → 2 s → SIGKILL → wait until no process in the group
     remains (bounded, 10 s) before recovering. Children never inherit the lock (close-on-exec).
   - **owner = reset / recover / uninstall:** **never interrupted.** Wait for the lock with bounded
     polling (default **60 s**, configurable) and print factual status. Timeout → exit 3 (`LOCK_TIMEOUT`),
     nothing killed.
   - After acquiring the lock: **re-read the journal** and run rule 4 — never assume the other process
     already did what this command needed.

- **Baseline rule [D R2]:** the first snapshot ever taken of a location becomes its `LocationRecord`
  baseline, with its bytes and restore metadata. **Never removed by automatic history retention.**
- **Retention [D R2]:** M3.2 prunes **transaction history only** (completed txs after 30 days, always
  keeping the last 10). `LocationRecord`s and baseline backups are never pruned automatically.
  Explicit "Delete all ScribeSense data" first offers a reset; if the user declines, it states that
  restoring the originals will no longer be possible.

### K5 — Transaction states (shared, defined by O4) — FROZEN 2026-10-09 · amended R2 2026-10-09

States [D R2]: `CREATED, SNAPSHOTTED, APPLYING, VERIFYING, AWAITING_CONFIRMATION, REVERTING,
COMPLETED, REVERTED, FAILED_CLEAN, REVERT_FAILED`. (**`CONFIRMED` removed** — Keep is one atomic step.)

| From | To |
|---|---|
| CREATED | SNAPSHOTTED · REVERTING (revert/uninstall tx) · FAILED_CLEAN |
| SNAPSHOTTED | APPLYING · FAILED_CLEAN |
| APPLYING | VERIFYING · REVERTING |
| VERIFYING | AWAITING_CONFIRMATION · REVERTING |
| AWAITING_CONFIRMATION | COMPLETED (only via `Journal.keep()`) · REVERTING |
| REVERTING | REVERTED · REVERT_FAILED |
| REVERT_FAILED | REVERTING (retry by recovery / `reset`) |

- **Terminal:** `COMPLETED`, `REVERTED`, `FAILED_CLEAN` (nothing was written).
- **`REVERT_FAILED` is unfinished** → login recovery and `reset` retry it; the user is told.
- A per-adapter revert after a failed verify (M2.1) happens inside the apply tx and does not
  change the tx state; it is recorded with `record_revert`.
- **If that per-adapter revert fails** (any item `failed`): the controller stops applying,
  moves the tx to `REVERTING` and rolls back the **whole** tx. Result `REVERTED`, or
  `REVERT_FAILED` if anything still needs recovery. The apply **never** proceeds to
  `AWAITING_CONFIRMATION` after a failed revert.

### K6 — Coverage status (shared, defined by O2) — FROZEN 2026-10-09

```python
class CoverageStatus(StrEnum):
    COVERED = "covered"
    NOT_COVERED = "not_covered"        # app keeps its font; text can go to the reader
    PENDING = "pending"                # app must be closed and wasn't
    RESTART_NEEDED = "restart_needed"

@dataclass(frozen=True)
class CoverageEntry:
    target: Target
    status: CoverageStatus
    detail_code: ErrorCode | str
```

**Mapping (owned by O2, in the controller) [D]:**

| Situation | Result |
|---|---|
| Target must be closed and is running | `PENDING` |
| Per-target adapter error or timeout | `NOT_COVERED` + that code |
| Unexpected exception | `NOT_COVERED` + `UNEXPECTED_ADAPTER_ERROR` (raw exception text never shown or logged) |
| **fontconfig** fails (incl. timeout) | **not a coverage result** — the whole apply stops and reverts |

### K7 — Font generator API (owner O1) — FROZEN 2026-10-09 · amended R2 2026-10-09

```python
@dataclass(frozen=True)
class FontSet:
    key: str                      # content key (M1.5)
    reading_family: str           # "AccessSans-<key>"
    ui_family: str                # "AccessSansUI-<key>"
    files: tuple[str, ...]        # installed file paths
    styles: tuple[str, ...]       # styles actually built, e.g. ("Regular", "Bold") — partial allowed [D R2]

@dataclass(frozen=True)
class FontBuildResult:
    status: Literal["built", "cached", "refused", "failed"]
    fonts: FontSet | None         # None unless built/cached
    reason_code: str | None       # e.g. "BASE_IS_MONOSPACE", "VALIDATION_FAILED"

def build(config: Configuration) -> FontBuildResult              # validated + installed, for Apply
def build_preview(config: Configuration) -> PreviewFonts         # temporary, private; never installed
def remove_unused() -> list[str]                                 # keys removed
```

**Rules [D]:**
1. `build()` is **synchronous**; callers run it off the UI thread. Single-flight per key
   (same key waits, different keys may build in parallel); the install + `fc-cache` step is serialized.
2. **Preview never installs:** `build_preview()` uses the same generation code, writes to a temp
   dir and loads the files privately for the preview only (A5 [V]; image fallback if unreliable).
   Files are deleted when the preview closes.
3. **`remove_unused()` keeps** a font if it is referenced by [D R2]:
   - the active configuration (`Journal.active()`);
   - a saved preset (keeps switching fast);
   - an unfinished tx;
   - any `LocationRecord` with `managed_font_key` or `unresolved` set;
   - any live setting found by **inspecting the supported font fields** of each target
     (e.g. the family names in a browser `Preferences` file — not its whole-file hash).
   If usage **can't be determined**, the font is kept. Runs only when no tx is active.
4. **Partial styles [D R2]:** a base font may lack styles (e.g. no italic). Only available styles are
   built and reported in `FontSet.styles`; ScribeSense **does not promise** how other apps synthesize
   a missing style. [V] check each bundled family's actual files.

### K8 — Paths and error codes (shared, defined by O3) — FROZEN 2026-10-09 (code list v1) · amended R2 2026-10-09

```python
def data_dir() -> Path        # $SCRIBESENSE_DATA_DIR  else ~/.local/share/scribesense
def fonts_dir() -> Path       # $SCRIBESENSE_FONTS_DIR else ~/.local/share/fonts/scribesense
def config_home() -> Path     # $XDG_CONFIG_HOME else $HOME/.config
def home() -> Path            # $HOME
def logs_dir() -> Path        # data_dir()/logs
def lock_path() -> Path       # data_dir()/apply.lock   (stable file; owner record inside, K4 rule 1)
def socket_path() -> Path     # $XDG_RUNTIME_DIR/scribesense/service.sock; fallback data_dir()/service.sock
                              # (dir 0700, socket 0600) — amended 2026-10-12, see note below

def run_cmd(argv: list[str], timeout: float) -> CompletedProcess   # the only way to start external commands (K3 rule 9)

class ErrorCode(StrEnum): ... # one stable list for logs, reports and UI

class ScribeSenseError(Exception):
    def __init__(self, code: ErrorCode): ...
```

**Socket location (amended 2026-10-12, decided by O4; O3 as K8 owner to confirm):** Unix socket paths are
limited to **108 bytes by the Linux kernel**; a socket under a long `$HOME` could fail to bind. The socket
therefore lives in the per-user runtime dir (`/run/user/<uid>`, short, mode 0700, cleared at logout), in a
`scribesense/` subfolder that ScribeSense owns (0700). If `$XDG_RUNTIME_DIR` is unset, it falls back to
`data_dir()/service.sock` (the old location; the length limit then applies). `lock_path()` is unchanged.

**Rules [D]:** no module hard-codes these paths. The test sandbox (M3.3) sets `HOME`,
`XDG_CONFIG_HOME`, `XDG_RUNTIME_DIR`, `SCRIBESENSE_*` and `FONTCONFIG_FILE` — inside bubblewrap, not
as the isolation boundary itself. Error codes are fixed identifiers — never paths, text, or exception messages.
Logging failures never break the app.

**ErrorCode list v1 — approved 2026-10-09, add-only** (compiled from the module cards). Codes may be **added** by PR with the
owner + one other; existing codes are never renamed or reused. K1 validation codes
(`NON_FINITE`, `OUT_OF_RANGE`, `UNKNOWN_FONT`, `NOT_ON_STEP`, `UI_LINE_ABOVE_RECOMMENDED`) are
`Issue` codes, not errors.

| Area | Codes |
|---|---|
| Fonts (O1) | `BUNDLED_FONT_MISSING` · `FONT_UNREADABLE` · `BASE_IS_MONOSPACE` · `BASE_IS_ICON_FONT` · `PATCH_UNSUPPORTED` · `VALIDATION_FAILED` · `INSTALL_FAILED` · `PREVIEW_UNAVAILABLE` |
| Apply / preflight (O2) | `APPLY_IN_PROGRESS` · `NOT_WRITABLE` · `NO_DISK_SPACE` · `ADAPTER_UNAVAILABLE` · `TX_NOT_PENDING` · `CONFIRM_STARTUP_TIMEOUT` · `CONFIRM_HEARTBEAT_LOST` · `CONFIRM_TIMEOUT` · `SERVICE_UNREACHABLE` |
| Adapters (O4) | `ADAPTER_TIMEOUT` · `ADAPTER_COMMAND_FAILED` (external command exited non-zero) · `ADAPTER_VERIFY_FAILED` · `UNEXPECTED_ADAPTER_ERROR` · `CHECK_UNAVAILABLE` · `SCHEMA_MISSING` · `QT6CT_NOT_IN_SESSION` · `BROWSER_RUNNING` · `PREFERENCES_UNREADABLE` · `PROFILE_NOT_FOUND` |
| Safety (O4) | `BACKUP_MISSING` · `BACKUP_CORRUPT` · `CHANGED_DURING_APPLY` · `PLAN_CONFLICT` · `LOCK_TIMEOUT` · `WRITE_NOT_DURABLE` · `REVERT_ITEM_FAILED` · `DRIFT_SKIPPED` · `KEYBIND_CONFLICT` |
| Store (O3) | `STORE_BUSY` · `STORE_READ_ONLY` · `MIGRATION_FAILED` · `STORE_CORRUPT` · `STORE_IO_ERROR` · `SCHEMA_UNSUPPORTED` · `PRESET_READ_ONLY` · `FONT_IDENTITY_CONFLICT` · `FONT_IN_USE` · `INTERRUPTED_BUILD` · `SAMPLES_DISABLED` · `RECOVERY_OBLIGATIONS_REMAIN` |
| Tests (O3) | `SANDBOX_UNAVAILABLE` |
| Reader / capture (O2) | `NOTHING_SELECTED` · `CLIPBOARD_EMPTY` · `SELECTION_UNAVAILABLE` · `TOO_LARGE` |
| Extension / IPC (O4 + O2) | `APP_NOT_RUNNING` · `HOST_MISSING` · `BAD_MESSAGE` |

### K9 — Store repositories (owner O3) — DRAFT 2026-10-09, **O3 sign-off required**

Blocks: M3.1 store, M4.10 journal, M4.11 revert, the 7 recovery scenarios (§9). Nothing else.

```python
# ---- Records ----
FontStatus = Literal["building", "validated", "failed", "removed"]

@dataclass(frozen=True)
class FontRegistryEntry:
    key: str
    config: Configuration
    reading_family: str
    ui_family: str
    styles: tuple[str, ...]             # styles actually built (K7: missing styles allowed)
    files: tuple[str, ...]
    file_sha256: tuple[str, ...]        # verified before activation; mismatch → VALIDATION_FAILED
    status: FontStatus
    reason_code: str | None
    created_at: datetime                # UTC, timezone-aware
    last_used_at: datetime

SETTINGS: dict[str, tuple[type, object]] = {      # fixed list: name → (type, default)
    "recommend_opt_in":    (bool, False),
    "keybind_reader":      (str, "SUPER ALT, R"),
    "keybind_preset_next": (str, "SUPER ALT, P"),
    "keybind_reset":       (str, "CTRL ALT SHIFT SUPER, BackSpace"),
    "lock_wait_seconds":   (int, 60),               # valid 10–600
}

# ---- Store ----
class Store:
    def __init__(self, path: Path, *, mode: Literal["normal", "recovery"] = "normal"): ...
        # every connection: WAL, synchronous=FULL, foreign_keys=ON, busy_timeout=5000
        # one connection per thread; never shared across threads or processes
    def transaction(self) -> ContextManager[None]
        # commit on exit, rollback on exception; nested call joins the outer one;
        # repository writes inside it join it; never spans an adapter write
    def delete_preferences(self) -> None            # presets, settings, samples — never recovery data
    def delete_recovery_data(self, *, precondition: NoRecoveryObligations) -> None
        # journal tables; token only from Journal.no_obligations()
    presets: PresetRepo
    fonts: FontRegistryRepo
    coverage: CoverageRepo                          # READ-ONLY
    settings: SettingsRepo
    samples: SampleRepo

class PresetRepo:
    def list(self) -> list[Preset]                  # built-ins first, then user presets by name
    def get(self, id: str) -> Preset | None
    def save(self, p: Preset) -> None               # built-in → PRESET_READ_ONLY
    def delete(self, id: str) -> bool               # False if not found; built-in → PRESET_READ_ONLY

class FontRegistryRepo:
    def get(self, key: str) -> FontRegistryEntry | None
    def create(self, e: FontRegistryEntry) -> None  # same key, different config/files → FONT_IDENTITY_CONFLICT
    def set_status(self, key: str, status: FontStatus, reason: str | None) -> None   # missing → KeyError
    def touch(self, key: str) -> None               # last_used_at; missing → KeyError
    def keys(self, status: FontStatus | None = None) -> list[str]
    def mark_removed(self, key: str) -> None        # in use (K7 rule 3 references) → FONT_IN_USE

class CoverageRepo:
    def last_completed_apply(self) -> list[CoverageEntry]
        # historical: the latest completed apply — not proof of what is active now

class SettingsRepo:
    def get(self, name: str) -> object              # unknown → KeyError; default if never set
    def set(self, name: str, value: object) -> None
        # unknown → KeyError; exact type (type(v) is T; True is not an int) → TypeError; range → ValueError

class SampleRepo:
    def add(self, config: Configuration) -> None    # re-reads recommend_opt_in each call; off → SAMPLES_DISABLED
    def delete_all(self) -> None                    # opting out also deletes existing samples
```

**Journal-only tables** (reached only through K4):

| Table | Key columns |
|---|---|
| `transactions` | id, kind, state, preset_id, reverts, created_at, updated_at |
| `journal_items` | id, tx_id, location, target, existed, old_value, old_sha256, file_mode, value_type, backup_id, intent_sha256, applied_sha256, retained, revert_outcome |
| `backups` | id, sha256, bytes (BLOB) |
| `location_records` | location (PK), target, **own copy** of baseline fields + `baseline_backup_id`, `managed_sha256`, `managed_font_key`, `unresolved` |
| `active_config` | single row: config, font_key, source_preset_id, style_revision |
| `coverage` | tx_id, target_id, status, detail_code |

`location_records` keeps its own baseline copy, so pruning `journal_items` never loses an original.

**Rules:**

| Area | Rule |
|---|---|
| Font status | `building → validated \| failed` · `failed → building` (retry) · `validated → removed`. At startup a leftover `building` → `failed`, reason `INTERRUPTED_BUILD`. |
| Busy | Reads and writes may both raise `STORE_BUSY`; callers retry a bounded number of times, never forever. SQLite busy timeout (5 s) ≠ `lock_wait_seconds` (60 s, apply/reset lock). |
| Errors | `STORE_BUSY` contention · `STORE_READ_ONLY` permission · `STORE_CORRUPT` integrity · `NO_DISK_SPACE` disk full · `STORE_IO_ERROR` other I/O. Never claim a save that didn't happen. |
| Migrations | Version in `PRAGMA user_version`; `store/migrations/NNN_name.sql` in order; schema change + version update in **one** transaction; failure → `MIGRATION_FAILED`, app refuses writes. **Recovery mode never migrates**: on an unsupported version it writes nothing and reports `SCHEMA_UNSUPPORTED`. Journal-table changes stay backward compatible. |
| Pruning | `Journal.prune_history(before, keep_last)` deletes only terminal txs older than `before`, outside the newest `keep_last`, not referenced by any unfinished revert or unresolved location. A backup is deleted only when nothing references it. Order `(created_at, id)`. `keep_last < 0` → `ValueError`. Runs under the apply lock; returns the count. Never touches `location_records`, their backups or `active_config`. |
| Delete all data | Run by the controller: apply lock → block applies and samples → finish unfinished txs + reset → report failed/skipped settings → `delete_recovery_data()` **only if** `no_obligations()` returns a token; otherwise refuse (`RECOVERY_OBLIGATIONS_REMAIN`) and offer `delete_preferences()` only. Wording: "removes ScribeSense's records" — not secure erasure. |
| Serialization | `Configuration` / `Preset` as versioned JSON `{"v":1,…}`; timestamps UTC ISO-8601 with timezone; tuples as JSON arrays. |
| Presets vs active | Deleting or editing a preset never changes `active_config` or the journal; `source_preset_id` is informational and may point to a deleted preset. |

**Acceptance tests (O3):**
1. Round-trip every repository and record type (incl. JSON `v` field and UTC timestamps).
2. Built-in preset save/delete → `PRESET_READ_ONLY`.
3. `SettingsRepo`: unknown name, wrong type (`True` for an int), out-of-range value each rejected.
4. `create()` with an existing key but different content → `FONT_IDENTITY_CONFLICT`.
5. Leftover `building` entry at startup → `failed` / `INTERRUPTED_BUILD`.
6. `mark_removed()` on a font that is active / managed / in a preset / in an unfinished tx → `FONT_IN_USE`.
7. Two processes (service + CLI) writing at once → one waits or gets `STORE_BUSY`; no corruption.
8. Migration from empty and from each previous version; failed migration leaves `user_version` unchanged.
9. Recovery mode on a newer schema → writes nothing, `SCHEMA_UNSUPPORTED`.
10. `prune_history()` keeps `location_records`, their backups, unfinished and newest `keep_last` txs.
11. `delete_recovery_data()` refused while anything is unfinished, unresolved or managed.
12. Opt-out deletes samples; `add()` after opt-out → `SAMPLES_DISABLED`; SELECT sweep finds no canary text.

**O3 sign-off checklist:** column types · matches the planned implementation · the 12 tests above
accepted · journal tables return everything in `JournalItem` / `LocationRecord`.

### K10 — CLI commands (owner O2) [D A7] — FROZEN 2026-10-09

| Command | Used by |
|---|---|
| `scribesense` | opens / focuses the app |
| `scribesense apply <preset-id>` | UI, scripts |
| `scribesense preset next` | keybind Super+Alt+P [D] |
| `scribesense read --selection` | keybind Super+Alt+R [D] |
| `scribesense read --clipboard` | UI |
| `scribesense reset` | recovery keybind Ctrl+Alt+Shift+Super+Backspace [D] |
| `scribesense recover --login` | login check [D A3] |
| `scribesense uninstall [--delete-data] [--remove-fonts-anyway]` | user [R2-9] |
| `scribesense doctor` | user, install |
| `scribesense confirm <tx>` | started by the service after apply — **fresh process, bypasses single-instance forwarding**; accepted only for the currently pending tx [D A15] |
| `scribesense quit` | user — stops the background service safely [D A1] |

**Exit codes [D]:**

| Code | Meaning |
|---|---|
| 0 | completed (partial coverage / skipped drift described in the output) |
| 1 | failed |
| 2 | usage error |
| 3 | another transaction is active |
| 4 | the service is required and couldn't be reached or started |

- `reset`, `recover --login`, `uninstall`, `doctor` never need the service or GUI (A2) and never return 4.
- `read` / `preset next` start the service if it isn't running; 4 only if that fails.
- Output is plain readable text; `reset` ends with one line: restored / skipped / failed counts.

### K11 — Extension ↔ app messages (owner O4 with O2) [D A6, A16] — FROZEN 2026-10-09 · amended R2 2026-10-09

Chromium family only. Path: extension → native-messaging host → service socket (K4 rule 5).
`get_style` returns the **confirmed** style only, re-requested after Apply / Revert / Original.

```json
{"v": 1, "type": "open_in_reader", "text": "<selected text>"}
{"v": 1, "type": "get_style"}
-> {"v": 1, "ok": true, "active": true,  "revision": 7, "css": "<M4.8 output>"}
-> {"v": 1, "ok": true, "active": false, "revision": 8}            # Original — remove styling
-> {"v": 1, "ok": false, "error": "APP_NOT_RUNNING"}
<- {"v": 1, "type": "style_changed", "revision": 9}                # pushed over the open connection
```

Errors: `APP_NOT_RUNNING`, `HOST_MISSING`, `TOO_LARGE` (A17), `BAD_MESSAGE`.

**Rules [D R2]:**
- **The app sends the CSS** (M4.8 is the only source); the extension never builds CSS itself.
- **Revisions order everything**, inactive replies included; the extension ignores an older revision.
- **Persistent connection** (`connectNative`): the service pushes `style_changed`; the extension then
  fetches `get_style` and updates eligible tabs. Restricted pages are reported, not promised.
- **On connect / reconnect:** always fetch the full confirmed state, even if the cached revision looks
  current. Notifications speed things up; they are not the source of correctness.
- **Disconnected:** keep the last styling, show "disconnected" in the extension.
- **Local Disable:** a button in the extension removes ScribeSense styling without the app.
- **Offline reset / uninstall:** the extension can't be told; on its next connection it receives
  `active: false`. Until then the user can use local Disable; the reset/uninstall output says so.
- **Bridge:** the native host translates Chrome's framing (UTF-8 JSON with a native-order 32-bit
  length prefix) ↔ newline-delimited JSON on the socket. Our 1 MiB limit applies to the **complete
  encoded JSON message**. Chrome's own limits: 1 MB host→browser, 64 MiB browser→host.
**Pinned extension ID [D 2026-10-09]:** the same public `key` in the development and distributed
`manifest.json`, so the ID is identical on every machine; the native-messaging host manifest lists
that exact ID in `allowed_origins`. **No private signing key is ever committed to the repo.**


---

## 6. Integration seams (how modules link)

| From → To | Contract | Handshake rule |
|---|---|---|
| UI (M2.5) → Config (M1.1) | K1 | UI builds controls only from `RANGES`; shows `validate()` warnings, blocks on errors |
| Controller (M2.1) → Generator (M1.8) | K7 | Apply never starts unless status is `built` or `cached` |
| Controller → Journal (M4.10) | K4, K5 | Controller opens the tx and moves every state; journal refuses illegal transitions |
| Controller → Adapters (M4.x) | K3 | Order per item: snapshot → `record_snapshot` → plan → `record_intent` → apply → `record_applied` → verify |
| Adapters → Journal | K3, K4 | Adapters return items; only the controller records them (adapters never call the store) |
| Journal → Store (M3.1) | K9 | Journal is the only writer of tx/journal/baseline/backup tables |
| Revert (M4.11) → Adapters | K3, K4 | Revert applies K4 rule 4 (original / intent / drift) first, then calls `adapter.revert(item, journal.backup(tx, item_id))` |
| Confirm (M2.3) → Journal / Revert | K4 | Keep → `journal.keep(tx, active)` (atomic, then ack) · timeout / close / Revert → `revert_tx(tx)` |
| Recovery (M4.12) → Journal + Revert | K4 | At login: step 1 of K4 rule 4 (finish unfinished txs). `reset` / uninstall: step 1 then step 2 (baselines) |
| Reader (M2.9) ← Capture (M2.10) | — | Capture passes normalized text in memory only |
| Extension (M4.9) ↔ App | K11 | Text goes app-ward only on an explicit right-click; no automatic capture |
| Install/Uninstall (M3.8) → Revert, Keybinds | K4, K10 | Uninstall = `revert_all` with the drift rule, then removals |
| Harness (M3.3) → everyone | K8 | Every isolated test runs inside the sandbox fixture |

---

## 7. Module cards

### O1 — Configuration & Fonts

#### M1.1 Configuration vocabulary
| Field | Spec |
|---|---|
| Purpose | Single definition of every setting: name, unit, range, step, default; validation and rounding. [D Step 2, F2] |
| Owner / may be helped? | O1 / no (contract) |
| Built by / reviewed by | TBD / one other owner |
| Depends on | nothing |
| Used by | M1.2, M1.4–M1.8, M2.1, M2.5, M3.1, M4.x |
| Provides | K1 |
| Exact interface | K1 |
| Example | `round_to_steps(letter_em=0.131)` → `0.14`; `validate(ui_line=1.5)` → `[Issue("warning","ui_line","UI_LINE_ABOVE_RECOMMENDED")]` |
| Data it reads/writes | none (pure) |
| Data ownership | Source of truth for ranges, steps and the shortlist |
| Preconditions | — |
| Postconditions | Every value returned by `round_to_steps` lies on a step inside its range |
| Failure cases + required behaviour | Out-of-range value → `error` issue, never silently clamped; UI line above 1.3× → `warning` only [D] |
| Must never | Import UI, store or adapter code |
| Acceptance tests | Every range/step from §1 present; rounding idempotent; out-of-range → error; ui_line 1.4 → warning not error |
| Done when | Tests pass · reviewed · frozen in the contract session |

#### M1.2 Presets
| Field | Spec |
|---|---|
| Purpose | Built-in presets (Default, Reading, High Separation), Custom, Original sentinel, user presets model. [D F6] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O2 |
| Depends on | M1.1 |
| Used by | M2.1, M2.5, M3.1 (persists user presets) |
| Provides | K2 |
| Exact interface | K2 |
| Example | `resolve(BUILTINS[0].values)` with Noto Sans → word target +0.16 em converted to a multiplier, rounded to 0.25, validated [D A9] |
| Data it reads/writes | none (persistence by M3.1) |
| Data ownership | Owns built-in values; user presets are owned by the user, stored by M3.1 |
| Preconditions | — |
| Postconditions | Every built-in passes `validate()` with no errors |
| Failure cases + required behaviour | — (static data) |
| Must never | Let a user preset overwrite a built-in |
| Acceptance tests | Built-ins resolve and validate; all six fields present; target_em recalculated on font change, multiplier kept; Original is not a preset; values match K2 table |
| Done when | Tests pass · reviewed |

#### M1.3 Font eligibility rules
| Field | Spec |
|---|---|
| Purpose | Decide whether a font may be patched and which glyphs get spacing. [D Step 4, F2] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M1.1, fontTools [D A4] |
| Used by | M1.4, M1.8 |
| Provides | `is_eligible(path) -> Eligibility(ok, reason_code)` · `latin_glyphs(font) -> set[str]` |
| Exact interface | as above [P] |
| Example | Fira Code → `Eligibility(False, "BASE_IS_MONOSPACE")`; Noto Sans → ok, Latin set excludes Devanagari/Greek/Cyrillic |
| Data it reads/writes | reads font files |
| Data ownership | Owns the definition of "monospace", "icon font" and "Latin glyph" |
| Preconditions | Font file readable |
| Postconditions | Decision is deterministic for the same file |
| Failure cases + required behaviour | Unreadable/corrupt font → `Eligibility(False, "FONT_UNREADABLE")` |
| Must never | Return ok for a monospace or icon font |
| Acceptance tests | Monospace refused; an icon font refused; only Latin glyphs in the spacing set |
| Done when | Tests pass · reviewed |

#### M1.4 Font patcher
| Field | Spec |
|---|---|
| Purpose | Produce a patched font: wider Latin advances (letter), wider space glyph (word), raised line metrics (line height), ligatures removed, renamed. [D Step 2, F2] |
| Owner / may be helped? | O1 / no — the core algorithm |
| Built by / reviewed by | TBD / O3 |
| Depends on | M1.1, M1.3, fontTools [D A4] |
| Used by | M1.5 |
| Provides | `patch(source_path, config, line: float, out_path) -> None` |
| Exact interface | as above [P] |
| Example | Noto Sans Regular + letter 0.12, word 1.5×, line 1.5 → file whose Latin advances are +0.12 em, space ×1.5, line height 1.5× |
| Data it reads/writes | reads base font file; writes one output file |
| Data ownership | — |
| Preconditions | Source eligible (M1.3) |
| Postconditions | **Spacing** (advance widths, space width) changed only for Latin glyphs. Permitted font-wide changes: vertical line metrics, removal of ligature features, naming tables. Nothing else changed [D R2] |
| Failure cases + required behaviour | Unsupported outline/format → raise `PATCH_UNSUPPORTED`, write nothing |
| Must never | Change non-Latin glyphs; keep the original family name (shadow mode = Later) |
| Acceptance tests | Measured advance and space width match config; Devanagari glyphs unchanged; no `liga` feature; TrueType and CFF fonts handled; variable fonts flattened per weight [D Step 2 fixes] |
| Done when | Tests pass for all 4 shortlist fonts · reviewed |

#### M1.5 Variants and naming
| Field | Spec |
|---|---|
| Purpose | Build the reading and UI variants, 4 styles each, with deterministic names. [D F2, Step 3] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M1.4 |
| Used by | M1.8 |
| Provides | `content_key(config, source_sha, generator_version) -> str` · `build_variants(config) -> list[path]` |
| Exact interface | as above [P] |
| Example | key = sha256(sha256 of every base-font style file + normalized `letter_em`, `word_scale`, `reading_line`, `ui_line` (fixed-decimal strings) + ligature setting + generator version)[:12] → `AccessSans-3f9a1c2b7d10` / `AccessSansUI-3f9a1c2b7d10` [D A19] |
| Data it reads/writes | writes into a temporary directory |
| Data ownership | Owns the key formula and family names [D A19] |
| Preconditions | Config rounded (M1.1) |
| Postconditions | Reading variant uses `reading_line`; UI variant uses `ui_line`; every style **present in the base font** is built (Regular required; Bold/Italic/BoldItalic when available) and listed in `FontSet.styles` [D R2] |
| Failure cases + required behaviour | Missing Regular → `BUNDLED_FONT_MISSING`; other missing styles are not an error — reported via `FontSet.styles` [D R2] |
| Must never | Produce two different outputs for the same inputs |
| Acceptance tests | Same font-building inputs → identical key and bytes; changing any **font-building** input (incl. `ui_line` alone) → new key; `text_scale` (a desktop setting) never changes the key; 1.5 and 1.50 → same key |
| Done when | Tests pass · reviewed |

#### M1.6 Font validation
| Field | Spec |
|---|---|
| Purpose | Check a built font really carries the requested spacing before it can be activated. [D F2] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O3 |
| Depends on | M1.5, `pango-view` |
| Used by | M1.8 |
| Provides | `validate_font(paths, config) -> ValidationResult(ok, measurements, reason_code)` |
| Exact interface | as above [P] |
| Example | Renders the fixed sample under a private fontconfig; expected width ratio vs the base font within tolerance |
| Data it reads/writes | temporary files only |
| Data ownership | Owns the tolerance values [P] |
| Preconditions | Fonts in a temporary directory, not installed |
| Postconditions | Result recorded on the font registry entry (via M1.8 → M3.1) |
| Failure cases + required behaviour | Out of tolerance → `VALIDATION_FAILED`; font never activated [D] |
| Must never | Use the user's real fontconfig |
| Acceptance tests | A correct font passes; a deliberately unpatched font fails |
| Done when | Tests pass · reviewed |

#### M1.7 Font cache and install
| Field | Spec |
|---|---|
| Purpose | Move validated fonts into the font directory and refresh the cache. [D Step 2] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M1.6, K8 |
| Used by | M1.8, M3.8 (removal on uninstall) |
| Provides | `install(key, paths) -> list[path]` · `is_installed(key)` · `remove(key)` |
| Exact interface | as above [P] |
| Example | temp dir → atomic rename to `~/.local/share/fonts/scribesense/<key>/` → `fc-cache <dir>` |
| Data it reads/writes | font directory |
| Data ownership | Owns the files under `fonts_dir()` |
| Preconditions | Validation ok |
| Postconditions | `fc-cache` run after every write [D Step 2] |
| Failure cases + required behaviour | Disk full / rename fails → nothing half-installed; `INSTALL_FAILED` |
| Must never | Write outside `fonts_dir()` |
| Acceptance tests | Font visible to `fc-match` in the sandbox right after install; remove deletes only that key |
| Done when | Tests pass · reviewed |

#### M1.8 Generator API
| Field | Spec |
|---|---|
| Purpose | One entry point: configuration → installed FontSet, or a clear refusal. [D F2] |
| Owner / may be helped? | O1 / no (contract) |
| Built by / reviewed by | TBD / O2 |
| Depends on | M1.1–M1.7, M3.1 (font registry) |
| Used by | M2.1, M2.5 (preview) |
| Provides | K7 |
| Exact interface | K7 |
| Example | Previously built config → `FontBuildResult("cached", FontSet(...), None)` instantly |
| Data it reads/writes | font registry rows (via M3.1) |
| Data ownership | Owns font registry entries |
| Preconditions | — |
| Postconditions | `built`/`cached` ⇒ fonts installed and validated |
| Failure cases + required behaviour | Any step fails → `failed`/`refused` with a reason code; nothing activated |
| Must never | Return `built` for an unvalidated font |
| Acceptance tests | Cache hit returns without rebuilding; refusal paths return codes |
| Done when | Tests pass · frozen in contract session · reviewed |

### O2 — User Experience

#### M2.1 Apply controller
| Field | Spec |
|---|---|
| Purpose | Run one apply from start to finish (§2.2), driving transaction states. [D F5] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K1, K3–K7, M1.8, M4.1–M4.13 |
| Used by | M2.5, M2.11 (apply, preset next) |
| Provides | `apply(preset) -> ApplyReport` · `preflight() -> list[Problem]` |
| Exact interface | as above [P] |
| Example | Brave open → Brave `PENDING`, others continue, report lists it |
| Data it reads/writes | through K4 only |
| Data ownership | Owns the sequence; does not own any data |
| Preconditions | Preflight passes: permissions, files writable, directories exist, fonts valid, disk space, adapters available, no other apply running [D] |
| Postconditions | Tx ends AWAITING_CONFIRMATION (then M2.3) or REVERTED/FAILED |
| Failure cases + required behaviour | Build fails → stop before writing · fontconfig fails → stop + revert · other adapter fails → continue, NOT_COVERED · verify fails → revert that adapter only; **if that revert fails → stop, roll back the whole tx (K5)** · verify passes → `mark_retained` · two targets plan the same location → `PLAN_CONFLICT` before any write · state changed since snapshot → that target `CHANGED_DURING_APPLY` · must-be-closed declined → PENDING [D] |
| Must never | Write directly; skip a snapshot; close or restart an app |
| Acceptance tests | One test per failure row above, with fake adapters |
| Done when | Tests pass with fakes and in the sandbox · reviewed by O4 |

> **Note (2026-10-12, O4 → O2):** until this controller exists, the §9 recovery tests use a
> **reference apply sequence** in `scribesense/safety/testing.py` (`RecoverySystem.apply`, K4 rule 2
> order + the failure rows above). It is test support, not production code. **When M2.1 is built,
> `make_recovery_system` must call this controller instead, and the §9 scenarios in `tests/recovery/`
> must still pass** — they are part of this card's acceptance.

> **Note (2026-10-12, O4 → O2) — verify rule for this controller:** treat a target's verify as **passed only
> if `chosen_ok and drawn_ok is not False`** (M4.13). `drawn_ok=None` means "no drawn check for this
> target" and does not fail it; `drawn_ok=False` (mismatch or probe unavailable) **does** fail it, even when
> `chosen_ok` is True. On failure: revert that adapter only (row above); if that revert fails, roll back the
> whole tx (K5). The reference apply in `safety/testing.py` already follows this rule and has tests for it.

#### M2.2 Restart notice
| Field | Spec |
|---|---|
| Purpose | Tell the user which running apps must be restarted. [D F5] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K3 (`Target.running`), K6 |
| Used by | M2.1, M2.6 |
| Provides | `restart_list(report) -> list[Target]` |
| Exact interface | as above [P] |
| Example | "Restart: LibreOffice, Zen" |
| Data it reads/writes | none |
| Data ownership | — |
| Preconditions | Apply report available |
| Postconditions | Every RESTART_NEEDED target that is running is listed |
| Failure cases + required behaviour | Unknown running state → listed as "may need restart" |
| Must never | Restart or kill an app |
| Acceptance tests | Running + restart-needed targets listed; others not |
| Done when | Tests pass · reviewed |

#### M2.3 Confirm flow
| Field | Spec |
|---|---|
| Purpose | Prove the new fonts are readable in a **fresh process** and run the 30 s keep-or-revert, with an independent watchdog. [D F4, F5, A15] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 (recovery + failure behaviour) |
| Depends on | K4, K5, K10 `confirm`, M4.11 |
| Used by | M2.1 |
| Provides | `start_confirmation(tx)` (service side, watchdog) · `scribesense confirm <tx>` (window side) |
| Exact interface | as above [P] |
| Example | Service launches `scribesense confirm 42` → window: "Can you read this? Keep (30)…"; no answer → revert |
| Data it reads/writes | tx state via K4 |
| Data ownership | — |
| Preconditions | Tx in AWAITING_CONFIRMATION; fonts generated, installed and `fc-cache` done |
| Postconditions | Tx COMPLETED or REVERTED |
| Deliverables | **Startup deadline** (window must become ready in time) · **readiness signal** sent when sample + controls are shown, not at process start · **30 s** counted from readiness · **heartbeat** from the window's event loop with a documented missed-heartbeat threshold · window checks its sample resolves to the intended family [D A15] |
| Failure cases + required behaviour | Startup deadline missed / crash / heartbeat lost / timeout / "Revert" pressed → watchdog reverts · late confirm after rollback → rejected · watchdog crash → login recovery (A3) |
| Must never | Run in the service's process or event loop · be forwarded to the existing instance · treat user inactivity as a hang (only the 30 s deadline applies) · accept confirmation for a tx that isn't the pending one |
| Acceptance tests | Keep → `keep()` commits, then ack; crash before commit → reverted at recovery; crash after → COMPLETED and active config match; timeout → REVERTED; startup failure, crash, freeze, rejection, late confirmation each → correct outcome; killed mid-countdown → recovered at next login (VM) |
| Done when | Tests pass · manual checklist item passes · reviewed by O4 |

#### M2.4 UI shell
| Field | Spec |
|---|---|
| Purpose | Application window, navigation, single-instance background service; closing the window doesn't quit; explicit Quit. [D F6, A1] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O3 |
| Depends on | GTK4 + libadwaita [D A4] |
| Used by | M2.5–M2.9 |
| Provides | window, pages, action entry points for M2.11 |
| Exact interface | [P] actions: `apply`, `preset-next`, `open-reader` |
| Example | Second launch focuses the existing window; `scribesense confirm` is the one exception (fresh process) [D A15] |
| Data it reads/writes | none directly |
| Data ownership | — |
| Preconditions | — |
| Postconditions | — |
| Failure cases + required behaviour | Startup error → message + recovery info, never a blank window |
| Must never | Be required by the recovery path [D A2] |
| Acceptance tests | Keyboard-only navigation; screen-reader labels; large text (manual checklist) [D] |
| Done when | Accessibility checklist passes · reviewed |

#### M2.5 Presets, adjust and preview screens
| Field | Spec |
|---|---|
| Purpose | Pick a preset, adjust values, see the fixed sample paragraph before applying. [D F6] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O1 |
| Depends on | K1, K2, K7, M3.1 |
| Used by | user |
| Provides | preset list (built-ins, user, Custom, Original), adjust controls, preview, save-as |
| Exact interface | controls generated from `RANGES` |
| Example | UI line set to 1.5 → warning "menus in some apps may cut off text" [D] |
| Data it reads/writes | user presets via M3.1 |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Saved preset passes `validate()` |
| Failure cases + required behaviour | Preview can't load font → show message, apply still possible [P] |
| Must never | Apply anything without the user pressing Apply |
| Acceptance tests | Controls match RANGES; warning shown above 1.3×; preview shows sample in the new font [V A5] |
| Done when | Tests + accessibility checklist pass · reviewed |

#### M2.6 Coverage report
| Field | Spec |
|---|---|
| Purpose | Show, per app, COVERED / NOT_COVERED / PENDING / RESTART_NEEDED. [D F6] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K6, M3.1 (last coverage) |
| Used by | user |
| Provides | report screen |
| Exact interface | input: list of `(Target, CoverageStatus, detail_code)` |
| Example | "Zen — not covered: keeps its font; use the reader (Super+Alt+R)" |
| Data it reads/writes | reads coverage rows |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Every detected target appears once |
| Failure cases + required behaviour | Unknown detail code → generic text, code shown |
| Must never | Say "covered" for an unverified target |
| Acceptance tests | All four statuses render with correct text |
| Done when | Tests pass · reviewed |

#### M2.7 Settings screen
| Field | Spec |
|---|---|
| Purpose | "Help improve recommendations" opt-in, keybinds, "Delete all ScribeSense data", recovery info. [D F8, F10] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O3 |
| Depends on | M3.1, M4.14 |
| Used by | user |
| Provides | settings UI |
| Exact interface | — |
| Example | Opt-in off by default [D] |
| Data it reads/writes | settings rows |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Delete-all only after explicit confirmation [D]; first offers a reset — if declined, states that restoring originals will no longer be possible [D R2] |
| Failure cases + required behaviour | Keybind conflict → refuse and show the conflicting bind |
| Must never | Enable data collection by default |
| Acceptance tests | Default off; delete requires confirmation; recovery keybind displayed |
| Done when | Tests pass · reviewed |

#### M2.8 Tray (optional)
| Field | Spec |
|---|---|
| Purpose | Optional tray entry for switching presets. [D F6] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / — |
| Depends on | M2.4; [V] tray support on Hyprland/waybar |
| Used by | user |
| Provides | preset switch, open app |
| Exact interface | — |
| Example | — |
| Data it reads/writes | none |
| Data ownership | — |
| Preconditions | Tray host present |
| Postconditions | — |
| Failure cases + required behaviour | No tray host → feature hidden, no error |
| Must never | Be required for any core function |
| Acceptance tests | App works fully with tray disabled |
| Done when | Optional — after Must items |

#### M2.9 Fallback reader window
| Field | Spec |
|---|---|
| Purpose | Show captured text in the reading font and line height. [D F7] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M2.10, K7 (active FontSet) |
| Used by | user |
| Provides | reader window |
| Exact interface | `show(text: str)` |
| Example | Text copied from a PDF appears with the user's spacing |
| Data it reads/writes | memory only |
| Data ownership | Holds session text in memory; owns nothing persistent |
| Preconditions | Normalized text |
| Postconditions | Text kept until the session ends [D] |
| Failure cases + required behaviour | No active preset → show with the Default preset [P] |
| Must never | Write text to disk, logs or store; add extra CSS spacing (spacing is in the font) [D] |
| Acceptance tests | Canary text never appears on disk after a session (store + logs + data dir sweep) |
| Done when | Tests + accessibility checklist pass · reviewed |

#### M2.10 Capture
| Field | Spec |
|---|---|
| Purpose | Bring text into the reader: paste, clipboard, selection keybind, extension right-click (Chromium only — Firefox/Zen use the keybind). [D F7, A6] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | Wayland clipboard / primary selection, K11 |
| Used by | M2.9, M2.11 |
| Provides | `from_clipboard()`, `from_selection()`, `from_extension(msg)`, `normalize(text)` |
| Exact interface | each returns `CaptureResult(text | None, code)` [P] |
| Example | Nothing selected → `code="NOTHING_SELECTED"` |
| Data it reads/writes | memory only |
| Data ownership | — |
| Preconditions | User action triggered it |
| Postconditions | Text normalized (NFC, control chars stripped) |
| Failure cases + required behaviour | Empty selection → "nothing selected", never fall back to a whole document · too large → explicit message [D A17] |
| Must never | Capture without an explicit user action; log text |
| Acceptance tests | Each way in works; empty and oversize cases give distinct messages |
| Done when | Tests pass · reviewed by O4 |

#### M2.11 CLI entry points
| Field | Spec |
|---|---|
| Purpose | Commands used by keybinds, install, recovery and the user. [D A7] |
| Owner / may be helped? | O2 (proposed) / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K10, M2.1, M2.10, M4.12, M3.7, M3.8 |
| Used by | keybinds, install, user |
| Provides | K10 |
| Exact interface | K10 |
| Example | `scribesense reset` → revert all, prints a plain summary |
| Data it reads/writes | — |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Exit code 0 only on success |
| Failure cases + required behaviour | App not running for `read`/`preset next` → start it [P] |
| Must never | Load UI code for `reset`, `recover`, `uninstall`, `doctor` [D A2] |
| Acceptance tests | Each command; import test proves recovery commands load no UI modules |
| Done when | Tests pass · reviewed |

### O3 — Data & Platform

#### M3.1 Store
| Field | Spec |
|---|---|
| Purpose | Local persistence for one user. [D F8] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K1–K8, sqlite3 [D A4] |
| Used by | M1.8, M2.5–M2.7, M3.2, M4.10 |
| Provides | K9 repositories, schema, migrations |
| Exact interface | K9 [P] |
| Example | Tables [D A8]: `presets`, `active_preset`, `font_registry`, `transactions`, `journal_items`, `baselines`, `backups`, `coverage`, `settings`, `recommendation_samples` |
| Data it reads/writes | `~/.local/share/scribesense/` [D] |
| Data ownership | Owns the storage medium; **content ownership** stays with the writing module (journal tables → M4.10, font registry → M1.8) |
| Preconditions | — |
| Postconditions | Schema version recorded; migrations ordered |
| Failure cases + required behaviour | Read-only/locked DB → clear error; never claim a save that didn't happen |
| Must never | Store reader/document text, window titles, user document paths, screen contents [D] |
| Acceptance tests | Round-trip per repository; migration from empty; SELECT sweep finds no canary text |
| Done when | Tests pass · reviewed by O4 |

#### M3.2 Retention
| Field | Spec |
|---|---|
| Purpose | Prune old **transaction history** (and its per-tx backups). Never `LocationRecord`s or baseline backups. [D F8, R2] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M3.1 |
| Used by | app startup [P] |
| Provides | `prune(now)` |
| Exact interface | as above |
| Example | 40-day-old completed tx pruned unless it is in the last 10 |
| Data it reads/writes | transactions, journal items, backups |
| Data ownership | — |
| Preconditions | No tx active |
| Postconditions | `LocationRecord`s, baselines and their backups untouched; anything referenced by an unfinished tx untouched [D R2] |
| Failure cases + required behaviour | Error → skip pruning, keep data |
| Must never | Delete baselines or unfinished transactions |
| Acceptance tests | 30-day/last-10 rule; baselines survive |
| Done when | Tests pass · reviewed |

#### M3.3 Test sandbox
| Field | Spec |
|---|---|
| Purpose | Run every isolated test without touching the real desktop. [D F9] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K8 |
| Used by | all tests; **precondition for helper work** [D] |
| Provides | (1) unit fixture: fake adapters, `subprocess`/`run_cmd` blocked · (2) integration runner: **bubblewrap** sandbox — private `/tmp`, temp `HOME`, private `XDG_RUNTIME_DIR`, **no host D-Bus / Wayland / X11 / Hyprland / service sockets**, `--unshare-net`, no inherited descriptors, only minimal system paths mounted read-only (not the real home), `GSETTINGS_BACKEND=keyfile`, private fontconfig [D R2] |
| Exact interface | `sandbox` fixture [P] |
| Example | Adapter test writes `~/.config/...` → lands in a temp dir |
| Data it reads/writes | temp dirs only |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Temp dirs removed after each test |
| Failure cases + required behaviour | Sandbox can't start → **fail closed** (`SANDBOX_UNAVAILABLE`), never run unsandboxed |
| Must never | Let a test reach the real home directory |
| Acceptance tests | Escape tests: a write to a host path is **blocked**; connecting to the host session bus / Hyprland socket fails; real Flatpak/compositor tests run only in the VM (M3.5) |
| Done when | Tests pass · reviewed · announced as ready to helpers |

> **Note (2026-10-12, O4 → O3 to review):** O4 changed the unit fixture in `tests/conftest.py`:
> `SCRIBESENSE_DATA_DIR` points to a short private `/tmp/ss-*` dir (removed after each test), and
> Unix sockets are allowed there as well as under `tmp_path`. Reason: Unix socket paths are limited to
> 108 bytes and pytest's `tmp_path` is longer. `tests/unit/test_sandbox_guard.py` was updated to match.
> Since the socket moved to `$XDG_RUNTIME_DIR` (K8 amendment), O4's socket tests point
> `XDG_RUNTIME_DIR` into that short dir themselves. **Suggested O3 follow-up:** make the fixture's
> `XDG_RUNTIME_DIR` short instead and drop the data-dir workaround — O3's decision.
> **Update (2026-10-12, review finding):** the default fixture still puts `XDG_RUNTIME_DIR` under the long
> `tmp_path`, so any NEW test that binds the socket without O4's per-module fixture will hit the 108-byte
> limit; and `test_sandbox_guard.py` checks the length of `data_dir/service.sock`, which is now only the
> fallback location. Both are O3's files — not changed by O4.

#### M3.4 Render checks
| Field | Spec |
|---|---|
| Purpose | "Drawn font" checks per engine for tests. [D F9; D A14] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M3.3; pango-view, Qt offscreen, headless Brave/Firefox with temp profiles |
| Used by | tests of M1.6, M4.x |
| Provides | `drawn_family(engine, sample) -> str` [P] |
| Exact interface | as above |
| Example | Headless Firefox with temp profile, `--no-remote --new-instance` [D Step 3] |
| Data it reads/writes | temp profiles |
| Data ownership | — |
| Preconditions | Sandbox active |
| Postconditions | No browser windows left; real browser untouched |
| Failure cases + required behaviour | Engine missing → test skipped with reason, not passed |
| Must never | Send anything to the user's running browser |
| Acceptance tests | Patched vs unpatched font distinguished per engine |
| Done when | Tests pass · reviewed |

#### M3.5 VM full-system tests
| Field | Spec |
|---|---|
| Purpose | End-to-end tests: Flatpak overrides, keybinds, relaunch + confirm, login auto-revert. [D F9] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | VM image with Hyprland [V GPU in VM] |
| Used by | release testing |
| Provides | VM image + snapshot reset scripts + test scenarios |
| Exact interface | — |
| Example | Apply → kill app at AWAITING_CONFIRMATION → reboot VM → verify everything reverted |
| Data it reads/writes | VM only |
| Data ownership | — |
| Preconditions | VM boots Hyprland |
| Postconditions | VM reset to snapshot after each run |
| Failure cases + required behaviour | Hyprland won't run in VM → report early [D: verify early] |
| Must never | Run on the development machine |
| Acceptance tests | Each scenario above |
| Done when | Scenarios pass · reviewed |

#### M3.6 Manual checklist
| Field | Spec |
|---|---|
| Purpose | Checks done by hand: keybinds, confirm dialog, screen-reader use (+ keyboard-only, large text). [D F9, F6] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O2 |
| Depends on | — |
| Used by | release testing |
| Provides | checklist document with pass/fail per item |
| Exact interface | — |
| Example | "Press Ctrl+Alt+Shift+Super+Backspace → everything reverts" |
| Data it reads/writes | — |
| Data ownership | — |
| Preconditions | — |
| Postconditions | — |
| Failure cases + required behaviour | Any accessibility item fails → release blocked |
| Must never | Be skipped for a release |
| Acceptance tests | — |
| Done when | Checklist written · reviewed by O2 |

#### M3.7 Doctor
| Field | Spec |
|---|---|
| Purpose | Name every missing dependency with the command to install it. [D F10] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | — |
| Used by | M3.8, user |
| Provides | `scribesense doctor` |
| Exact interface | K10 |
| Example | "qt6ct not found — Qt apps will not be covered (optional)" |
| Data it reads/writes | reads system state |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Required vs optional dependencies distinguished [P] |
| Failure cases + required behaviour | — |
| Must never | Install anything itself |
| Acceptance tests | Each missing dependency reported in the sandbox |
| Done when | Tests pass · reviewed |

#### M3.8 Install and uninstall
| Field | Spec |
|---|---|
| Purpose | Install from repo with `uv`/pip; uninstall with full revert. [D F10] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M3.7, M4.11, M4.14, M1.7 |
| Used by | user |
| Provides | install steps, `scribesense uninstall [--delete-data]` |
| Exact interface | K10 |
| Example | Uninstall: reset (K4 rule 4, steps 1+2) → remove generated fonts **except** those still referenced by unresolved/skipped settings or of unknown use (unless `--remove-fonts-anyway`) [D R2-9] → remove keybind file and include line → remove login check → keep data unless "delete all" |
| Data it reads/writes | keybind file, login check, fonts dir, data dir (only on delete-all) |
| Data ownership | — |
| Preconditions | No tx active |
| Postconditions | Report lists skipped (user-changed) and failed settings, kept fonts, and: "these apps may fall back to another font and their appearance may change" [D R2] |
| Failure cases + required behaviour | Revert of one item fails → continue, report it, offer `reset` |
| Must never | Delete user data without explicit confirmation; overwrite a user's later change [D] |
| Acceptance tests | VM: install → apply → uninstall → every touched file byte-identical to baseline (except skipped ones) |
| Done when | VM test passes · reviewed by O4 |

#### M3.9 CI and import rules
| Field | Spec |
|---|---|
| Purpose | Run tests on every change; enforce which packages may import which. [DR] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | all |
| Used by | everyone |
| Provides | CI pipeline; import test |
| Exact interface | — |
| Example | Import test fails if recovery commands import UI modules [D A2]; core (non-GTK) tests run on Python 3.11 and the dev version; GUI tests on system Python only [D A20] |
| Data it reads/writes | — |
| Data ownership | — |
| Preconditions | Repo exists |
| Postconditions | — |
| Failure cases + required behaviour | Failing test blocks merge |
| Must never | Run tests outside the sandbox |
| Acceptance tests | A deliberate rule violation fails CI |
| Done when | CI green on the skeleton · reviewed |

> **Note (2026-10-12, O4 → O3 to review):** `pyproject.toml` has two pytest markers added by O4 (Step C):
> `real_subprocess` (only for `run_cmd` and lock-takeover tests; may launch only `sys.executable` or files
> in the test's temp dir) and `recovery` (the §9 scenarios). The import rules R1–R6 live in
> `tests/unit/test_import_rules.py`; CI runs in `.github/workflows/tests.yml` (Python 3.11 + 3.14).

### O4 — System Integration & Safety

Helper rules apply to cards marked **helper: yes** (see §8).

#### M4.1 Adapter interface
| Field | Spec |
|---|---|
| Purpose | Define K3 so every adapter behaves the same and helpers can build adapters independently. [D F3] |
| Owner / may be helped? | O4 / **no (O4 only)** [D] |
| Built by / reviewed by | O4 / one other owner |
| Depends on | K1, K7, K8 |
| Used by | M2.1, M4.2–M4.7, M4.11, M4.13 |
| Provides | K3 + a fake adapter for other owners' tests |
| Exact interface | K3 |
| Example | `FakeAdapter(fail_on="verify")` for controller tests |
| Data it reads/writes | — |
| Data ownership | Owns the adapter rules |
| Preconditions | — |
| Postconditions | Frozen before helper work starts [D] |
| Failure cases + required behaviour | — |
| Must never | Change after the freeze without O4 + one other approval |
| Acceptance tests | Conformance test suite every adapter must pass |
| Done when | Frozen · conformance suite published |

#### M4.2 fontconfig adapter
| Field | Spec |
|---|---|
| Purpose | Make fontconfig hand out the generated fonts; the base for every other target. [D F3] |
| Owner / may be helped? | O4 / **no (O4 only)** [D] |
| Built by / reviewed by | O4 / O1 |
| Depends on | M4.1, K7 |
| Used by | M2.1 (applied first) |
| Provides | adapter `fontconfig` |
| Exact interface | K3 |
| Example | Writes `~/.config/fontconfig/conf.d/99-scribesense.conf` [P name]: generic families + named UI fonts → generated families [D A13]; monospace untouched |
| Data it reads/writes | that rule file; current UI font names from gsettings |
| Data ownership | Owns the rule file |
| Preconditions | Fonts installed (M1.7) |
| Postconditions | `fc-match sans-serif` → reading family; named UI font → UI family; `fc-cache` run |
| Failure cases + required behaviour | Any failure → whole apply stops and reverts [D] |
| Must never | Redirect monospace or icon families |
| Acceptance tests | Sandbox: fc-match results as above; revert restores byte-identical state (or no file) |
| Done when | Conformance + tests pass · reviewed |

> **Implemented 2026-10-12 (O4) — notes for reviewers and the integration runner:**
> one target `fontconfig:user`, one file `config_home()/fontconfig/conf.d/99-scribesense.conf`.
> Rules: `sans-serif`, `serif` → reading family; the gsettings `font-name` family → UI family
> (`<match target="pattern">` + `<edit mode="prepend" binding="strong">`). Never redirected: generic
> monospace/emoji/math/system-ui names and their aliases, names that look monospace/icon/emoji (heuristic),
> ScribeSense's own families. UI redirects already in an existing ScribeSense rule file are **kept** on the
> next apply (once the GTK adapter has set `font-name` to our UI family, gsettings no longer names the
> original font). `verify` = chosen-font check with `fc-match` (patterns escaped, e.g. `sans\-serif`);
> plus the Pango drawn check from M4.13 (`drawn_ok` is True/False; see the M4.13 note). Directories created by apply are left in place on revert (only the
> rule file is removed). The integration test needs the bubblewrap runner to expose `/etc/fonts` and system
> fonts read-only. Adapter logs through `scribesense.log` (moved out of `safety/` on 2026-10-12 so R5 still holds).

#### M4.3 GTK adapter
| Field | Spec |
|---|---|
| Purpose | Set GTK fonts and text size. [D F3, Step 3] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.1, M3.3 |
| Used by | M2.1 |
| Provides | adapter `gtk` |
| Exact interface | K3 |
| Example | gsettings `font-name` → UI family · `document-font-name` → reading family · `text-scaling-factor` → `text_scale` [DR]; `monospace-font-name` untouched |
| Data it reads/writes | those gsettings keys |
| Data ownership | — |
| Preconditions | gsettings schema present |
| Postconditions | Keys read back equal what was written; `read_state()` reports unset keys as not present [R2-8] |
| Failure cases + required behaviour | Schema missing → NOT_COVERED |
| Must never | Touch the monospace key |
| Acceptance tests | Conformance; snapshot → plan → apply → revert restores exact values |
| Done when | Conformance + tests pass in sandbox · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `src/scribesense/adapters/gtk.py`, target `gtk:user`, three keys of
> `org.gnome.desktop.interface`: `font-name` → UI family, `document-font-name` → reading family (both keep the
> user's current font size), `text-scaling-factor` → `text_scale`. `monospace-font-name` is never touched.
> Reads with `Gio.Settings.get_user_value()` (R2-8; unset = not present), writes with `gsettings set/reset`
> via `run_cmd`, then reads back (short poll — the settings service may commit a moment later; no match →
> `ADAPTER_COMMAND_FAILED`). Planned text is **canonical GLib GVariant text** (GLib prints 1.1 as
> `1.1000000000000001`); a pure-Python formatter is checked against real GLib for every `text_scale` step.
> Missing schema → `SCHEMA_MISSING` (detect still lists the target, so it shows as NOT_COVERED).
> `verify` = configuration check (effective values name our families); `drawn_ok=None` (GTK drawn check is
> harness-only, A14). Checked read-only against the dev machine's real settings. Integration test (real
> `gsettings` with `GSETTINGS_BACKEND=keyfile`) needs O3's bubblewrap runner.

#### M4.4 Flatpak adapter
| Field | Spec |
|---|---|
| Purpose | Let each Flatpak app see the fontconfig rule. [D F3] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.1, M3.3, M3.5 |
| Used by | M2.1 |
| Provides | adapter `flatpak`, one target per app |
| Exact interface | K3 |
| Example | `flatpak override --user <app-id> --filesystem=xdg-config/fontconfig:ro` [D]; snapshot the app's override file [V location] |
| Data it reads/writes | per-app override file |
| Data ownership | — |
| Preconditions | `flatpak` installed |
| Postconditions | `flatpak run --command=fc-match <id> sans-serif` → reading family [D Step 3] |
| Failure cases + required behaviour | Flatpak missing → no targets (not an error) |
| Must never | Use a global override for all apps [D per-app] |
| Acceptance tests | VM: override applied and reverted; fc-match inside the app |
| Done when | Conformance + VM test pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `adapters/flatpak.py` — every installed app (detected, no fixed list), one
> per-app user override keyfile written directly (exact bytes; `[Context] filesystems=` gains
> `xdg-config/fontconfig:ro`, everything else preserved; GLib keyfile line rules). The global override is never
> touched. Decisions 2026-10-12: **accepted** that our `:ro` entry can narrow an app whose manifest grants write
> access to the fontconfig folder (rare; font managers); **accepted for now** that the user Flatpak dir comes
> from `FLATPAK_USER_DIR` / `XDG_DATA_HOME` / `home()` because K8 has no `data_home()` (known K8 gap).

#### M4.5 Qt adapter
| Field | Spec |
|---|---|
| Purpose | Set Qt fonts through qt6ct when the session already uses it. [D F3] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.1, M3.3 |
| Used by | M2.1 |
| Provides | adapter `qt` |
| Exact interface | K3 |
| Example | `qt6ct.conf` general font → UI family by name [D Step 3]; fixed font untouched [DR] |
| Data it reads/writes | `qt6ct.conf` |
| Data ownership | — |
| Preconditions | Session already sets `QT_QPA_PLATFORMTHEME=qt6ct` [D] |
| Postconditions | Config read back matches |
| Failure cases + required behaviour | Session doesn't use qt6ct → NOT_COVERED, reason shown [D] |
| Must never | Set the session environment variable [D] |
| Acceptance tests | Conformance; not-qt6ct session → NOT_COVERED; menus checked at UI line 1.2 [V system Qt + qt6ct untested] |
| Done when | Conformance + tests pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `adapters/qt.py` — only when the session already uses qt6ct; edits only
> `[Fonts] general=` (UI family, size kept, default 11), never `fixed=`; adds `[Fonts]` if missing; writes through
> symlinks. **[V] in the VM:** the exact Qt 6 `QFont::toString()` field layout, qt6ct's own default size, and that
> the ScribeSense process sees `QT_QPA_PLATFORMTHEME` (a service started without the session environment would
> wrongly report Qt as not covered — O2/O3: start the service from the session).

#### M4.6 Firefox adapter
| Field | Spec |
|---|---|
| Purpose | Restyle web pages in Firefox-family browsers, every profile, incl. Zen (Flatpak). [D F1, F3] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.1, M4.8, M3.3, M3.4 |
| Used by | M2.1 |
| Provides | adapter `firefox`, one target per profile |
| Exact interface | K3 |
| Example | `user.js`: `toolkit.legacyUserProfileCustomizations.stylesheets = true` + `chrome/userContent.css` from M4.8 |
| Data it reads/writes | `profiles.ini`, `user.js`, `chrome/userContent.css` (native and `~/.var/app/…` paths) |
| Data ownership | — |
| Preconditions | Profile found |
| Postconditions | Files read back match; restart needed |
| Failure cases + required behaviour | Browser running → PENDING (user asked to close) [D] |
| Must never | Edit a profile while the browser runs; add letter/word spacing in CSS [D] |
| Acceptance tests | Headless Firefox (temp profile) draws the reading family on a test page with its own font; icons and code blocks unchanged |
| Done when | Conformance + render test pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `adapters/firefox.py` — every profile of native Firefox
> (`~/.mozilla/firefox` and the newer `~/.config/mozilla/firefox`), Flatpak Firefox and Zen (native + Flatpak).
> Managed blocks in `user.js` (the stylesheet pref) and `chrome/userContent.css` (M4.8 CSS); everything else kept.
> Running = the profile's `lock` symlink → apply refuses (`BROWSER_RUNNING`). Decisions 2026-10-12:
> **target ids use a 12-hex hash of the profile path** (`firefox:<root>:<hash>`) so user-chosen profile names never
> reach logs; the display name keeps the readable name; **revert is allowed while the browser runs** (Firefox never
> rewrites these files). Real-browser render check: O3's harness (M3.4).

#### M4.7 Chromium preferences adapter
| Field | Spec |
|---|---|
| Purpose | Set default fonts in Chromium-family browsers, every profile. [D F1, F3] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.1, M3.3, M3.4 |
| Used by | M2.1 |
| Provides | adapter `chromium`, one target per profile |
| Exact interface | K3; `must_be_closed = True` [D] |
| Example | `Preferences` → `webkit.webprefs.fonts.{standard,sansserif}.Zyyy` = reading family [D Step 3] |
| Data it reads/writes | each profile's `Preferences` JSON (Brave/Chrome/Chromium paths [V Flatpak paths]) |
| Data ownership | — |
| Preconditions | Browser closed |
| Postconditions | File still valid JSON; all other keys unchanged |
| Failure cases + required behaviour | Browser running → PENDING · JSON unreadable → NOT_COVERED, file untouched |
| Must never | Write a new minimal Preferences file (hung Brave in tests) [D Step 3]; edit while running |
| Acceptance tests | Edit preserves every other key; headless Brave draws reading family for `sans-serif` |
| Done when | Conformance + render test pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4) — key-level locations (K3 rule 10b, decision option A, finding M4.7-1):**
> `adapters/chromium.py`. Chrome rewrites `Preferences` on every run, so each profile owns **two key
> locations** (`…/Preferences#webkit.webprefs.fonts.standard.Zyyy` and `…sansserif.Zyyy`) whose state is only that
> value. apply/revert change only that key; revert of an absent key deletes it and prunes empty parents; the file
> is never created. Proven end to end: after the browser rewrites the file, `reset` still removes our font and
> keeps the browser's own changes; a user-changed font is skipped (drift), never overwritten. Brave, Chrome,
> Chromium (native + Flatpak Brave/Chromium). Running = `SingletonLock` → apply **and revert** refuse
> (`BROWSER_RUNNING`; decision 2026-10-12 — the browser would overwrite a revert on exit; recovery retries).
> Not yet covered: Chrome Flatpak, Vivaldi, Edge, beta channels (later). VM: Chrome accepts the edit.

#### M4.8 Browser CSS generator
| Field | Spec |
|---|---|
| Purpose | One CSS text for Firefox `userContent.css` and the extension. [D F1] |
| Owner / may be helped? | O4 / **helper: yes** (part of browser layer) |
| Built by / reviewed by | TBD / O4 |
| Depends on | K1, K7 |
| Used by | M4.6, M4.9 |
| Provides | `browser_css(fonts, config) -> str` |
| Exact interface | as above [P] |
| Example | font-family reading family `!important` + `line-height` `!important`; excludes `code, pre, kbd, samp` and icon fonts |
| Data it reads/writes | none (pure) |
| Data ownership | Owns the CSS rules and the icon-exclusion list |
| Preconditions | — |
| Postconditions | No letter-spacing or word-spacing declarations [D] |
| Failure cases + required behaviour | — |
| Must never | Use `@font-face`, `file://`, or any URL [D] |
| Acceptance tests | Output contains font + line-height only; exclusions present; Material Icons/Symbols and Font Awesome pages keep their icons. Coverage is stated as **the tested icon fonts only**, not a guarantee; other pages → reader fallback (per-site disable = Later) [D R2] |
| Done when | Tests pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `src/scribesense/adapters/browser_css.py`, `browser_css(fonts, config)`.
> One rule: `:root :not(<excluded>, <excluded> *) { font-family: "<reading family>", sans-serif !important;
> line-height: <reading_line> !important; }` (Selectors Level 4 lists inside `:not()`). Exclusions are a
> declared heuristic list: code/pre/kbd/samp/tt/var, common code editors (CodeMirror, Monaco, Ace), classes
> containing mono/code/highlight, Font Awesome, Material Icons/Symbols, generic icon classes, svg, math.
> Only an `AccessSans-<key>` family can be emitted; spacing, `@font-face`, `@import`, `url(`, `file:` and
> `http(s):` are refused with an explicit check. **Not yet checked in a real browser** (headless Brave hung on
> the dev machine) — needs O3's render harness (M3.4): parsing of the `:not()` list, icons and code keep
> their fonts.

#### M4.9 Browser extension (JavaScript)
| Field | Spec |
|---|---|
| Purpose | Inject the CSS into Chromium pages; "Open in ScribeSense" right-click. [D F1, F7] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.8 (via K11), M2.10 |
| Used by | user (installs once) [D] |
| Provides | content script, context-menu item |
| Exact interface | K11 [D A6, A16] |
| Example | Right-click selected text → reader opens with it |
| Data it reads/writes | current style from the app; selected text only on right-click |
| Data ownership | — |
| Preconditions | Extension installed; app running |
| Postconditions | — |
| Failure cases + required behaviour | App not running → page unchanged; right-click shows "ScribeSense not running" |
| Must never | Send page content anywhere except the local app, and only on explicit right-click; make network requests |
| Acceptance tests | Headless Brave with extension: test page restyled; right-click message delivered |
| Done when | Tests pass · reviewed by O4 |

> **Implemented 2026-10-12 (O4):** `extension/` (MV3, no libraries, node tests: `node --test
> 'extension/test/*.test.mjs'`) + native-messaging host `src/scribesense/safety/native_host.py` (K11 framing
> bridge to the IPC socket). Disconnected → keeps the last styling (K11/R2-5 overrides this card's older "page
> unchanged"). **Key:** O4 keeps the private key; `extension/tools/set_key.py` inserts only the PUBLIC key and prints
> the ID (README there has the openssl steps); no key is in the repo. **Note for O2 (decision 2026-10-12):** there
> is no service push yet — after Apply/Revert/Original the extension only updates when it reconnects; the service
> (M2.4) must push `style_changed` to the native host for A16's "refreshes after Apply" to hold.
> **Packaging (O3):** exclude `extension/test/`, `extension/tools/` and any `__pycache__` from the packed extension.

#### M4.10 Journal
| Field | Spec |
|---|---|
| Purpose | Record every change before it happens; transaction states; apply lock. [D F4, F5] |
| Owner / may be helped? | O4 / **no (O4 only)** [D] |
| Built by / reviewed by | O4 / O3 |
| Depends on | K3–K5, M3.1 |
| Used by | M2.1, M2.3, M4.11, M4.12, M3.2 |
| Provides | K4 |
| Exact interface | K4 |
| Example | First-ever snapshot of `qt6ct.conf` stored as its baseline |
| Data it reads/writes | tx, journal item, baseline, backup tables |
| Data ownership | **Source of truth** for what ScribeSense changed |
| Preconditions | — |
| Postconditions | Snapshot recorded before any apply for that target |
| Failure cases + required behaviour | Cannot persist → apply refused before any write |
| Must never | Allow two active transactions; allow an illegal state transition |
| Acceptance tests | Lock; illegal transitions refused; kill mid-apply → `unfinished()` returns the tx |
| Done when | Tests pass · reviewed by O3 |

#### M4.11 Revert
| Field | Spec |
|---|---|
| Purpose | Undo one transaction or everything, with the drift rule. [D F4, F10] |
| Owner / may be helped? | O4 / **no (O4 only)** [D] |
| Built by / reviewed by | O4 / O3 |
| Depends on | M4.10, M4.1 |
| Used by | M2.1, M2.3, M4.12, M3.8 |
| Provides | `revert_tx(tx) -> RevertReport` · `revert_all() -> RevertReport` |
| Exact interface | as above [P] |
| Example | User changed GTK font after apply → that key skipped, reported [D] |
| Data it reads/writes | through adapters |
| Data ownership | — |
| Preconditions | No other tx active |
| Postconditions | Every non-skipped location equals its snapshot (tx) or baseline (all) |
| Failure cases + required behaviour | One item fails → continue others, report it |
| Must never | Overwrite a value whose current hash ≠ what we wrote [D] |
| Acceptance tests | revert_all → byte-identical files/keys; drift case skipped and reported |
| Done when | Tests pass in sandbox + VM · reviewed |

#### M4.12 Recovery
| Field | Spec |
|---|---|
| Purpose | `reset` (keybind + command) and auto-revert at login. [D F4] |
| Owner / may be helped? | O4 / **no (O4 only)** [D] |
| Built by / reviewed by | O4 / O2 |
| Depends on | M4.10, M4.11 |
| Used by | keybind, login check [D A3], M2.11 |
| Provides | `reset()` · `recover_at_login()` |
| Exact interface | K10 `reset`, `recover --login` |
| Example | Login after a crash at AWAITING_CONFIRMATION → reverted, notice shown next time the app opens |
| Data it reads/writes | via journal |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Every recoverable item attempted; failures keep what is needed to retry; recovery **never reports complete while anything still needs restoring**. Note: hash check → restore is not atomic against another program writing at the same moment (best effort, as on apply). |
| Failure cases + required behaviour | Revert fails → message + instructions in plain text |
| Must never | Depend on the GUI [D A2]; remove the recovery keybind (only uninstall does) [D] |
| Acceptance tests | VM: crash + reboot scenario; keybind triggers reset |
| Done when | VM scenarios pass · manual checklist item passes |

#### M4.13 Verify
| Field | Spec |
|---|---|
| Purpose | Check which font each target chose and, where possible, which font is drawn. [D Step 3, F3] |
| Owner / may be helped? | O4 / no |
| Built by / reviewed by | O4 / O3 |
| Depends on | M4.1, fc-match, pango-view |
| Used by | adapters' `verify`, M3.4 |
| Provides | `chosen_family(target, query)` · `drawn_family_pango(sample)` |
| Exact interface | as above [P] |
| Example | `flatpak run --command=fc-match <id> sans-serif` |
| Data it reads/writes | — |
| Data ownership | — |
| Preconditions | Target applied |
| Postconditions | — |
| Failure cases + required behaviour | **Updated 2026-10-12 (O4):** chosen check can't run → `chosen_ok=False`, `CHECK_UNAVAILABLE` · drawn check can't run → `chosen_ok` keeps the fc-match result, `drawn_ok=False`, `CHECK_UNAVAILABLE` · **"verify passed" = `chosen_ok and drawn_ok is not False`** (`drawn_ok=None` = no drawn check for that target); a failed verify → that adapter is reverted [D] |
| Must never | Report verified from fc-match alone where a drawn check is required [D A14] |
| Acceptance tests | Distinguishes patched vs original font |
| Done when | Tests pass · reviewed |

> **Implemented 2026-10-12 (O4):** `src/scribesense/adapters/verify.py` (in `adapters/`, not `safety/`, so
> adapters may import it under rule R5).
> - `chosen_family(target, query)` — fc-match on the host, or `flatpak run --command=fc-match <app-id>` for
>   `flatpak:<app-id>` targets (app id validated; patterns escaped).
> - `drawn_family_pango(query)` — runs `python -P -m scribesense.adapters.pango_probe` in a **fresh process**
>   (a long-running process keeps its old fontconfig setup) and returns the families Pango actually drew for
>   a **fixed** Latin sample (never user content). A run with an unidentified font fails the probe (fails closed).
> - Errors: launch failure / empty or bad output → `CHECK_UNAVAILABLE`.
> - **Semantics in VerifyResult (now the card's rule — "Failure cases" updated 2026-10-12):** when the *drawn*
>   check can't run, `chosen_ok` keeps the fc-match result and `drawn_ok=False` with `CHECK_UNAVAILABLE`.
>   "Verify passed" = `chosen_ok and drawn_ok is not False`. The fontconfig adapter fails verify unless both pass.
> - Checked read-only on the dev machine: the real probe reports `Noto Sans` for `sans-serif`, matching fc-match.
>   The integration tests (before/after/revert with generated fonts) still need O3's bubblewrap runner.


#### M4.14 Keybind writer
| Field | Spec |
|---|---|
| Purpose | Write ScribeSense keybinds to a separate file included from the Hyprland config; check conflicts. [D F7, F10; D A18] |
| Owner / may be helped? | O4 (proposed) / no |
| Built by / reviewed by | TBD / O3 |
| Depends on | `hyprctl binds -j` |
| Used by | M3.8, M2.7 |
| Provides | `install_keybinds(binds)`, `remove_keybinds()`, `conflicts(binds)` |
| Exact interface | as above [P] |
| Example | Super+Alt+R, Super+Alt+P, Ctrl+Alt+Shift+Super+Backspace [D] |
| Data it reads/writes | ScribeSense keybind file + one include line in `hyprland.lua` or `hyprland.conf` [D both formats] |
| Data ownership | Owns the keybind file |
| Preconditions | No conflict |
| Postconditions | Include line journaled; survives `reset` [D] |
| Failure cases + required behaviour | Conflict → refuse, name the conflicting bind |
| Must never | Edit other parts of the user's Hyprland config |
| Acceptance tests | Lua and classic configs; conflict detected |
| Done when | Tests pass · VM check |

> **Implemented 2026-10-12 (O4) — decisions D1–D5 + review:** `src/scribesense/safety/keybinds.py`.
> Changeable binds (format-neutral `Keybind(action, mods, key)`; K9 setting strings parsed); a desktop-backend
> interface with one backend, Hyprland (Lua **and** classic). ScribeSense's own file
> `config_home()/scribesense/hyprland.{lua,conf}` holds the binds + the login-recovery hook (A3). One marked,
> guarded include block goes into the rice's user hook (`custom.lua` when `hyprland.lua` loads it — ML4W) or
> else the entry config; in Lua it is inserted before a final top-level `return`. `hyprctl reload` after
> changes; **skipped when Hyprland isn't running** (decision 2026-10-12). Conflict check via `hyprctl binds -j`.
> **"Include line journaled" (postcondition above) is satisfied by the marked block itself (decision
> 2026-10-12):** removal is byte-exact and drift-aware (a hand-edited block is never touched → `DRIFT_SKIPPED`),
> so no K4 journal entry is needed. `reset` never removes keybinds (tested); only uninstall does.
> API for callers (O2 M2.7, O3 M3.8): `install_keybinds(binds, command, *, include_login_hook=True,
> allow_unchecked=False)`, `remove_keybinds()`, `conflicts(binds)`, `default_keybinds()`,
> `parse_keybind(text, action)`, `keybind_status()`. **VM checks:** Lua `dofile`/`pcall` allowed, reload re-runs a
> Lua config, real `hyprctl binds -j` format, `hl.on("hyprland.start")` not stacking on reload.

#### M4.15 Logging
| Field | Spec |
|---|---|
| Purpose | Privacy-safe logs for every module. [D] |
| Owner / may be helped? | O4 / no |
| Built by / reviewed by | O4 / O3 |
| Depends on | K8 |
| Used by | all |
| Provides | `log(code, category, **non_content_fields)` |
| Exact interface | as above [P] |
| Example | `log("ADAPTER_VERIFY_FAILED", "adapter", adapter="qt")` |
| Data it reads/writes | log file in data dir [P] |
| Data ownership | — |
| Preconditions | — |
| Postconditions | — |
| Failure cases + required behaviour | Logging failure never breaks the app |
| Must never | Log text, titles, user document paths |
| Acceptance tests | Canary grep over logs after a full session → zero hits |
| Done when | Tests pass · reviewed |

> **Implemented 2026-10-12 (O4) — how every module must call it:**
> `from scribesense.log import log` (top-level module, so adapters may use it without breaking R5) · `log(ErrorCode.X, "category", field=value, ...)`
> (`code` and `category` are positional). Numbers, booleans, `None` and ErrorCodes are allowed in any field.
> **Strings are accepted only in identifier fields** — `adapter, target, role, state, step, setting,
> preset, kind, reason` — and only if identifier-shaped (≤ 64 chars, no spaces, no `/`). Any other string is
> dropped, never written. Never put user content in an identifier field. Logging never raises.
> File: `logs_dir()/scribesense.log` (0600, dir 0700), rotated at 1 MiB, one old file kept.
---

## 8. Helper work model [D]

| Rule | Detail |
|---|---|
| Helper cards | M4.3 GTK · M4.4 Flatpak · M4.5 Qt · M4.6 Firefox · M4.7 Chromium · M4.8 CSS · M4.9 extension |
| O4-only cards | M4.1 interface · M4.2 fontconfig · M4.10 journal · M4.11 revert · M4.12 recovery |
| Before helping | Contracts frozen · M3.3 sandbox ready · helper's own area tested and reviewed |
| Claiming | GitHub issue per card; one card per person at a time |
| Review | O4 reviews every helper card |
| Attribution | "Built by / reviewed by" filled in on the card |

---

## 9. Dependency order (what must exist before what — not a schedule)

1. **Contract session:** K1–K8, K10, K11 frozen 2026-10-09 · **K9 pending O3 review** · A1–A20 ✅ decided 2026-10-08 (§3).
2. **Foundations:** M1.1 · M3.3 sandbox · M3.1 store · M4.1 interface + fake adapter · M3.9 CI.
   **Recovery slice first [D R2]** — one fake adapter + one temp file + the journal must pass, before any real adapter:
   1. keep A → apply B → revert B → reset → original;
   2. keep A → write B → crash before Keep → reset → B recovered, then original;
   3. one adapter rolls back, others kept → that location's `managed` unchanged;
   4. prune history past retention → baseline still restorable;
   5. crash after `keep()` commits, before notifying the extension → reconnect gives the correct state;
   6. service stopped → standalone reset → extension local Disable / next connect shows Original;
   7. user edits a managed setting → reset skips and reports it; uninstall states kept fonts and consequences.
   (5 and 6 run once the extension exists.)
3. **Parallel cores:** O1 M1.2–M1.8 · O2 M2.1–M2.6 against fake adapters · O3 M3.2, M3.4, M3.7 · O4 M4.2, M4.10, M4.11, M4.13, M4.15.
4. **First end-to-end slice:** preset → build → fontconfig + GTK → relaunch confirm → revert.
5. **Fan-out:** remaining adapters (owners + helpers) · reader + capture · extension · keybinds · recovery.
6. **Hardening:** VM scenarios · manual checklist · install/uninstall · failure paths.
