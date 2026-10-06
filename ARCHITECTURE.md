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
| 13 | Confirm → COMPLETED · timeout/close → revert | M2.3 → M4.11 | K4, K5 |
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

## 3. Open architecture decisions

Each has a proposed default **[P]** so work is not blocked, but the team must confirm.

| # | Question | Proposed default | Affects |
|---|---|---|---|
| A1 | How does a process stay alive for the session (needed for session-only reader text and keybind actions)? | The GUI app is single-instance and stays running in the background | M2.4, M2.9, M2.11 |
| A2 | Must the recovery path avoid loading GUI code? | Yes — `reset`, login recovery and uninstall import no UI modules (follows the "screen may be unreadable" rule) | M4.12, M3.9 |
| A3 | Login auto-revert mechanism | Hyprland `exec-once` running the recovery command (alternative: systemd user service) | M4.12, M3.8 |
| A4 | Libraries | fontTools (fonts) · PyGObject with GTK4 + libadwaita (UI) · sqlite3 (store) · pytest (tests) | all |
| A5 | How the preview shows a font that is not activated yet | [V] load the generated file directly into the preview widget; fallback: render an image with `pango-view` | M2.5 |
| A6 | Extension: manifest version and channel to the app | Manifest V3 [V]; native messaging to the running app | M4.9, M2.10 |
| A7 | Who owns `cli.py` | O2 | M2.11 |
| A8 | Database tables | as listed in M3.1 | M3.1 |
| A9 | Default preset says word spacing "≈ 0.16 em", but the unit is "× space width" (font-dependent) | Convert per base font at build time (store the ×-value that equals +0.16 em for that font) | M1.2, M1.4 |
| A10 | Presets don't state base font, UI line height or text size | Noto Sans · UI line 1.2× · text size 1.0 | M1.2 |
| A11 | `text_scale` range and step | 1.0–2.0, step 0.05 | M1.1, M4.3 |
| A12 | Absolute bounds for UI line height (warning above 1.3× is decided) | 1.0–2.0, step 0.1 | M1.1 |
| A13 | Which font goes where: reading vs UI variant for generic families (`sans-serif`, `serif`) and named UI fonts | generic families → reading variant · named UI fonts (from gsettings `font-name`) → UI variant | M4.2 |
| A14 | Which "drawn font" checks run in production vs only in the test harness | Production: chosen-font check per target + Pango drawn check; Qt and browser drawn checks harness-only | M4.13, M3.4 |
| A15 | Who enforces the 30 s timeout if the relaunched window never appears or hangs | The process that started the apply keeps a watchdog until the new window confirms it is alive | M2.3 |
| A16 | How the extension learns the current font name and line height | Asks the running app over the same native-messaging channel | M4.9, M4.8 |
| A17 | Reader text size limit | 200 000 characters, with an explicit message | M2.10 |
| A18 | Who owns the Hyprland keybind writer (used by install and recovery) | O4 (safety) | M4.14, M3.8 |
| A19 | Names of generated families and files | `AccessSans-<key>` (reading, decided name) and `AccessSansUI-<key>` (UI, proposed) | M1.5 |
| A20 | Minimum Python version | 3.11 (carried over from the old plan, not re-confirmed) | M3.7 |

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
| Testing | No test touches the real desktop. [D] |

---

## 5. Shared contracts

### K1 — Configuration (owner O1)

```python
@dataclass(frozen=True)
class Configuration:
    base_family: str     # one of SHORTLIST
    letter_em: float     # 0.00–0.30, step 0.02             [D]
    word_scale: float    # 1.0–3.0 × space width, step 0.25  [D]
    reading_line: float  # 1.0–2.0 ×, step 0.1               [D]
    ui_line: float       # recommended 1.2–1.3 ×, warn above [D]; bounds [O A12]
    text_scale: float    # GTK text-scaling-factor           [O A11]

SHORTLIST = ("Carlito", "Atkinson Hyperlegible", "Lexend", "Noto Sans")   # [D]
RANGES: dict[str, Range]          # single source of truth for UI controls and validation

@dataclass(frozen=True)
class Issue:
    level: Literal["error", "warning"]
    field: str
    code: str                     # e.g. "UI_LINE_ABOVE_RECOMMENDED"

def validate(c: Configuration) -> list[Issue]
def round_to_steps(c: Configuration) -> Configuration
```

### K2 — Preset (owner O1)

```python
@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    config: Configuration
    kind: Literal["builtin", "user", "custom"]

BUILTINS: tuple[Preset, ...]      # Default, Reading, High Separation   [D]
ORIGINAL = "original"             # sentinel: revert everything          [D]
```

| Built-in | letter / word / reading line [D, values draft] |
|---|---|
| Default | 0.12 em / ≈ +0.16 em [O A9] / 1.5× |
| Reading | 0.12 em / 1.5× / 1.8× |
| High Separation | 0.20 em / 2.5× / 1.6× |

### K3 — Adapter (owner O4, O4-only)

```python
@dataclass(frozen=True)
class Target:
    adapter: str          # "fontconfig", "gtk", "flatpak", "qt", "firefox", "chromium"
    target_id: str        # e.g. "flatpak:app.zen_browser.zen", "brave:Default"
    display_name: str     # shown in the coverage report
    running: bool         # needed for must_be_closed and the restart notice

@dataclass(frozen=True)
class SnapshotItem:
    target: Target
    kind: Literal["file", "key"]
    location: str         # file path, or "schema key" for gsettings
    existed: bool
    old_value: str | None # key value; for files None (content stored as a backup, see K4)
    old_sha256: str | None

@dataclass(frozen=True)
class AppliedItem:
    target: Target
    location: str
    new_sha256: str       # what we wrote — used later for the drift rule

@dataclass(frozen=True)
class VerifyResult:
    target: Target
    chosen_ok: bool       # the app/config resolves to our font
    drawn_ok: bool | None # None = no drawn check for this target [O A14]
    detail_code: str

class Adapter(Protocol):
    name: str
    requires_restart: bool
    must_be_closed: bool
    def detect(self) -> list[Target]: ...
    def snapshot(self, target: Target) -> list[SnapshotItem]: ...        # reads only
    def apply(self, target: Target, fonts: FontSet,
              config: Configuration) -> list[AppliedItem]: ...
    def verify(self, target: Target, fonts: FontSet) -> VerifyResult: ...
    def revert(self, item: SnapshotItem) -> None: ...                    # restore one item
```

**Rules every adapter follows [D/DR]:**
1. `snapshot` writes nothing; its items are journaled **before** `apply` is called.
2. `apply` writes only locations returned by `snapshot` for that target.
3. The drift check is **not** done by adapters — M4.11 compares the current value with
   `AppliedItem.new_sha256` before calling `revert`.
4. All paths come from K8.
5. Errors are raised as K8 error codes; adapters never show UI.

### K4 — Journal (owner O4, O4-only)

```python
class Journal:
    def begin(self, kind: Literal["apply", "revert", "uninstall"],
              preset_id: str | None) -> TxId           # raises APPLY_IN_PROGRESS if a tx is active (lock)
    def set_state(self, tx: TxId, state: TxState) -> None  # only allowed transitions (K5)
    def record_snapshot(self, tx: TxId, item: SnapshotItem,
                        file_backup: bytes | None) -> None  # stores baseline if first ever for location
    def record_applied(self, tx: TxId, item: AppliedItem) -> None
    def record_coverage(self, tx: TxId, target: Target,
                        status: CoverageStatus, detail_code: str) -> None
    def unfinished(self) -> list[TxId]                 # state not in {COMPLETED, REVERTED, FAILED}
    def items(self, tx: TxId) -> list[tuple[SnapshotItem, AppliedItem | None]]
    def baseline(self, location: str) -> SnapshotItem | None
```

- **Baseline rule [D]:** the first snapshot ever taken of a location is kept permanently.
- **Retention [D]:** completed transactions pruned after 30 days, always keeping the last 10
  (done by M3.2); baselines never pruned.

### K5 — Transaction states (shared, defined by O4)

States [D]: `CREATED, SNAPSHOTTED, APPLYING, VERIFYING, AWAITING_CONFIRMATION, CONFIRMED,
REVERTING, COMPLETED, FAILED, REVERTED`.

Allowed transitions [DR]:

| From | To |
|---|---|
| CREATED | SNAPSHOTTED · FAILED (nothing written) |
| SNAPSHOTTED | APPLYING · FAILED (nothing written) |
| APPLYING | VERIFYING · REVERTING |
| VERIFYING | AWAITING_CONFIRMATION · REVERTING |
| AWAITING_CONFIRMATION | CONFIRMED · REVERTING |
| CONFIRMED | COMPLETED |
| REVERTING | REVERTED · FAILED (revert itself failed → user told, recovery command offered) |

### K6 — Coverage status (shared, defined by O2)

`COVERED` · `NOT_COVERED` (app keeps its existing font; text can go to the reader) ·
`PENDING` (app must be closed and wasn't) · `RESTART_NEEDED`. [D]
Each comes with a `detail_code` (K8).

### K7 — Font generator API (owner O1)

```python
@dataclass(frozen=True)
class FontSet:
    key: str                      # content key (M1.5)
    reading_family: str           # e.g. "AccessSans-3f9a1c2b7d10"
    ui_family: str                # [O A19]
    files: tuple[str, ...]        # installed file paths

@dataclass(frozen=True)
class FontBuildResult:
    status: Literal["built", "cached", "refused", "failed"]
    fonts: FontSet | None         # None unless built/cached
    reason_code: str | None       # e.g. "BASE_IS_MONOSPACE", "VALIDATION_FAILED"

def build(config: Configuration) -> FontBuildResult
```

### K8 — Paths and error codes (shared, defined by O3)

```python
def data_dir() -> Path        # ~/.local/share/scribesense
def fonts_dir() -> Path       # ~/.local/share/fonts/scribesense
def config_home() -> Path     # $XDG_CONFIG_HOME or ~/.config
def home() -> Path
class ErrorCode(StrEnum): ... # one list for logs, reports and UI messages
```
All functions honour environment overrides so the test sandbox (M3.3) can redirect them.

### K9 — Store repositories (owner O3)

One repository per table in M3.1, typed with K1–K7 objects. Only the store module touches
SQLite. Exact methods frozen in the contract session. [P]

### K10 — CLI commands (owner [O A7], proposed O2)

| Command | Used by |
|---|---|
| `scribesense` | opens / focuses the app |
| `scribesense apply <preset-id>` | UI, scripts |
| `scribesense preset next` | keybind Super+Alt+P [D] |
| `scribesense read --selection` | keybind Super+Alt+R [D] |
| `scribesense read --clipboard` | UI |
| `scribesense reset` | recovery keybind Ctrl+Alt+Shift+Super+Backspace [D] |
| `scribesense recover --login` | login check [O A3] |
| `scribesense uninstall [--delete-data]` | user |
| `scribesense doctor` | user, install |

### K11 — Extension ↔ app messages (owner O4 with O2) [O A6, A16]

```json
{"type": "open_in_reader", "text": "<selected text>"}
{"type": "get_style"} -> {"family": "AccessSans-<key>", "line_height": 1.8}
```

---

## 6. Integration seams (how modules link)

| From → To | Contract | Handshake rule |
|---|---|---|
| UI (M2.5) → Config (M1.1) | K1 | UI builds controls only from `RANGES`; shows `validate()` warnings, blocks on errors |
| Controller (M2.1) → Generator (M1.8) | K7 | Apply never starts unless status is `built` or `cached` |
| Controller → Journal (M4.10) | K4, K5 | Controller opens the tx and moves every state; journal refuses illegal transitions |
| Controller → Adapters (M4.x) | K3 | Order per target: snapshot → `record_snapshot` → apply → `record_applied` → verify |
| Adapters → Journal | K3, K4 | Adapters return items; only the controller records them (adapters never call the store) |
| Journal → Store (M3.1) | K9 | Journal is the only writer of tx/journal/baseline/backup tables |
| Revert (M4.11) → Adapters | K3 | Revert checks drift first, then calls `adapter.revert(item)` |
| Confirm (M2.3) → Revert | K4 | Timeout or close → `revert_tx(tx)` |
| Recovery (M4.12) → Journal + Revert | K4 | At login: every `unfinished()` tx is reverted |
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
| Example | `BUILTINS[1]` → `Preset("reading","Reading", Configuration(…, letter_em=0.12, word_scale=1.5, reading_line=1.8), "builtin")` |
| Data it reads/writes | none (persistence by M3.1) |
| Data ownership | Owns built-in values; user presets are owned by the user, stored by M3.1 |
| Preconditions | — |
| Postconditions | Every built-in passes `validate()` with no errors |
| Failure cases + required behaviour | — (static data) |
| Must never | Let a user preset overwrite a built-in |
| Acceptance tests | Built-ins validate; Original is not a Configuration; draft values match K2 table |
| Done when | Tests pass · A9/A10 resolved · reviewed |

#### M1.3 Font eligibility rules
| Field | Spec |
|---|---|
| Purpose | Decide whether a font may be patched and which glyphs get spacing. [D Step 4, F2] |
| Owner / may be helped? | O1 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | M1.1, fontTools [P A4] |
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
| Depends on | M1.1, M1.3, fontTools [P A4] |
| Used by | M1.5 |
| Provides | `patch(source_path, config, line: float, out_path) -> None` |
| Exact interface | as above [P] |
| Example | Noto Sans Regular + letter 0.12, word 1.5×, line 1.5 → file whose Latin advances are +0.12 em, space ×1.5, line height 1.5× |
| Data it reads/writes | reads base font file; writes one output file |
| Data ownership | — |
| Preconditions | Source eligible (M1.3) |
| Postconditions | Only Latin glyphs changed; ligature features removed; new family name set |
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
| Example | key = sha256(source hash + rounded values + ligature setting + generator version)[:12] [D Step 2] → `AccessSans-3f9a1c2b7d10` |
| Data it reads/writes | writes into a temporary directory |
| Data ownership | Owns the key formula and family names [O A19] |
| Preconditions | Config rounded (M1.1) |
| Postconditions | Reading variant uses `reading_line`; UI variant uses `ui_line`; Regular/Bold/Italic/BoldItalic each |
| Failure cases + required behaviour | Missing style in the base font → [P] build the styles that exist and report the rest |
| Must never | Produce two different outputs for the same inputs |
| Acceptance tests | Same inputs → identical key and identical bytes; changed value → new key |
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
| Failure cases + required behaviour | Build fails → stop before writing · fontconfig fails → stop + revert · other adapter fails → continue, NOT_COVERED · verify fails → revert that adapter only · must-be-closed declined → PENDING [D] |
| Must never | Write directly; skip a snapshot; close or restart an app |
| Acceptance tests | One test per failure row above, with fake adapters |
| Done when | Tests pass with fakes and in the sandbox · reviewed by O4 |

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
| Purpose | Relaunch the ScribeSense window with the new fonts and run the 30 s keep-or-revert. [D F4, F5] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K4, K5, M4.11 |
| Used by | M2.1 |
| Provides | `await_confirmation(tx)` |
| Exact interface | as above [P] |
| Example | New window: "Can you read this? Keep (30)…"; no answer → revert |
| Data it reads/writes | tx state via K4 |
| Data ownership | — |
| Preconditions | Tx in AWAITING_CONFIRMATION |
| Postconditions | Tx COMPLETED or REVERTED |
| Failure cases + required behaviour | Window never appears/hangs → watchdog reverts [O A15] · crash/reboot → login recovery [D] |
| Must never | Count the timeout before the relaunched window is visible |
| Acceptance tests | Confirm → COMPLETED; timeout → REVERTED; killed mid-countdown → recovered at next login (VM) |
| Done when | Tests pass · manual checklist item passes · reviewed by O4 |

#### M2.4 UI shell
| Field | Spec |
|---|---|
| Purpose | Application window, navigation, single instance, background lifetime. [D F6; O A1] |
| Owner / may be helped? | O2 / no |
| Built by / reviewed by | TBD / O3 |
| Depends on | GTK4 + libadwaita [P A4] |
| Used by | M2.5–M2.9 |
| Provides | window, pages, action entry points for M2.11 |
| Exact interface | [P] actions: `apply`, `preset-next`, `open-reader` |
| Example | Second launch focuses the existing window |
| Data it reads/writes | none directly |
| Data ownership | — |
| Preconditions | — |
| Postconditions | — |
| Failure cases + required behaviour | Startup error → message + recovery info, never a blank window |
| Must never | Be required by the recovery path [O A2] |
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
| Postconditions | Delete-all only after explicit confirmation [D] |
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
| Purpose | Bring text into the reader: paste, clipboard, selection keybind, extension right-click. [D F7] |
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
| Failure cases + required behaviour | Empty selection → "nothing selected", never fall back to a whole document · too large → explicit message [O A17] |
| Must never | Capture without an explicit user action; log text |
| Acceptance tests | Each way in works; empty and oversize cases give distinct messages |
| Done when | Tests pass · reviewed by O4 |

#### M2.11 CLI entry points
| Field | Spec |
|---|---|
| Purpose | Commands used by keybinds, install, recovery and the user. [O A7] |
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
| Must never | Load UI code for `reset`, `recover`, `uninstall`, `doctor` [O A2] |
| Acceptance tests | Each command; import test proves recovery commands load no UI modules |
| Done when | Tests pass · reviewed |

### O3 — Data & Platform

#### M3.1 Store
| Field | Spec |
|---|---|
| Purpose | Local persistence for one user. [D F8] |
| Owner / may be helped? | O3 / no |
| Built by / reviewed by | TBD / O4 |
| Depends on | K1–K8, sqlite3 [P A4] |
| Used by | M1.8, M2.5–M2.7, M3.2, M4.10 |
| Provides | K9 repositories, schema, migrations |
| Exact interface | K9 [P] |
| Example | Tables [O A8]: `presets`, `active_preset`, `font_registry`, `transactions`, `journal_items`, `baselines`, `backups`, `coverage`, `settings`, `recommendation_samples` |
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
| Purpose | Prune old transactions and backups. [D F8] |
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
| Postconditions | Baselines untouched [D] |
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
| Provides | pytest fixture: temporary HOME + XDG dirs + private fontconfig |
| Exact interface | `sandbox` fixture [P] |
| Example | Adapter test writes `~/.config/...` → lands in a temp dir |
| Data it reads/writes | temp dirs only |
| Data ownership | — |
| Preconditions | — |
| Postconditions | Temp dirs removed after each test |
| Failure cases + required behaviour | Detects a write outside the sandbox → test fails |
| Must never | Let a test reach the real home directory |
| Acceptance tests | A deliberate escape attempt fails the test |
| Done when | Tests pass · reviewed · announced as ready to helpers |

#### M3.4 Render checks
| Field | Spec |
|---|---|
| Purpose | "Drawn font" checks per engine for tests. [D F9; O A14] |
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
| Example | Uninstall: revert all (drift rule) → remove generated fonts → remove keybind file and include line → remove login check → keep data unless "delete all" |
| Data it reads/writes | keybind file, login check, fonts dir, data dir (only on delete-all) |
| Data ownership | — |
| Preconditions | No tx active |
| Postconditions | Report lists skipped (user-changed) settings and any app that falls back to its default font [D] |
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
| Example | Import test fails if recovery commands import UI modules [O A2] |
| Data it reads/writes | — |
| Data ownership | — |
| Preconditions | Repo exists |
| Postconditions | — |
| Failure cases + required behaviour | Failing test blocks merge |
| Must never | Run tests outside the sandbox |
| Acceptance tests | A deliberate rule violation fails CI |
| Done when | CI green on the skeleton · reviewed |

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
| Example | Writes `~/.config/fontconfig/conf.d/99-scribesense.conf` [P name]: generic families + named UI fonts → generated families [O A13]; monospace untouched |
| Data it reads/writes | that rule file; current UI font names from gsettings |
| Data ownership | Owns the rule file |
| Preconditions | Fonts installed (M1.7) |
| Postconditions | `fc-match sans-serif` → reading family; named UI font → UI family; `fc-cache` run |
| Failure cases + required behaviour | Any failure → whole apply stops and reverts [D] |
| Must never | Redirect monospace or icon families |
| Acceptance tests | Sandbox: fc-match results as above; revert restores byte-identical state (or no file) |
| Done when | Conformance + tests pass · reviewed |

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
| Postconditions | Keys read back equal what was written |
| Failure cases + required behaviour | Schema missing → NOT_COVERED |
| Must never | Touch the monospace key |
| Acceptance tests | Conformance; snapshot → apply → revert restores exact values |
| Done when | Conformance + tests pass in sandbox · reviewed by O4 |

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
| Acceptance tests | Output contains font + line-height only; exclusions present; Material Icons/Symbols and Font Awesome pages keep their icons |
| Done when | Tests pass · reviewed by O4 |

#### M4.9 Browser extension (JavaScript)
| Field | Spec |
|---|---|
| Purpose | Inject the CSS into Chromium pages; "Open in ScribeSense" right-click. [D F1, F7] |
| Owner / may be helped? | O4 / **helper: yes** [D] |
| Built by / reviewed by | TBD / O4 |
| Depends on | M4.8 (via K11), M2.10 |
| Used by | user (installs once) [D] |
| Provides | content script, context-menu item |
| Exact interface | K11 [O A6, A16] |
| Example | Right-click selected text → reader opens with it |
| Data it reads/writes | current style from the app; selected text only on right-click |
| Data ownership | — |
| Preconditions | Extension installed; app running |
| Postconditions | — |
| Failure cases + required behaviour | App not running → page unchanged; right-click shows "ScribeSense not running" |
| Must never | Send page content anywhere except the local app, and only on explicit right-click; make network requests |
| Acceptance tests | Headless Brave with extension: test page restyled; right-click message delivered |
| Done when | Tests pass · reviewed by O4 |

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
| Used by | keybind, login check [O A3], M2.11 |
| Provides | `reset()` · `recover_at_login()` |
| Exact interface | K10 `reset`, `recover --login` |
| Example | Login after a crash at AWAITING_CONFIRMATION → reverted, notice shown next time the app opens |
| Data it reads/writes | via journal |
| Data ownership | — |
| Preconditions | — |
| Postconditions | No unfinished transactions remain |
| Failure cases + required behaviour | Revert fails → message + instructions in plain text |
| Must never | Depend on the GUI [O A2]; remove the recovery keybind (only uninstall does) [D] |
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
| Failure cases + required behaviour | Check can't run → `chosen_ok=False`, adapter reverted [D] |
| Must never | Report verified from fc-match alone where a drawn check is required [O A14] |
| Acceptance tests | Distinguishes patched vs original font |
| Done when | Tests pass · reviewed |

#### M4.14 Keybind writer
| Field | Spec |
|---|---|
| Purpose | Write ScribeSense keybinds to a separate file included from the Hyprland config; check conflicts. [D F7, F10; O A18] |
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

1. **Contract session (all four):** K1–K11 frozen; open items A1–A20 decided or accepted as proposed.
2. **Foundations:** M1.1 · M3.3 sandbox · M3.1 store · M4.1 interface + fake adapter · M3.9 CI.
3. **Parallel cores:** O1 M1.2–M1.8 · O2 M2.1–M2.6 against fake adapters · O3 M3.2, M3.4, M3.7 · O4 M4.2, M4.10, M4.11, M4.13, M4.15.
4. **First end-to-end slice:** preset → build → fontconfig + GTK → relaunch confirm → revert.
5. **Fan-out:** remaining adapters (owners + helpers) · reader + capture · extension · keybinds · recovery.
6. **Hardening:** VM scenarios · manual checklist · install/uninstall · failure paths.
