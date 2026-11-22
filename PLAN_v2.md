# ScribeSense — Plan v2 (living document)

Supersedes the product definition in `MENTOR_PROPOSAL.html` and `PHASE_PLAN.md`.

> **This file is a history log.** Later entries override earlier ones (e.g. Flatpak became a
> per-app override; presets were renamed Default / Reading / High Separation). The **current
> plan** is `ARCHITECTURE.md`; the visual overview is `ScribeSense_Architecture.html`.
Updated one step at a time. Each step records **Decisions**, **Findings**, and **Open**.

Legend: ✅ verified on dev machine · ⚠️ partial · ❌ not possible · ? untested

---

## Step 1 — What we are building

**Decision (confirmed by team):**

> ScribeSense is a **system-wide reading accessibility tool for Linux**. The user picks a
> configuration (font, size, line / word / letter spacing) and ScribeSense applies it across the
> desktop — browsers, GTK/GNOME apps, and other apps where possible. Configurations are stored.
> **Fallback:** where an app can't be changed, the user selects text and views it in ScribeSense's
> pop-up reader (paste, or right-click → "View in ScribeSense").
> **Phase 3:** a recommender suggests configurations; the user can edit a suggestion from the
> notification, and the recommender remembers the edit.

**Why we pivoted (user research, team's informal conversations — a few people, preference only):**
- They read mostly in **web browsers**, often on phones.
- **Line spacing** alone helped most.
- Font choice mattered; Calibri was preferred over OpenDyslexic.

**Consequences:**
- Conflicts with both old documents, which explicitly exclude modifying other applications.
  The old Reading View becomes the **fallback**, not the core.
- The mentor approved the old version → the change must be taken to the mentor.

**Parked (not discussed yet):** voice assistant / TTS.

---

## Step 2 — What can actually be changed system-wide

**Findings** (Fedora 44, Hyprland, GTK 4.22, Pango 1.57):

| Setting | Browser | GTK/GNOME | Terminal (Ptyxis) | Qt / LibreOffice / Electron | Canvas (Google Docs, PDF) |
|---|---|---|---|---|---|
| Font | ✅ | ✅ | ✅ | ? (not render-tested — see Step 3) | ❌ |
| Size | ? | ? (✅ in Step 3) | ? | ? | ❌ |
| Letter spacing | ✅ patched font | ✅ patched font | ⚠️ cell width | ? | ❌ |
| Word spacing | ✅ patched font | ✅ patched font only | ❌ fixed grid | ? | ❌ |
| Line spacing | ✅ CSS | ✅ GTK4 CSS; GTK3 font metrics only | ✅ cell height | ? | ❌ |

Supporting facts:
- ✅ Patched font (wider advances + wider space glyph + raised line metrics) works across Pango apps.
- ✅ GTK3 CSS: letter-spacing only. GTK4 CSS: letter-spacing + line-height. Neither has word-spacing.
- ✅ Running apps don't pick up new fonts; relaunched apps do. **Restart is acceptable.**
- ✅ fontconfig cache uses whole-second mtimes → always run `fc-cache <dir>` after writing.
- ✅ UI font is set *by name* (Fira Sans), separately from `sans-serif` (Noto Sans) → both must be redirected.
- ✅ No GNOME settings daemon on Hyprland → no automatic font-reload signal.
- ? Flatpak apps may not see user fontconfig rules.

**Decisions:**
- **Architecture shape:** one core mechanism + per-app adapters + fallback.
  - Core: a generated, renamed **patched font** activated via fontconfig → carries letter + word spacing.
  - Adapters: line height per app class (browser CSS, GTK4 CSS, terminal settings).
  - Fallback: pop-up reader for canvas apps and anything that fails.
- **No double spacing:** CSS layers set font family + line-height only, never extra letter/word spacing.
- Fonts are **files on disk** (fontconfig requirement), cached by a content key; SQLite holds metadata only.
- Recommender output is **rounded to fixed steps** so the font cache stays bounded.
- A "preset" is just a named configuration (e.g. `wcag-baseline`, `wide`, `extra-wide`).

**Flagged / open:**
1. Units changed: word spacing is now "× space width", not `em`; step sizes differ from
   PHASE_PLAN → the shared settings vocabulary must be redefined.
2. **Recovery is critical:** a broken font can make the desktop unreadable for the very users we
   serve. Pre-activation validation (`pango-view` measure) helps; a guaranteed revert is still needed.
3. Line height is applied in two places (font metrics and CSS) → need one rule for which wins;
   inflated metrics enlarge UI chrome (menus, buttons, cursor).
4. Base fonts must permit modification (e.g. SIL OFL, renamed). Calibri is out; Carlito is a
   candidate — licence to be checked.
5. **Parked as MVP scope creep:** multi-user privileged font store (`/usr/local`, polkit). MVP
   uses per-user `~/.local/share/fonts/`.
6. Generator fixes needed before real use: ligatures off; letter spacing on Latin only
   (Devanagari breaks); CFF outline shifting; variable fonts flattened per weight.

---

## Step 3 — Real-app checks

Test font: "AccessTest" (Noto Sans, letter +0.08 em, word 2.0×, line 1.8×). All changes reverted.

**Findings — per app:**

| App / layer | Result | How it is reached |
|---|---|---|
| GTK3 / GTK4 | ✅ GTK4 at line 1.8 — no clipping (button 24→36 px). GTK3 screenshots ? | gsettings font keys |
| Size | ✅ live in GTK3 + GTK4 (96→120 dpi) | `text-scaling-factor` |
| LibreOffice | ✅ all three spacings render in documents | fontconfig |
| Qt main windows | ✅ widgets grow (26→45 px) | qt6ct font by name |
| Qt menus | ❌ text truncated — caused **only** by raised line height | — |
| Brave (Chromium) | ✅ by name/rule; generic `sans-serif` only after Preferences edit | `webkit.webprefs.fonts.*` edited while closed |
| Zen (Firefox engine) | ✅ nothing engine-specific needed | Flatpak rule |
| Flatpak apps | ✅ see host fonts; ignore host fontconfig rules unless overridden | `flatpak override --user --filesystem=xdg-config/fontconfig:ro` |
| Stubborn apps (Qt) | ✅ "shadow font" (patched font keeps original name) works | opt-in shadow mode |

**Findings — corrections to earlier notes:**
- fontconfig *does* redirect named fonts; **Qt** skips the substitution for installed families → set the font by name directly.
- Chromium defaults are built in, not snapshotted at first run.
- Qt menu truncation is real on Wayland, not a test artifact.
- `fc-match` checks which font is *chosen*, not what is *drawn* → verification needs **both** fc-match and a rendered check per engine (Pango, Qt, Chromium).

**Decisions:**
- **Two font variants per configuration:**
  - **Reading variant** — full line height, used for documents/web content.
  - **UI variant** — line height capped (~1.2–1.3×), full letter + word spacing, used for menus/chrome.
  - (GTK already separates `font-name` vs `document-font-name`, which fits this.)
- Size is handled by `text-scaling-factor`, not by the font.
- Flatpak handled once via one user override, not per-app copies.
- Shadow mode is **opt-in only**.

**Flagged:**
1. **ScribeSense now writes into many other programs' configs:** gsettings, fontconfig, Flatpak
   overrides, qt6ct, Chromium Preferences. Every one must be **snapshotted and reversible** — the
   test agent had to revert them by hand. This is the core safety requirement.
2. Editing Chromium Preferences is fragile (must be closed; a malformed file hung Brave) and
   browser-version dependent.
3. Shadow mode is the riskiest path: licences (Reserved Font Names) and recovery (system believes
   it is the original font).
4. GTK4 line height works via font metrics alone → is the GTK4 CSS line-height adapter still needed?

**Still unknown:** system Qt 6.11 + qt6ct menus · a real Electron app · GTK3 screenshots ·
shadow mode inside Flatpak · Chromium Flatpak profile paths · apps with embedded font config.

---

## Step 4 — System parts (DRAFT, under review)

Agreed so far:
1. Configuration (settings vocabulary + units, redefined)
2. Font generator (reading + UI variants, validate, store)
3. Per-app writers (GTK, Qt, Chromium, Flatpak, fontconfig)
4. Snapshot & revert
5. Verification (chosen font + drawn font)
6. Fallback reader (pop-up)
7. Store (configs, fonts, change records)

Proposed additions (pending team review):
8. Orchestrator — runs build → verify → apply → restart prompt → keep-or-revert
9. App discovery — detects installed apps, browsers, profiles, Flatpaks → decides which writers run
10. Restart manager — tells the user which apps need restarting; never kills apps itself
11. Recovery path outside the GUI — auto-revert timer + a reset command usable when the screen is unreadable
12. Text capture for the fallback — clipboard / primary selection / shortcut (the old O4 work, smaller)
13. User interface — choosing a config, preview before applying, keep-or-revert dialog
14. Test harness that never touches the real desktop (isolated user / VM)

Cross-cutting concerns to decide: how the user chooses a config (presets vs. old calibration);
drift (someone else changes a setting after we wrote it); uninstall = full revert; monospace /
icon / non-Latin fonts; supported desktops; own-UI accessibility; mentor approval of the pivot.

### Step 4 decisions (team)

- **Configuration is chosen by user presets + manual adjustment. A/B calibration is dropped.**
- **Uninstall = full revert** of everything ScribeSense changed.
- **Language scope: English only.**
- **Fonts never patched:**
  - monospace fonts (code editors, terminals — alignment must not change)
  - icon fonts
  - complex / multi-script fonts (exact rule pending — see open question)
- **Desktops:** Hyprland is the base; GNOME support wanted (scope level pending).

### Proposed merged structure (pending confirmation)

| # | Part | Merges | Job |
|---|---|---|---|
| 1 | Configuration & presets | 1 | Settings, units, presets, font-eligibility rules |
| 2 | Font generator | 2 | Check eligibility → build reading + UI variants → self-validate → cache |
| 3 | Target adapters | 3 + 9 + per-app part of 5 | One per target; each can **detect, apply, verify, revert** |
| 4 | Apply controller | 8 + 10 | Runs the sequence, tells user which apps to restart, keep-or-revert timer |
| 5 | Safety journal | 4 + 11 + uninstall | Snapshot every change, revert, reset command, uninstall, drift detection |
| 6 | Store | 7 | Presets, fonts, change records |
| 7 | User interface | 13 | Preset editor, preview, keep-or-revert dialog |
| 8 | Fallback reader | 6 + 12 | Capture (clipboard / selection / shortcut) + pop-up reader |
| 9 | Test harness | 14 | Isolated testing that never touches the real desktop |

**Open:**
1. Multi-script rule: most system fonts (Noto Sans, DejaVu) cover several scripts — "don't patch"
   would exclude nearly everything. Alternative: patch only the Latin glyphs (Step 2 finding).
2. GNOME: MVP target, or after Hyprland works? GNOME blocks primary-selection reading (Step 2)
   and has its own font-reload behaviour → needs separate testing.
3. Without calibration, what is the project's novel contribution?

**Resolved:** GNOME deferred — Hyprland only for now.

---

## Revision 1 — Where we stand (2026-10-02)

### How the program works now

1. **Choose** — user picks a preset and adjusts it; preview shown before anything changes.
2. **Save** — configuration stored.
3. **Build** — generator skips monospace / icon fonts, builds reading + UI variants, self-validates.
4. **Detect** — adapters find installed targets (GTK, Qt, Chromium, Firefox, Flatpaks).
5. **Snapshot** — safety journal records every setting/file before it is changed.
6. **Apply** — adapters write fontconfig rule, gsettings, text-scaling, qt6ct, Chromium prefs, Flatpak override.
7. **Verify** — per app: chosen font (fc-match) + drawn font (render check).
8. **Confirm** — user told which apps to restart; keep-or-revert timer; no answer → auto-revert.
9. **Fallback** — where a change fails: capture text → pop-up reader.
10. **Maintain** — switch preset, detect drift, recovery command, uninstall = full revert.
11. **Phase 3** — recommender suggests configs; edits from the notification are remembered.

### Role division (proposed)

| Owner | Area | Parts |
|---|---|---|
| **O1** | Configuration & Fonts | 1 Configuration & presets · 2 Font generator · Phase 3 recommender |
| **O2** | User Experience | 4 Apply controller · 7 User interface · 8 Fallback reader |
| **O3** | Data & Platform | 6 Store · 9 Test harness · CI, packaging, install/uninstall packaging |
| **O4** | System Integration & Safety | 3 Target adapters · 5 Safety journal |

### Contracts to define before parallel work

| Contract | Owner | Used by |
|---|---|---|
| Configuration vocabulary (settings, units, steps, presets, font rules) | O1 | all |
| Generator API: config → reading + UI font variants + validation result | O1 | O2, O4 |
| Adapter interface: detect / apply / verify / revert | O4 | O2 |
| Journal record: what was changed, old value, how to revert | O4 | O3 (stores), O2 (UI) |
| Store API | O3 | all |

### Still open
1. Multi-script rule: skip such fonts, or patch Latin glyphs only?
2. Real deadline and what is assessed.
3. Novel contribution now that calibration is dropped.
4. Mentor approval of the pivot.

### Open items — answers (team)
1. Multi-script: **patch Latin glyphs only.**
2. Deadline: **20 October.**
3. Novelty claim: **applying spacing to the entire system** (incl. word spacing the OS does not expose).
   Team states no existing application does this. Keep a short prior-art note (what was searched, what was found) as evidence.
4. Phase 3: recommender (ML) + voice assistant. Recommender idea not yet thought through.
   Screen contrast: not included (evidence considered weak).

---

## Step 5 — MVP scope for the deadline (DRAFT)

| Priority | Includes |
|---|---|
| **Must** | Presets + manual adjust · generator (Latin only, skip mono/icon, reading + UI variants, validation) · adapters: fontconfig, GTK gsettings + text-scaling, Flatpak override (covers GTK, LibreOffice, Firefox/Zen, Flatpaks) · safety journal + revert all + recovery command · minimal UI (presets, preview, apply, keep-or-revert) · store · Qt via qt6ct · Chromium Preferences · pop-up reader with paste |
| **Later** | Shadow mode · drift detection · Electron · GNOME · recommender · voice assistant |

## Modules per owner (DRAFT)

**O1 — Configuration & Fonts**
- `config/settings.py` — settings, units, ranges, steps
- `config/presets.py` — named presets (wcag-baseline, wide, extra-wide)
- `config/rules.py` — font eligibility: skip monospace + icon fonts; Latin glyphs only
- `fontgen/patcher.py` — letter spacing, word spacing (space glyph), line metrics; ligatures off
- `fontgen/variants.py` — reading variant + UI variant (capped line height)
- `fontgen/naming.py` — content key → family name `AccessSans-<key>`
- `fontgen/validate.py` — render sample text and measure before activating
- `fontgen/cache.py` — write fonts to disk + `fc-cache`

**O2 — User Experience**
- `ui/app.py`, `ui/window.py` — application shell
- `ui/preset_editor.py` — choose and adjust a preset
- `ui/preview.py` — sample text in the generated font
- `ui/confirm_dialog.py` — keep-or-revert timer
- `controller/apply_flow.py` — build → detect → snapshot → apply → verify → confirm
- `controller/restart_notice.py` — which apps need restarting
- `reader/capture.py` — paste / clipboard input
- `reader/popup.py` — fallback reader window

**O3 — Data & Platform**
- `store/schema.sql`, `store/migrations.py`, `store/repository.py` — presets, fonts, journal entries
- `harness/sandbox.py` — isolated HOME / fontconfig for tests; never touches the real desktop
- `harness/render_check.py` — screenshot-based checks per engine (reuses O4's verify)
- `pyproject.toml`, `scripts/doctor.py`, CI, install + uninstall scripts

**O4 — System Integration & Safety**
- `adapters/base.py` — interface: detect / apply / verify / revert
- `adapters/fontconfig.py` — conf.d rule, incl. redirecting named UI fonts
- `adapters/gtk.py` — gsettings font keys + `text-scaling-factor`
- `adapters/flatpak.py` — user override for fontconfig
- `adapters/qt.py`, `adapters/chromium.py`
- `safety/journal.py` — record the old value before every write
- `safety/revert.py` — revert one change / revert all (also used by uninstall)
- `safety/recovery.py` — `scribesense reset` command + keybind
- `safety/verify.py` — fc-match + drawn-font check per app

## Browser finding (2026-10-02)

Two separate browser layers:
- **Browser UI + default fonts** → Chromium Preferences adapter (O4).
- **Web page content** (sites set their own fonts and line height) → needs **CSS injection** via a
  browser extension: font family + line-height only.
- **Canvas / PDF content** (Google Docs, PDF viewers) → cannot be restyled → offer the fallback
  reader as an explicit user option.
- **Open:** the CSS-injection extension is not in the Must list or any owner's modules yet.

---

## Checkpoints (end goal: module specifications for the team, in Python)

| # | Checkpoint | Output | Status |
|---|---|---|---|
| C1 | Understanding | Problem, users, evidence, constraints | ✅ done |
| C2 | Ideation & feasibility | Product definition + tested mechanisms (Steps 1–3) | ✅ done |
| C3 | Feature specification | Every feature: what it does, acceptance criteria, failure behaviour, priority | ✅ done |
| C4 | Architecture & contracts | Parts, data flow, contracts K1–K11 | ⚠️ drafted in `ARCHITECTURE.md`; A1–A20 open |
| G  | **Mentor gate** | Approval of pivot + scope | pending |
| C5 | Module specifications | Per module: purpose, inputs/outputs, interface, libraries, tests, owner | ⚠️ 43 cards drafted |
| C6 | Team handoff | Owner explain-back, first-cut rule, repo setup | not started |

## C3 — Feature 1: Browser layer (DRAFT)

**Decision:** browser support is **Must** (users read mostly in browsers), including web-page CSS injection.

| Browser family | Default fonts | Web page content | Browser UI |
|---|---|---|---|
| Firefox (Firefox, Zen) | fontconfig / Flatpak rule (✅ Step 3) | `userContent.css` + enable pref via `user.js` in the profile — no extension | `userChrome.css` (optional) |
| Chromium (Brave, Chrome) | Preferences `webkit.webprefs.fonts.*.Zyyy` (✅ Step 3) | **our own small extension** injecting CSS | ? likely follows GTK — unverified |
| Canvas / PDF (Google Docs, PDF viewer) | — | ❌ cannot restyle → "Open in ScribeSense reader" option | — |

`Zyyy` = Unicode script code for "Common", i.e. the default font for all scripts. It is the
file-level equivalent of `chrome://settings/fonts`, and only affects pages that set no font.

**Injected CSS rules:** reading-variant font by **name** (it is installed system-wide, so no
`@font-face`, no server, no `file://`) + `line-height`; never extra letter/word spacing;
exclude `code, pre, kbd, samp` and icon fonts.

**Rejected:** Tampermonkey / Stylus (third-party extension with access to every site) and
hosting the font on a server or CDN (network use breaks the offline/privacy promise).

**Constraints to verify:**
- Chromium will not silently install an extension → user installs it once (developer mode or store).
- Release Firefox requires signed extensions → reason to use `userContent.css` there.
- `toolkit.legacyUserProfileCustomizations.stylesheets` is a "legacy" pref; may change in future versions.
- Icon-font exclusion selectors are incomplete (Material Symbols etc.).
- Extension is **JavaScript**, not Python — the one non-Python component.
- Zen is a Flatpak → profile lives under `~/.var/app/…`.

**Owner:** O4 (browser adapters + extension) — to confirm.

## C3 — Feature 2: Font generator (DRAFT)

**What it does:** takes one configuration + one base font → produces two installed fonts
(reading variant, UI variant) that carry the spacing, or refuses with a clear reason.

| | |
|---|---|
| **Input** | base font family (from a shortlist) · letter spacing · word spacing · line height |
| **Output** | `AccessSans-<key>` reading + UI variants, 4 styles each (Regular/Bold/Italic/BoldItalic), installed + cached |
| **Rules** | Latin glyphs only · ligatures off · refuse monospace + icon fonts · UI variant line height capped (~1.2–1.3×) · values rounded to fixed steps |

**Acceptance criteria (draft):**
- Rendered sample text measures within tolerance of the requested spacing (pango-view check).
- Same inputs → same key and identical output (deterministic).
- Monospace or icon font as input → refused with a reason, nothing written.
- Build of one family completes in seconds (measured 1–6 s).

**Failure behaviour:** validation fails → font marked failed, never activated, current setup untouched, user told why.

**Licensing (clarified):** the generator *does* create a modified font (changed widths, space
glyph, line metrics, ligatures removed). So the base font's licence must allow modification.
- Open licences (SIL OFL, Apache 2.0): allowed; modified copies must be **renamed** → already
  handled by `AccessSans-<key>`.
- Proprietary fonts (Calibri, Segoe, Arial): typically not allowed → never used as a base.
- Generating on the user's machine vs. shipping fonts with the app: both governed by the
  base font's licence; open licences permit both (licence text must be included when shipping).
- Shadow mode (keeping the original name) is the unclear case → stays Later; get advice.
- **Rule: shortlist = open-licensed fonts only. Verify each licence file before adding.**

**Decisions needed:**
1. ✅ Base-font shortlist for development: **Carlito, Atkinson Hyperlegible, Lexend, Noto Sans** (all open-licensed). Licences re-audited before any public release.
2. Generate from the chosen base font only, or also patch fonts already on the system? (patching existing fonts = shadow mode = Later)
3. ✅ Ranges and steps:

   | Setting | Range | Step | Values |
   |---|---|---|---|
   | Letter spacing | 0 – **0.30 em** | 0.02 | 16 |
   | Word spacing | 1.0 – 3.0× space width | 0.25 | 9 |
   | Reading line height | 1.0 – 2.0× | 0.1 | 11 |
   | UI line height | **separate setting; recommended 1.2–1.3×** (not a global hard cap) | — | — |

   - Reading/content line height is controlled by the user's accessibility setting.
   - UI line height has its own limits, independent of the reading value.
   - Known risk: Qt menus truncated at 1.8×; values between 1.3× and 1.8× untested.
   - ✅ The user may raise the UI value above the recommended range, **with a warning** ("menus in some apps may cut off text").

## C3 — Feature 3: App adapters (DRAFT)

**What they do:** each adapter makes one kind of app use the generated fonts, and can undo it.
Every adapter has the same five actions:

| Action | Meaning |
|---|---|
| detect | Is this target present? (installed, profile found, running?) |
| snapshot | Hand the current value to the safety journal **before** any write |
| apply | Write the new value |
| verify | Chosen font (fc-match) + drawn font (render check) |
| revert | Restore the snapshot |

**MVP adapters:**

| Adapter | Writes | Restart needed | Notes |
|---|---|---|---|
| fontconfig | `~/.config/fontconfig/conf.d/` rule: generic + named UI fonts → generated fonts | yes | base for everything |
| GTK | gsettings `font-name`, `document-font-name` + `text-scaling-factor` | font: yes · size: live | |
| Flatpak | `flatpak override --user --filesystem=xdg-config/fontconfig:ro` | yes | |
| Qt | `qt6ct.conf` font by name | yes | only works if Qt apps use qt6ct |
| Chromium | Preferences `webkit.webprefs.fonts.*.Zyyy` + our extension | yes | browser must be **closed** to edit |
| Firefox | `user.js` pref + `chrome/userContent.css` | yes | incl. Zen under `~/.var/app/…` |

**Failure behaviour:** an adapter that can't apply or verify reports it; the others continue;
the user sees which apps are covered and which fall back to the reader.

**Decisions (team):**
1. ✅ App running → **ask the user to close it**; never close it ourselves.
2. ✅ Flatpak → **per-app override** (`flatpak override --user <app-id> …`).
3. ✅ Browsers → **every profile**.
4. ✅ Qt → configure via qt6ct **only if the session already uses it**; otherwise report Qt as
   not covered (fallback reader). Never change the session globally for coverage.

**Consequences:** Flatpak apps and browser profiles added *after* applying are not covered until
the next apply/check → relevant to drift detection (Later).

---

## C3 — Feature 4: Safety journal, revert, recovery (DRAFT)

**What it does:** records every change before it happens, so any change — or all of them — can
be undone, even when the screen is unreadable.

| Part | Behaviour |
|---|---|
| Journal entry | target · file path or settings key · old value (or "did not exist") · new value · time · apply-session id. Files are backed up as full copies. |
| Keep-or-revert | After applying, a countdown; no confirmation → automatic revert of that apply session |
| Revert | One apply session, or everything (uninstall uses revert-everything) |
| Recovery | `scribesense reset` command + a keybind; works without the GUI |

**Acceptance criteria (draft):** after revert-everything, every touched file and key is
byte-identical to before; killing the app mid-apply leaves a revertable journal, never a half-applied, unrecorded state.

**Decisions (team):**
1. ✅ Countdown: **30 s**.
2. ✅ Unconfirmed apply + crash/reboot → **auto-revert at next login**.
3. ✅ Value changed by the user after we wrote it → **skip it and tell the user**.
4. ✅ Recovery keybind: **Ctrl + Alt + Shift + Super + Backspace** (deliberately hard to trigger).
   - The keybind itself is written into the Hyprland config → it is journaled, but survives
     `reset`; only uninstall removes it.
   - Shown during setup and in docs, since a four-key chord is easy to forget.

---

## C3 — Feature 5: Apply controller (DRAFT)

**What it does:** runs one apply from start to finish and keeps the user informed.

Sequence: build fonts → detect targets → snapshot → apply → verify → restart notice → keep-or-revert.

| Situation | Behaviour (proposed) |
|---|---|
| Font build or validation fails | Stop before any write; nothing changes |
| fontconfig adapter fails | Stop and revert — every other adapter depends on it |
| One other adapter fails | Continue; report that app as "not covered → use the reader" |
| App must be closed (browser) | Ask; skip it if the user declines, report as pending |
| Apps need restart | List the affected running apps; never restart them ourselves |

**Decisions (team):**
1. ✅ Countdown starts after **ScribeSense relaunches its own window with the new fonts**; the
   user confirms the dialog is readable (like a display-resolution change).
   - Limitation: this proves the GTK path only; other apps may still differ → recovery keybind stays available.
2. ✅ Apply succeeds but verify fails → **revert that adapter only**, mark the app not covered,
   continue with the others. The journal never records an unverified change as applied.
3. ✅ **Preflight check** before any snapshot/apply: permissions, files writable, directories
   exist, generated fonts valid, disk space, adapters available, no other apply running (lock).
4. ✅ **Explicit transaction states**, one ID per apply:
   `CREATED → SNAPSHOTTED → APPLYING → VERIFYING → AWAITING_CONFIRMATION → CONFIRMED | REVERTING → COMPLETED`
   (plus `FAILED` / `REVERTED` end states). Recovery at login reads the state of any unfinished transaction.
5. ✅ Wording: **"not covered"** = the app keeps its existing font; its text can still be opened
   in the ScribeSense reader.

---

## C3 — Feature 6: User interface (DRAFT)

**What it does:** lets the user pick and adjust a configuration, see it before applying, and
understand what happened afterwards.

| Screen / element | Behaviour |
|---|---|
| Preset picker | wcag-baseline / wide / extra-wide + the user's saved presets |
| Adjust | font, letter, word, reading line height, UI line height (warning above 1.3×), size |
| Preview | sample text rendered in the generated font **before** anything is applied |
| Apply + coverage report | per app: covered / not covered / pending (browser open) / restart needed |
| Confirm dialog | 30 s countdown in the relaunched window |
| Recovery info | shows the reset keybind and command |

**Own accessibility (release gate, carried over):** keyboard-only use, screen-reader labels,
works at large text sizes.

**Decisions (team):**
1. ✅ Preview: **fixed sample paragraph** (public-domain English text).
2. ✅ Presets: small built-in set + user-saved presets.

   | Preset | Intent | Draft values (letter / word / line) |
   |---|---|---|
   | Default | conservative (≈ WCAG 1.4.12) | 0.12 em / matched to ≈0.16 em / 1.5× |
   | Reading | more line height | 0.12 em / 1.5× / 1.8× |
   | High Separation | stronger letter + word separation | 0.20 em / 2.5× / 1.6× |
   | Custom | the user's current adjustments | — |

   Plus an **Original** state = everything reverted (ScribeSense off). Values to be confirmed.
3. ✅ **Global keybind** switches presets; **tray icon optional**.
   - A switch is a full apply (restart notice + 30 s confirm), not instant — fonts are cached, so it is fast to build.
   - Keybind combination: to choose.

---

## C3 — Feature 7: Fallback reader (DRAFT)

**What it does:** shows text from apps that can't be changed (canvas apps, PDFs, uncovered apps)
in a ScribeSense window using the user's reading font and line height.

| Way in | Status |
|---|---|
| Paste into the reader | certain |
| Clipboard ("open what I copied") | certain |
| Selected text via keybind (primary selection) | works on Hyprland ✅ |
| Browser right-click → "Open in ScribeSense" | possible through **our own extension** |
| System-wide right-click menu | ❌ not available on Wayland |

PDF viewers and Google Docs usually allow selecting/copying text → those reach the reader via copy or selection.

**Carried over from the original plan:** text is treated as data, never stored or logged;
empty selection says "nothing selected" and never grabs a whole document; size limit with a clear message.

**Decisions (team):**
1. ✅ MVP ways in: **all four** (paste, clipboard, selection keybind, browser right-click via
   our extension). No system-wide right-click (not possible on Wayland).
2. ✅ Reader text is kept **in memory for the session only**; never written to disk; lost when the session ends.
3. ✅ Keybinds (checked free on the dev machine's Hyprland config):

   | Action | Keybind |
   |---|---|
   | Open selection in reader | **Super + Alt + R** |
   | Switch preset | **Super + Alt + P** |
   | Recovery reset | **Ctrl + Alt + Shift + Super + Backspace** |

   Other machines differ → keybinds are configurable, and install checks for conflicts first.

---

## C3 — Feature 8: Store (DRAFT)

**What it does:** keeps everything ScribeSense must remember between sessions, locally, for one user.

| Stored | Not stored (ever) |
|---|---|
| Presets (built-in + user) and the active one | Reader text / any document text |
| Generated-font registry: key, base font, values, status, path, hash | Window titles, file paths of user documents |
| Transactions + journal entries + file backups | Screen contents |
| Last coverage result per app | |

Location (MVP, single user): `~/.local/share/scribesense/`.

**Proposed rule:** the **original value of every target** (before ScribeSense ever touched it)
is kept permanently, so uninstall/Original can always restore it, even after many applies.

**Decisions (team):**
1. ✅ Recommender data: **off by default**. Optional setting **"Help improve recommendations"**
   records only anonymized configuration values — never text or content. Stays local.
2. ✅ Retention: keep recent completed transactions; prune older records and backups after a
   retention period (proposed default: **30 days, always keeping the last 10**). **Original
   baseline values are kept permanently.**

---

## C3 — Feature 9: Test harness (DRAFT)

**What it does:** tests every part without ever touching the real desktop (Step 3's tests
changed real settings and opened a browser tab — this prevents that).

| Level | How | Covers |
|---|---|---|
| Unit | plain Python tests | config, presets, journal logic, transaction states |
| Isolated | temporary `HOME` / XDG folders + private fontconfig per test | generator, fontconfig/GTK/Qt/Firefox/Chromium adapters, revert |
| Render | pango-view, Qt offscreen, headless Brave/Firefox with temp profiles | "drawn font" checks |
| Full system | separate test user or VM | Flatpak overrides, Hyprland keybinds, relaunch + confirm, login auto-revert |

Test pages are local files (body text, code blocks, icon fonts) — no network.

**Decisions (team):**
1. ✅ Full-system level: **VM** (disposable; reset from a snapshot between runs).
   - Verify early that Hyprland runs in the chosen VM (it needs working GPU acceleration).
2. ✅ Manual checklist: **keybinds, confirm dialog, screen-reader use**.

---

## C3 — Feature 10: Install, uninstall, packaging (DRAFT)

**What it does:** gets ScribeSense onto a machine and removes it completely.

| Part | Behaviour (proposed) |
|---|---|
| Install | Python package + a `doctor` check that names every missing dependency (pango-view, qt6ct, flatpak…) |
| Keybinds | Written to a separate ScribeSense file, included from the Hyprland config; conflicts checked first |
| Browser extension | Chromium: user installs it once (guided) · Firefox: handled by the adapter |
| Uninstall | Revert everything to the original baselines → remove generated fonts → remove keybind file → remove data |

**Packaging constraint:** ScribeSense **cannot be a Flatpak.** A sandboxed app can't write the host's
fontconfig, gsettings, other apps' configs or Flatpak overrides — the whole product. The old
plan's Flatpak route is dropped; MVP installs natively (an RPM can come later).

**Found on the dev machine:** the Hyprland config is **Lua** (`hyprland.lua`), not the classic
`.conf` format → the keybind writer must support both.

**Decisions (team):**
1. ✅ MVP install: **repo + `uv`/pip + `doctor`**. RPM deferred.
2. ✅ Uninstall **keeps user data by default** (presets, settings). A separate, explicit
   **"Delete all ScribeSense data"** option removes it — asked, never automatic.
3. ✅ Uninstall follows the same drift rule as revert: a value the user changed after we wrote
   it is **skipped and reported, never overwritten** with the original.
   - Edge case: a skipped setting may still name a generated font that uninstall removes → that
     app falls back to its default font; the uninstall report says so.

---

## C3 review — feature specification complete

| # | Feature | Status | Owner |
|---|---|---|---|
| 1 | Browser layer (Firefox CSS, Chromium prefs + our extension) | ✅ | O4 |
| 2 | Font generator (4 open-licensed fonts, ranges, reading + UI variants) | ✅ | O1 |
| 3 | App adapters (fontconfig, GTK, Flatpak per app, Qt via qt6ct, browsers, all profiles) | ✅ | O4 |
| 4 | Safety journal, revert, recovery (30 s, login auto-revert, skip-and-report) | ✅ | O4 |
| 5 | Apply controller (preflight, transaction states, per-adapter revert on failed verify) | ✅ | O2 |
| 6 | User interface (presets, fixed preview, coverage report, keybind + optional tray) | ✅ | O2 |
| 7 | Fallback reader (4 ways in, session-only text) | ✅ | O2 |
| 8 | Store (opt-in recommender data, 30-day retention, permanent baselines) | ✅ | O3 |
| 9 | Test harness (unit → isolated → render → VM, manual checklist) | ✅ | O3 |
| 10 | Install / uninstall (uv + doctor, keep data unless asked) | ✅ | O3 |

**Still open before C4:**
1. Preset values (Default / Reading / High Separation) — drafts need confirming.
2. Workload: O4 now holds features 1, 3, 4 (incl. the JavaScript extension) — heaviest owner.
3. Prior-art search for the novelty claim.
4. Mentor gate — pivot + scope approval.

## Next

- Resolve C3 open items → C4 (architecture & contracts).

- C4 + C5 draft (architecture, contracts, module specs, helper work packages): see `ARCHITECTURE.md`.

---

## Decisions (2026-10-02, after C3)

- ✅ Ownership: **original owners (O4 keeps adapters + safety + browser layer)**, with O1–O3
  assisting O4 through helper work packages once their own area is done.
- ✅ Preset draft values accepted for now.
- ✅ Mentor will be consulted on the pivot and scope.
- ✅ `ARCHITECTURE.md` accepted as the working draft for C4/C5; its 9 undiscussed choices are still open.
- ▶ Under discussion: the assist model and the level of detail the module plan needs.
- ✅ Helper packages open to O1–O3: GTK, Flatpak, Qt, Firefox, Chromium prefs, browser extension.
  **O4 only:** adapter interface, fontconfig adapter, journal, revert, recovery.
- ✅ Helpers claim work as GitHub issues, one package per person at a time.
- ✅ Assist preconditions: contracts frozen · O3 isolated harness ready · helper's own area tested + reviewed.
- ✅ Module card template: 16 fields (see `ARCHITECTURE.md` §0).
- ✅ `ARCHITECTURE.md` rebuilt from recorded decisions only; undiscussed choices listed as A1–A20 (§3).

---

## Decisions (2026-10-08) — A1–A20 closed

- ✅ All twenty open architecture items decided (team proposals + six reviewed amendments).
  Full text in `ARCHITECTURE.md` §3.
- ✅ Amendments:
  1. Confirmation runs as a **fresh process** `scribesense confirm <tx>` (bypasses single-instance);
     the background service is the watchdog until the tx ends. Added `confirm` + `quit` to K10.
  2. Browser boundary: Chromium = extension (styling + right-click); Firefox/Zen = CSS file +
     selection keybind. Flatpak native messaging = feasibility test.
  3. Presets store **intent** (word spacing `target_em` or `multiplier`); `resolve()` produces the
     one concrete Configuration. K2 rewritten.
  4. Font key includes **every input** that changes the generated files (incl. both line heights).
  5. Python 3.11 = core-logic compatibility only; GUI validated on system Python (3.14).
  6. M2.3 owns startup deadline, readiness signal, heartbeat and failure tests; O4 reviews.
- ⚠️ PBL date now possibly **14 October** (tentative, unconfirmed) — down from 20 October.
- ▶ Next: freeze K1–K11.

## Decisions (2026-10-09) — contracts

- ✅ K1–K8, K10, K11 **frozen** (details in `ARCHITECTURE.md` §5). K9 = draft, **O3 review required**.
- Key changes: K1 canonical rounding + `NOT_ON_STEP` guard · K2 preset intent + bundled fonts + UUID ids ·
  K3 `plan()`/`apply()` split (write-ahead intent), `revert(item, backup)`, atomic writes, per-operation timeouts ·
  K4 cross-process `flock`, intent before every write, idempotent revert, Unix-socket IPC (same user, ≤1 MiB),
  `reset` never "busy" (pidfd-verified takeover) · K5 `FAILED_CLEAN` vs `REVERT_FAILED` ·
  K6 context-based coverage mapping · K7 private preview build + live-reference cleanup ·
  K8 ErrorCode list v1 (add-only) · K10 exit codes · K11 versioned replies + pinned extension key.
- ✅ Follow-up fixes (2026-10-09): K4 `items()` returns `JournalItem` incl. intent; revert rule defines
  absent originals (`existed=False`) and uses baseline + latest intent for revert-all · K5/M2.1: a failed
  per-adapter revert stops the apply and rolls back the whole tx · ErrorCode v1 approved (add-only):
  `BUNDLED_FONT_MISSING`, `ADAPTER_COMMAND_FAILED` added · K9 review checklist for O3.

## Decisions (2026-10-09) — recovery review R2

- ✅ Applied to `ARCHITECTURE.md` (§3 "Recovery review R2", K3–K11 marked "amended R2"):
  permanent `LocationRecord` (baseline + managed) · recovery = finish unfinished txs, then baselines ·
  `CONFIRMED` removed, atomic `Journal.keep()` · named lock owners, 60 s wait, pidfd + process-group takeover ·
  canonical `State` / `read_state()` · `run_cmd()` + default timeouts · bubblewrap-enforced tests ·
  extension revisions, reconnect, local Disable · durability wording · partial font styles ·
  `CHANGED_DURING_APPLY`, `PLAN_CONFLICT` · 7 recovery scenarios before any real adapter.
- ⏳ Proposed, awaiting confirmation: R2-8 (`dconf read` for settings state) · R2-9 (keep referenced fonts
  on uninstall — **reverses F10**).
- ✅ K9 store written (2026-10-09) as **draft for O3 sign-off**: records, settings list, signatures, journal-only
  tables, busy/error/migration/pruning/delete-all rules, 12 acceptance tests. K4 gains `prune_history()` +
  `no_obligations()`; K8 gains 9 store/font codes; M4.12 postcondition reworded.
- ▶ Implementation starts now for modules not blocked by K9. R2-8 (Gio vs dconf) and R2-9 still open.

## Decisions (2026-10-12) — socket location + cross-owner notes

- ✅ **K8 amended (contract change, decided by O4; O3 as K8 owner to confirm):** `socket_path()` moves to
  `$XDG_RUNTIME_DIR/scribesense/service.sock` (fallback `data_dir()/service.sock`). Reason: the kernel limits
  Unix socket paths to 108 bytes; the runtime dir is short. `lock_path()` unchanged. Contract code
  `contracts/paths.py` and its contract test updated.
- ✅ Cross-owner changes recorded as notes in `ARCHITECTURE.md`: M3.3 (O3 — short test data dir in
  `tests/conftest.py`), M2.1 (O2 — reference apply sequence in `safety/testing.py` until the controller
  exists), M3.9 (O3 — two pytest markers in `pyproject.toml`).
- Rule from now on: O4 does not edit other owners' files; unavoidable changes are recorded in `ARCHITECTURE.md`.

## Decisions (2026-10-12) — R2-8, R2-9

- ✅ **R2-8 = Gio:** settings state is read with `Gio.Settings.get_user_value()` (works with the sandbox's
  `GSETTINGS_BACKEND`); `Gio` is allowed in recovery, only `Gtk`/`Adw` are banned. Unblocks M4.3 GTK adapter.
- ✅ **R2-9 = yes:** uninstall keeps fonts still referenced by skipped/unresolved settings;
  `--remove-fonts-anyway` removes them. Reverses PLAN Feature 10.

## Decisions (2026-10-12) — remaining O4 adapters

- ✅ M4.14 keybinds D1–D5 accepted (rice user hook / guarded include block, own file outside dotfiles, `hyprctl
  reload`, login hook in the same file, command path as parameter).
- ✅ **K3 rule 10b (contract amendment, O4; second owner sign-off pending):** key locations inside a JSON file the
  app rewrites (`<path>#<dotted.key>`), used by Chromium — fixes M4.7-1 (reset couldn't undo the browser font).
- ✅ The 8 smaller items: keybind marked block counts as the journal record · Firefox revert allowed while running,
  Chromium refused · hashed Firefox target ids · reload skipped when Hyprland isn't running · Flatpak `:ro` narrowing
  accepted · K8 `data_home()` gap accepted for now · extension push path → O2 (M2.4) · native Firefox XDG path added.
- Qt and Flatpak run automatically on the user's system at apply time; nothing is run against the dev machine.
- Extension private key: kept by O4, never in the repo; `extension/tools/set_key.py` inserts the public key.

---

## Implementation progress (live checklist — update when a card changes state)

Legend: ✅ built, its acceptance tests from `ARCHITECTURE.md` §7 pass · 🟡 partial / test double · ⏳ blocked ·
⬜ not started. "(review pending)" = not yet reviewed by the card's reviewer.

**Foundations**
- ✅ Repo skeleton, `pyproject.toml` (uv, `uv_build`), package layout, `.gitignore`, `AGENTS.md` (private)
- ✅ Contracts as code `src/scribesense/contracts/` — K1–K8, K10, K11 frozen · ⏳ K9 DRAFT (awaiting sign-off)
- ✅ Unit-test isolation `tests/conftest.py` (temp HOME/XDG, process launch + foreign sockets blocked, integration fails closed)
- ✅ Import-rule tests R1–R6 (`tests/unit/test_import_rules.py`)
- ✅ CI workflow `.github/workflows/tests.yml` (Python 3.11 + 3.14)

**O1 — Configuration & Fonts**
- ⬜ M1.1 Configuration vocabulary · ⬜ M1.2 Presets · ⬜ M1.3 Eligibility · ⬜ M1.4 Patcher
- ⬜ M1.5 Variants + naming · ⬜ M1.6 Validation · ⬜ M1.7 Cache/install · ⬜ M1.8 Generator API
- ⬜ Bundled fonts + licence check · ⬜ confirm `PreviewFonts` fields

**O2 — User Experience**
- ⬜ M2.1 Apply controller · ⬜ M2.2 Restart notice · ⬜ M2.3 Confirm flow · ⬜ M2.4 UI shell
- ⬜ M2.5 Presets/preview · ⬜ M2.6 Coverage report · ⬜ M2.7 Settings · ⬜ M2.8 Tray (optional)
- ⬜ M2.9 Reader · ⬜ M2.10 Capture · ⬜ M2.11 CLI

**O3 — Data & Platform**
- ✅ K9 **signed off by O3 2026-10-15** (O4 accepted the journal decisions D1/D2/D4/D5/D7 the same day — `store/journal_tables.py` is O3's to write; D3 confirmed by O1 2026-10-18; decisions in `ARCHITECTURE.md`, reasoning in `docs/K9_draft.md`) · 🟡 M3.1 Store (built 2026-10-15: `store/` core + 5 repos + `journal_tables.py` for O4's journal; 126 unit tests + 3-process test in the runner; approved by O4 2026-10-16; O3 fixed O4's 5 notes 2026-10-18; O4's SQLite journal now runs on it) · 🟡 M3.2 Retention (built 2026-10-15: `store/retention.py` `prune(journal, now)` — 30 days / last 10, no pruning while a tx is active, errors skip pruning; 28 tests on both journals; approved by O4 2026-10-16, the in-memory double now keeps the newest COMPLETED apply too; the start-up call is O2/O4's) · ✅ M3.3 Sandbox (unit fixture + bubblewrap runner; 10/10 escape tests pass, 2026-10-14; full runner: 14 pass, 1 fail = O4 GTK read-back bug, review by O4 pending)
- 🟡 M3.4 Render checks (`harness/render.py`: pango engine + unit tests ✅; pango ✅ and qt ✅ verified in the bubblewrap runner (3 passed, 2026-10-15); firefox/chromium engines NOT implemented (known gap, 2026-10-15: they need a live browser process and a protocol — Marionette / DevTools — but `run_cmd` (K3 rule 9) only runs a command to completion, so O4 must approve an exception for the harness; Firefox is `dnf`-installable, Brave is not in Fedora's repos so the Chromium family would use `chromium`) — they report "not implemented" and skip; 2026-10-14) · 🟡 M3.5 VM tests (guard + Hyprland feasibility check + runbook in `vm/`, tested without a VM, 2026-10-14; hypervisor/snapshot decision open; scenarios wait for CLI wiring + M3.8) · 🟡 M3.6 Manual checklist (written `docs/manual-checklist.md`, 2026-10-14; O2 review pending, not yet run) · ✅ M3.7 Doctor (acceptance tests pass, real-host run checked, 2026-10-14; `cli.py` wiring is O2, review by O4 pending) · 🟡 M3.8 Install/uninstall (built 2026-10-15: `uninstall.py` + `docs/INSTALL.md`; 21 unit tests + 2 real-font-cache sandbox tests; NOT wired into `cli.py` (O2) and needs O4's SQLite journal (M4.10); VM acceptance test (byte-identical) waits for M3.5; reviewed by O4 2026-10-16 → O4 fixed lock / fonts / login-check gaps 2026-10-17 → accepted by O3 2026-10-18)
- 🟡 M3.9 CI + import rules (import rules ✅ incl. deliberate-violation self-test; CI now has a bubblewrap `integration` job, 2026-10-14 — NOT yet run on GitHub; branch protection optional)

**O4 — System Integration & Safety**
- ✅ M4.1 Adapter interface — contract K3, **fake adapter** (`adapters/fake.py`) and **conformance suite**
  (`adapters/conformance.py`, proven against 6 broken adapters) (2026-10-12, review pending)
- 🟡 M4.2 fontconfig — `adapters/fontconfig.py`: conformance (3 start states) + unit tests ✅, 4-lens review +
  adversarial verify, 12 findings fixed (2026-10-12, review pending). Remaining: real-fontconfig integration test
  (written, needs O3's bubblewrap runner with `/etc/fonts` + system fonts read-only) · Pango drawn check now wired (M4.13)
- 🟡 M4.3 GTK — `adapters/gtk.py`: conformance (3 start states) + unit tests ✅, GLib text formatting checked
  against real GLib, plan checked read-only on the dev machine (2026-10-12, review pending). Remaining:
  integration test in O3's bubblewrap runner (`GSETTINGS_BACKEND=keyfile`)
- 🟡 M4.4 Flatpak · 🟡 M4.5 Qt · 🟡 M4.6 Firefox — built, conformance + unit tests ✅, reviewed/verified/fixed
  (2026-10-12, review pending). Remaining: VM / bubblewrap integration (real flatpak, qt6ct font format, real Firefox)
- 🟡 M4.7 Chromium — key-level (K3 10b), reset-after-browser-rewrite proven; VM: Chrome accepts the edit
- 🟡 M4.9 Extension + native host — node 35/35, host tests ✅; needs: O4's public key (set_key.py), O2's push path,
  real-browser check (M3.4), packaging excludes test/ and tools/ (O3)
- 🟡 M4.8 Browser CSS — `adapters/browser_css.py`: font family + line height only, code/icon exclusions,
  unit tests ✅ (2026-10-12, review pending). Remaining: real-browser render check (O3's harness, M3.4)
- 🟡 M4.10 Journal — **in-memory double** `safety/memory_journal.py` implements all of K4 (2026-10-12);
  SQLite journal ⏳ K9 · ✅ cross-process lock `safety/lock.py` (flock, owner record, close-on-exec) ·
  ✅ IPC socket `safety/ipc.py` (same-user, JSON lines ≤ 1 MiB, stale-socket handling) (2026-10-12, review pending)
- 🟡 M4.11 Revert — `safety/revert.py`, K4 rule 4 step 1, repeatable, drift-safe; sandbox tests ✅, VM tests ⬜ (2026-10-12, review pending)
- 🟡 M4.12 Recovery — `safety/recovery.py`: `reset()` + `recover_at_login()` ✅ · K4 rule 6 lock handling ✅
  (ask the service over IPC, pidfd + process-group takeover of a hung service only, bounded wait for other
  recoveries, journal re-read) — tested with real helper processes in the sandbox (2026-10-12, review pending).
  Remaining: VM scenarios (crash + reboot, keybind → reset) · CLI wiring `reset` / `recover --login` (O2, M2.11)
- 🟡 M4.13 Verify — `adapters/verify.py` + `adapters/pango_probe.py`: chosen (host + Flatpak) and Pango drawn check,
  wired into M4.2; 3-lens review, 6 findings fixed; real probe checked read-only on the dev machine (2026-10-12,
  review pending). Remaining: integration tests in O3's bubblewrap runner
- 🟡 M4.14 Keybind writer — `safety/keybinds.py`, Lua + classic, ML4W hook, drift-safe block; VM checks listed on card
- ✅ M4.15 Logging — `scribesense/log.py` (moved from `safety/` 2026-10-12 so adapters may log under R5), canary acceptance test passes; strings only in identifier fields (2026-10-12, review pending)

**§9 recovery slice** — `tests/recovery/` via `safety/testing.make_recovery_system`
(in-memory journal + reference apply sequence until M2.1 / SQLite journal exist)
- ✅ S1 keep A → B → revert B → reset · ✅ S2 crash before Keep · ✅ S3 rolled-back adapter keeps `managed`
- ✅ S4 prune keeps baselines · ⏳ S5, S6 need the extension · ✅ S7 user edit skipped and reported

**Open decisions:** K9 sign-off · K8 socket amendment (O3 to confirm) · PBL date.

## Note (2026-10-14, O3) — limits of the CLI-only Fedora server

The O3 dev machine is a **CLI-only Fedora aarch64 server and itself a QEMU guest**: no display, no browser, no `gh`, no
`virsh`/`qemu-img`, `/dev/dri` present. Consequences:

- **Cannot be done from this machine (needs a website or a graphical session):**
  - Viewing the GitHub Actions run of the new `integration` job, and marking it a *required check* (branch protection)
    — GitHub web settings, and the repo is owned by another account (admin rights needed). M3.9.
  - The GUI/desktop items of the manual checklist (sections A–E of `docs/manual-checklist.md`: screen reader, large text,
    confirm window, keybinds on a live desktop). M3.6.
  - Firefox / Brave drawn-font engines for M3.4 and every M3.5 desktop scenario: need a real browser / a Hyprland session.
- **Can be done from the CLI:** everything else — unit tests, the bubblewrap runner, `doctor`, the Pango engine of M3.4
  (after `dnf install gobject-introspection`), `vm.check_hyprland` (prints a report only), checklist sections F–G that are
  shell-only (doctor, install/uninstall, privacy greps).
- The GUI/browser items stay ⬜ until a machine with a graphical session (the test VM, M3.5) exists. They are **not** counted as passed.

## Note (2026-10-15, O3) — CI findings for O4

The new `integration` CI job (Ubuntu 24.04 runner, Python 3.12.3, Pango 1.52.1) fails two tests; both are in O4's code:

1. `adapters/gtk.py` `_read_back`: under the keyfile backend Gio returns a stale value until the GLib main context is iterated
   (reproduced locally). Fix: drain `GLib.MainContext.default()` before each read.
2. `adapters/pango_probe.py` `drawn_families()`: `run.item.analysis.font.describe().get_family()` **segfaults** (exit 139) on
   Ubuntu 24.04 — with and without the sandbox (verified in CI, staged script with `faulthandler`; stages 1–8 pass, stage 9
   crashes). It works on Fedora (Pango 1.57.1). It is a production bug, not a CI setup problem. O4 to find an access path
   that does not crash (and re-test on Ubuntu 24.04). `harness/render.py` (M3.4) reuses this function, so the Pango engine
   has the same limit on that runner.

Until both are fixed the `integration` job stays red; it must not be made a required check yet.

## Open items per owner (2026-10-15, O3) — what each person still has to review, decide or build

Built by O3 and waiting for review: M3.1 store · M3.2 retention · M3.3 sandbox · M3.7 doctor · M3.8 uninstall (details in the cards'
notes in `ARCHITECTURE.md`). K9 is signed off (2026-10-15); the journal part was accepted by O4.

**O4 — System Integration & Safety**
- **Review:** M3.1, M3.2, M3.3, M3.7, M3.8; also M3.4, M3.5, M3.9 (O4 is the named reviewer on those cards).
- **Build the SQLite journal (M4.10) on `store.JournalTables`** (API: the M3.1 note in `ARCHITECTURE.md`; `harness/table_journal.py` shows
  the K4-name mapping). Until it exists `reset` / `recover` / `uninstall` cannot run on the real store.
- **Fix `safety/memory_journal.py`:** `prune_history` does not keep the newest COMPLETED apply (K9 D4 / finding F2, accepted by O4); the SQL
  layer does, so the in-memory double and the SQL journal differ there.
- **Confirm D7:** `journal_tables.is_managed_location_shape()` exists and agrees with `adapters/conformance.py`, but it is not enforced inside
  `add_item` because the fake adapter uses free location names.
- **Confirm ownership of `src/scribesense/uninstall.py`** (new top-level recovery module, put under O3 like `doctor.py`; not in the AGENTS.md table).
- **CI:** check the `integration` job on Ubuntu 24.04 after the Pango/GTK fixes. (O3 removed the diagnostic step 2026-10-15; the job was green.)
- **K1 amendment `NOT_A_NUMBER`** (added by O4, "sign-off by one other owner pending"): needs one other owner's sign-off — O1 or O3.
- **Fontfield inspection for `remove_unused()`** (see the O1/O3 gap below): the fifth K7 rule 3 source — live settings found by inspecting each target's
  font fields — needs adapter support.

**O1 — Configuration & Fonts**
- **K9 D3:** confirm `custom` is stored as one row with the reserved id `custom` (the code in `config/presets.py` already matches). (done)
- **K9 D8:** confirm the font key's inputs are `base_family`, `letter_em`, `word_scale`, `reading_line`, `ui_line` (not `text_scale`).
- **Uninstall:** confirm that using `fontgen.cache.installed_keys()` and `remove()` is the intended way to remove generated fonts.
- **Open on O1's side:** base fonts are not bundled yet (`build()` refuses with `BUNDLED_FONT_MISSING`); `GENERATOR_ERROR` is proposed for the error codes.
- **Gap, not yet owned by anyone:** `fontgen.generator.remove_unused()` needs a `ReferenceSource` (`transaction_active()`, `references() -> References(keys,
  complete)`); the code says nothing implements it. The store now holds four of K7 rule 3's five sources (active config, saved presets, unfinished
  transactions, location records); the fifth (live settings per target) needs the adapters. A store-only source cannot honestly report `complete=True`,
  so `remove_unused()` would still remove nothing. Decide who writes it (O3 can supply the four store-backed sources).

**O2 — User Experience**
- **Wire `cli.py`:** `uninstall` → `uninstall.run_uninstall(journal, adapters, store=Store(default_path()), policy=LockPolicy(lock, "uninstall"), …)`
  (exact call in the M3.8 note); `reset` / `recover` once O4's journal exists. `doctor` is already wired.
- **At service start-up:** call `store.recover_interrupted_builds()` once and `prune(journal, now)` once (not on every `Store` open).
- **Review `docs/manual-checklist.md`** (M3.6).
- **Agree with O3** on who owns the uninstall wiring (flow = O3, command line = O2).

**Repo owner:** mark the `integration` CI job as a required check once it is green.

**O3 (still open):** M3.4 Firefox / Chromium engines (needs a browser installed; O4 approved the harness exception) · M3.5 VM scenarios (needs a VM with a
graphical session) · M3.6 graphical checklist sections · M3.8 byte-identical VM test · (done 2026-10-15: the CI diagnostic step was removed after the `integration` job turned green on Ubuntu).

## Status update (2026-10-18, O3) — after merging main into O3

- **Done by others:** O4 built the SQLite journal on `store.JournalTables` (M4.10, `safety/journal.py`) and approved M3.1 and M3.2; the in-memory journal now
  keeps the newest COMPLETED apply; O4 fixed three gaps in `uninstall.py` (lock for the whole run, fonts of unfinished txs kept, login check kept while
  recovery is pending), accepted by O3.
- **Done by O3:** the five notes from O4's M3.1 review (see the M3.1 note in `ARCHITECTURE.md`). 1657 unit tests, 21 sandbox tests, CI green.
- **Still open for others:** O2 — wire `cli.py` (`uninstall` with `service=ServiceControl(running=…, stop=quit_via_cli)`, `reset`, `recover`), start-up calls `recover_interrupted_builds()` and `prune()`, review
  `docs/manual-checklist.md`; O1 — confirm D3 / D8, bundled fonts, `ReferenceSource` owner; O4 — the K1 `NOT_A_NUMBER` second sign-off; repo owner — required check.
- **Still open for O3:** M3.4 Firefox / Chromium engines, M3.5 VM + scenarios, M3.6 graphical sections, M3.8 byte-identical VM test.

**2026-10-18 (O3):** `--delete-data` vs a running service decided (option C) and built in `uninstall.py` — the service is asked to quit after the user confirms;
if it cannot be confirmed stopped, all data is kept (exit 1). O2 must supply the "is the service running" check; without it `--delete-data` refuses.
- **O4 (added 2026-10-18):** look over the new `ServiceControl` / `quit_via_cli` part of `src/scribesense/uninstall.py` (O3 added it on top of O4's own
  review fixes; 12 tests; not blocking). **O2:** pass `service=ServiceControl(running=…, stop=quit_via_cli)` from `cli.py`, otherwise `--delete-data` refuses.

**Scope note (2026-10-18, O3) — what a green M3.4 does and does not prove.** The M3.4 card prescribes *headless* browsers with a temporary profile
(`--no-remote --new-instance`), and that is what the drawn-font engines test: given this fontconfig / CSS configuration, which font does the rendering
engine actually draw (A14). Headless Firefox / Chromium run on a CLI-only machine. A passing M3.4 test therefore means "the engine picks our font from this
configuration" — **not** "ScribeSense works in a user's browser". The product targets the real desktop browsers on Hyprland; that is only shown by the M3.5 VM
scenarios (a real profile picks up `userContent.css` / `user.js` after a restart; Brave/Chrome rewriting `Preferences` while running; the extension and
native-messaging path; what is drawn on Wayland) and by the M3.6 manual checklist (restart notice, what the user sees). Those stay open until a graphical
test VM exists.

**K9 status (2026-10-18, O3):** signed off and built; `docs/K9_draft.md` brought up to date (schema = the migration file, tests listed with their real files, §9 = O1's reply).
**O1 states D3 and D8 as confirmed.** D8 was corrected by O1 (`base_family` is not part of the registry identity either) and is **adopted and built** with tests. D3: O1 says confirmed, but
describes `custom` as unsaved while their code stores it and expects `PresetRepo.list()` not to return it (M3.1 does) — O1 to say in one line whether `custom` is one stored row and whether
`list()` includes it. Still O3's by hand: the "DRAFT" docstring in `contracts/store.py` (agents may not edit `contracts/`).

**M4.10 reviewed by O3 (2026-10-18): approved, with three non-blocking notes for O4** (details in the M4.10 card in `ARCHITECTURE.md`): (1) `SqliteJournal.begin()` is not thread-safe (not reachable
today, the service serializes with an `RLock`); (2) `prune_history()` checks the apply lock but does not hold it; (3) a new apply is accepted while an unfinished tx from a crash exists —
A3 relies only on the login hook; O2/O4 to decide where to refuse it. Four tests are missing (listed in the card note).

**M4.11, M4.13, M4.14, M4.15 reviewed by O3 (2026-10-18): all approved, with non-blocking notes for O4** (details in each card in `ARCHITECTURE.md`). M4.11: `finish_tx` raises for a tx in `SNAPSHOTTED` that has an
intent (unreachable today), failed items carry no error code. M4.13: document the `sys.executable` assumption and what the fontset probe measures. M4.14: an empty `~/.config/scribesense/` is left after removal, the card
says "include line journaled" but the implementation uses the marker line, the installer must pass `allow_unchecked` on purpose, the VM check is still open. M4.15: use `fullmatch`, identifier fields do not guarantee privacy by
themselves. Reviews still named for O3: M1.4, M1.6, M2.4, M2.7.

**M1.4 and M1.6 reviewed by O3 (2026-10-18): both approved, with notes for O1** (details in each card in `ARCHITECTURE.md`). M1.4: the patcher leaves name records 21 / 22 / 25 untouched, so the source family stays visible to
fontconfig (proved with `fc-scan`) — fix before the real fonts land; the typo-metrics bit is set on `OS/2` tables older than v4; the "all 4 shortlist fonts" condition cannot be met until `fonts/` is bundled. M1.6: the naming check ignores
records 21 / 22 / 25 (the real renderer catches it, fail-closed), the docstring still says the rendering half was never executed, tolerances are tuned on synthetic fonts. Reviews still named for O3: M2.4 and M2.7.

**M2.4 and M2.7 reviewed by O3 (2026-10-18): both approved, with notes for O2** (details in each card in `ARCHITECTURE.md`; read-only review, the UI could not be run on the O3 machine). M2.4: the UI tests skip in CI (no GTK typelibs) and the accessibility
checklist is not run (needs the graphical VM). M2.7: delete-all runs outside the service mutex and the apply lock (a refused deletion after the presets are gone gives a generic error — nothing is lost); the pure helpers sit in a GTK-importing
module and are never tested in CI; keybind normalization is cosmetic. **All reviews named for O3 are now recorded:** M4.10, M4.11, M4.13, M4.14, M4.15, M1.4, M1.6, M2.4, M2.7.

**D3 closed (2026-10-18, O3):** O1 confirmed that `custom` is stored as ONE row (id `custom`) and that `PresetRepo.list()` must NOT return it ("stored ≠ listed"). O1's trap — the font clean-up must see the custom row or it deletes the font of the user's
own configuration — is handled by a new `PresetRepo.list_all()` (step 1, done, with a regression test). **Step 2 is waiting for O2 / O4:** change `controller/service.py:117` from `presets=self.store.presets.list` to `presets=self.store.presets.list_all`;
only then does O3 make `list()` drop the custom row (today it still returns it, which keeps the clean-up safe). Also: O1 to fix the stale `"""ID of the transient Custom entry."""` in `config/presets.py:156`; the K9 owner to add `list_all` to
`contracts/store.py` (frozen; agents may not edit it). The "O1 — confirm D3" item in the per-owner list above is done.

## Status correction (2026-10-18, O4) — after merging O1, O2, O3 and O4 into main

The O3 status update above (2026-10-18) was written on a branch that did not yet contain three merges — O4's
`FontReferenceSource` (24322f9), the K11 push (e574bdb) and the O2 deadline fixes (e3e9858) — so parts of it are out of
date. Kept above as written; the current state:

- **Done since:** `cli.py` wires `reset`, `recover --login`, `uninstall` (now with O3's `ServiceControl`), `doctor`
  (M2.11); the service calls `recover_interrupted_builds()`, `prune()` and `remove_unused()` at start-up
  (`controller/service.py`); `ReferenceSource` is implemented by O4 (`safety/font_references.py`); the K11 push
  (`subscribe_style`), the M2.3 confirm window, the M2.5 private preview, the reader and settings fixes are in main.
  main = O1 + O2 + O3 + O4: 2065+ unit tests and 23 sandbox tests pass, CI configurations green.
- **Blocker for a live Apply:** no base font is bundled (`fonts/` holds only `.gitkeep` on every branch), so `build()`
  refuses with `BUNDLED_FONT_MISSING`. Owner O1 (licence check per K2).
- **Sign-offs still pending:** K1 `NOT_A_NUMBER` — K1 is O1's contract, so O1 confirms as owner plus one of O3 / O4
  (the O3 note above says "O4", an earlier line says "O1 or O3"; either other owner works); `GENERATOR_ERROR` error code
  (O1 + one other); K4 rule 5 addition `subscribe_style` (O4 + one other); K3 rule 10b and K8 (O4 + one other).
- **Still open:** O1 — bundled fonts (D3 and D8 are closed, see the O3 entry above); O2 / O4 — switch
  `controller/service.py` to `presets.list_all` (O3's D3 step 2); O2 — review of the deadline fixes, decide the reader's
  `text_scale` handling and whether `apply` also returns exit 4, `docs/manual-checklist.md`; O3 — M3.4 browser engines,
  M3.5 VM scenarios, M3.6 graphical sections, M3.8 byte-identical VM test; O4 — VM checks (Hyprland Lua / reload,
  `hyprctl binds -j`, qt6ct format, Chrome Preferences, Flatpak); repo owner — make `integration` a required check.

## Decisions (2026-10-18, O4 with the user's go-ahead) — fonts and two sign-offs

- **Base fonts bundled:** Noto Sans 2.015, static Regular/Bold/Italic/BoldItalic made from Fedora's variable fonts
  (`fonts/tools/build_noto_sans.py`, reproducible), OFL-1.1 without a Reserved Font Name; `fonts/manifest.toml` + checksum
  test. This unblocks Apply: all three built-ins build and validate with the real tools. Carlito / Atkinson / Lexend later.
- **M1.4 / M1.6 review notes done** (O3's notes): name records 21/22/25 dropped and checked, typo-metrics bit only on OS/2 ≥ 4,
  explicit invariant check, stale docstring, tolerances re-checked on the real fonts.
- **K1 `NOT_A_NUMBER` — signed off** (proposed by O1 in M1.1; O4 second owner). The "sign-off pending" wording in the
  docstring of `contracts/config.py` is updated by a human (agents may not edit contracts).
- **ErrorCode `GENERATOR_ERROR` — added** to the Fonts row (proposed by O1 in M1.8; O4 second owner). The enum line in
  `contracts/errors.py` is added by a human.
- Still needing a second owner (O4's own proposals): K3 rule 10b, K8, K4 rule 5 `subscribe_style` — O1, O2 or O3.

## O4 VM checks done without the VM (2026-10-18)

On the dev machine (Fedora 44, Hyprland 0.56.2, ML4W Lua config), with all writes on scratch copies / temporary
profiles / a scratch HOME: Flatpak (real sandbox, Obsidian) PASS · qt6ct format (real file, no `[Fonts]` yet) PASS ·
Brave Preferences edit kept by the browser PASS · Firefox `user.js` accepted PASS (gap: the stylesheet pref stays in
`prefs.js` after revert) · real `hyprctl binds -j` parse + no default-keybind conflicts PASS · Lua hook install +
`Hyprland --verify-config` "config ok" + byte-identical removal PASS. Details in the M4.4–M4.7 and M4.14 cards.
Also: a full demo dry run (real app on a private display; real fontconfig / GTK / Qt adapters; bundled Noto Sans):
apply → fc-match picks AccessSans, GTK and Qt fonts switched → Keep → browser `get_style` active → `scribesense reset`
restored everything (5 restored, 0 failed).
**Still for the VM / a real session:** a live `hyprctl reload`; Qt / GTK / browsers actually drawing the font on screen;
the Firefox `prefs.js` revert gap (proposed: key-level location for that pref); M3.5 scenarios and the M3.8 byte-identical
uninstall test.

## Fonts: Carlito, Atkinson Hyperlegible and Lexend bundled (2026-10-18, O4 for O1, user's go-ahead)

Found during the demo rehearsal: choosing any base font except Noto Sans gave "Preview unavailable" and "Nothing changed
(BUNDLED_FONT_MISSING)", because only Noto Sans was in `fonts/`. Fixed by bundling the other three K1 shortlist families
from google/fonts (pinned commit, git blobs verified), with licences, manifest SHA-256 values and `fonts/tools/build_lexend.py`
(Lexend ships only a variable font; Regular + Bold static instances, no italic). Details and licence notes in the M1.2 card.
Tests: 2190 passed (unit) · 14 passed (real-tool fontgen integration in bubblewrap). **For O1 (Sukhmani) to review.**
Still open: the font dropdown should list only families that are actually bundled (guard in O2's `ui/preset_editor.py`),
so a missing folder can never reach the user again.

## Phase III plan (decided with the user, 2026-10-24)

Phase II was evaluated; the app works end to end (2190 unit tests pass; real-tool font builds pass in bubblewrap;
demo: Apply → Keep → Original / `scribesense reset`). This entry is the full list of what remains, the decisions taken
for Phase III and what is deliberately left for later. Owners in brackets. Matching notes are on the ARCHITECTURE.md
cards (§1 Scope, K1, K2, K9, M1.2, M1.3, M2.3, M2.5, M2.11, M3.5, M3.8, M4.9, M4.14).

### Decisions (Phase III)

| # | Decision | Notes |
|---|---|---|
| P3-1 | **Target stays Hyprland only** (Wayland). | GNOME (and other desktops) after Phase III. The Hyprland-specific code is keybinds + login hook (`safety/keybinds.py`), part of `doctor.py` and the settings page. |
| P3-2 | **Packaging: Fedora first** — an RPM, published through Fedora COPR. **Owner O3**; O4 supplies the keybind / login-hook install step. | Releases = version tags + GitHub releases, **made by humans** (agents never tag or release). Arch (AUR `PKGBUILD`) is the next distro, as a Phase III stretch goal. |
| P3-3 | **User font import** is in Phase III. Imported fonts are **copied** into ScribeSense's data folder; sources: a **file / folder picker** and **fonts already installed on the system** (fontconfig list); **folders, no zip archives**. | Needs the K1 / K2 contract change below (O1 + a second owner). **No licence policy for user-chosen fonts** (user's decision): the user picks the font, the generated copy stays user-local and is never shared or redistributed. Fonts we **bundle in the package** are redistribution and keep the existing rule: licence file + manifest entry per font (as done for Carlito, Atkinson, Lexend). |
| P3-4 | **In Phase III:** browser extension end to end, VM full-system tests, user validation study. | |
| P3-5 | **Future work (after Phase III):** GNOME / other desktops, ML / AI (preset recommender), **phonetics** (below), AT-SPI system-wide text access. | Recorded only; no work in Phase III. |

### W1 — User font import [O1 lead · O2 UI · O3 store · O4 references/uninstall]

User flow: (1) the user selects a font file, a folder with a family's styles, or an installed font; (2) ScribeSense checks
it can parse the files safely; (3) it copies them in and applies the user's spacing exactly like a bundled font;
(4) every change stays journaled and reversible as today.

Design (to agree before coding):
1. **Contract change (O1 + one other owner, decision entry here first):**
   - K1: `base_family` may name a bundled family (today's `SHORTLIST`) **or** an imported family. Proposal: keep
     `SHORTLIST` as the bundled set and add a lookup of imported families from the store, so validation still refuses
     unknown names.
   - K2: rule 2 ("bundled only, never from the system") becomes "bundled **or imported** (copied into the data
     folder); never read in place from the system". `base_font_files(family)` resolves both.
   - New error codes (add-only, need approval), e.g. `IMPORT_FAILED`, `IMPORT_NOT_ELIGIBLE`, `IMPORTED_FONT_IN_USE`.
2. **Storage (O3, K9 addition):** files copied to `data_dir()/user-fonts/<sha256>/` (dir 0700, files 0600); a
   `user_fonts` table: family name, style, file name, sha256, size, imported_at, source kind (file / folder / installed).
   Same file imported twice → one copy (content-addressed). Deleting an imported font that a kept apply still uses →
   refused (like `FONT_IN_USE`).
3. **Validation (O1):** reuse `config/rules.py` (32 MB cap, required tables, not icon / monospace, Latin coverage) and
   the existing variable-font flattening in the patcher. **Parse imported files in a separate process with a timeout**:
   the generated font is later read by every app's font engine, so a malformed file must be stopped here.
   Must have a Regular style; other styles optional (as today). **OTF/CFF fonts are untested** — add tests.
4. **Naming:** an imported family whose name equals a bundled one gets a distinct display name; the generated font is
   still `AccessSans-<key>` (the key hashes the source bytes, so caching and revert work unchanged).
5. **UI (O2):** "Import font…" (file / folder) and "Choose an installed font" (from the fontconfig list) in the preset
   editor; the base-font dropdown lists **only fonts that are actually available** (bundled + imported) — this also
   closes the open "only bundled families" guard.
6. **References / uninstall (O4 / O3):** `safety/font_references.py` counts imported sources too; `scribesense
   uninstall` removes imported copies only with `--delete-data` (they are user data).
7. Tests: import file / folder / installed font; refuse monospace, icon, broken, oversized and parse-timeout files;
   OTF/CFF and variable sources; duplicate import; delete-in-use refused; full Apply → Keep → reset with an imported font.

### W2 — Packaging, Fedora first [O3 · O4 for keybinds / login hook]

1. **Blocker — bundled fonts outside the package:** `config/bundled.py:45` finds fonts as
   `Path(__file__).parents[3] / "fonts"` (the repo root). An installed package has no repo root, so every Apply would
   fail with `BUNDLED_FONT_MISSING`. Fix (O1 + O3): install fonts to a data location (e.g. `/usr/share/scribesense/fonts`)
   and resolve it through K8 paths with the repo path as the development fallback; same question for `extension/`
   files the native-host installer needs. Check that `store/migrations/*.sql` are included in the built package.
2. **RPM spec** (`scribesense.spec`): `BuildArch: noarch`, Python ≥ 3.11 (Fedora 44 ships 3.14).
   Requires: `python3-gobject`, `gtk4`, `libadwaita`, `python3-fonttools`, `fontconfig`, the package that provides
   `pango-view`, `glib2` (gsettings), `wl-clipboard`. Recommends / Suggests: `qt6ct`, `flatpak`, `hyprland`.
   **Verify every package name on Fedora**; `doctor.py`'s check list is the source for the dependency list.
3. Files: `/usr/bin/scribesense`, the Python package, fonts + `manifest.toml`, a `.desktop` file with a
   **"Restore original" desktop action** (`scribesense reset`), an icon, AppStream metainfo, and every bundled font's
   `OFL.txt` under `/usr/share/licenses/scribesense/`. Check Fedora's font packaging guidelines for bundled fonts.
4. Per-user setup stays in the app (keybinds, login-recovery hook: Settings → keybinds, O4). The package installs no
   per-user files.
5. **Removal rule:** `dnf remove` runs as root and cannot undo per-user settings. README, the app and the package
   description must say: run **Restore original** or `scribesense uninstall` **before** removing the package.
   Residual risk to document: if the package is removed first, the login hook can no longer run (`pcall` keeps
   Hyprland's config loading), and the fontconfig rule points at fonts that still exist in the user's font folder
   until uninstall — harmless but not reverted.
6. COPR project + build from tagged releases; install-test the RPM in the VM (W4 doubles as the package test).
7. Stretch: AUR `PKGBUILD` from the same tagged release.

### W3 — Browser extension end to end [O4 · O2 for the service side]

1. Generate the extension key **outside the repo**; `extension/tools/set_key.py` puts only the public key in
   `manifest.json` and prints the extension ID.
2. **Native-messaging host installer** (missing today): write the host manifest
   (`safety/native_host.render_host_manifest`) for each installed Chromium-family browser (per-user
   `NativeMessagingHosts/` folders, e.g. Brave's under `~/.config/BraveSoftware/Brave-Browser/`), pointing at the
   installed host executable; journaled and removed by uninstall. Packaging (W2) decides the host executable path.
3. Load the extension unpacked (developer mode) in Brave; check YouTube / Twitch text restyles; check revert.
4. Unskip recovery scenarios 5 and 6 (`tests/recovery/test_recovery_scenarios.py`).

### W4 — VM full-system tests [O3 · O4]

M3.5 scenarios; M3.8 byte-identical uninstall; M3.4 browser engines (Brave needs a short `TMPDIR`; clean stale
`Singleton*` links); a live `hyprctl reload` with our keybinds; on-screen checks that GTK, Qt and Firefox/Zen actually
draw the font. Run them on the **installed RPM**, not the repo.

### W5 — Safety and UI completions [O2 · O4]

1. **Confirm window (M2.3) — not built yet** (stated in `ui/confirm.py`): startup deadline, readiness signal, heartbeat,
   and the check that the sample really renders in the new font. O4 adds the service-side API. Until then only the
   30 s timeout + watchdog protect the user. Remove the "heartbeat" claim from `docs/OS_DBMS_Concepts.html` until built.
2. **Visible "Restore original" button** (O2) — today it is the "Original (revert all)" entry in the preset dropdown.
3. Show `RevertReport.failed_codes` in the reset / Original result (O2, CLI + app).
4. Decide: the reader's `text_scale` handling; whether `apply` exits 4 when the service cannot start (O2).
5. Accessibility audit of our own UI: keyboard-only use, screen-reader labels, contrast (O2).
6. Firefox/Zen: `toolkit.legacyUserProfileCustomizations.stylesheets` stays in `prefs.js` after revert — make it a
   key-level location so revert is byte-identical (O4).

### W6 — User validation study [whole team; O2 coordinates]

1. Consent and ethics through the university; collect only what the study needs; no reading text is stored (A17).
2. Participants: readers with dyslexia, plus non-dyslexic readers as a comparison if possible.
3. Protocol: the same texts with default fonts vs each preset (order varied between participants).
4. Metrics: reading speed (words per minute), comprehension questions, errors, a comfort / preference score, and a
   standard usability score (e.g. SUS) for the app.
5. **WCAG 2.1 SC 1.4.12 check of the presets** (letter ≥ 0.12 em, word ≥ 0.16 em, line ≥ 1.5×) as an automated test
   (O1). **Open question to settle first — how to compare word spacing.** Measured 2026-10-24: Default's target
   +0.16 em is rounded on the 0.25 word_scale grid, so the space glyph grows by +0.130 em (Noto Sans), +0.140 em
   (Atkinson, Lexend) or +0.170 em (Carlito, word_scale 1.75); Reading gives +0.113 to +0.140 em; High Separation
   +0.339 to +0.420 em. Every word gap **also** gains the +0.12 em letter spacing of the letter before it, so the total
   gap grows more than the space alone. O1 to define the comparison against WCAG (space-glyph growth only, or the
   whole gap as CSS letter- + word-spacing would produce) and, if needed, round target_em up instead of to the nearest
   step.
6. Report coverage per app type and the known limits (browsers without the extension; apps needing a restart).

### W7 — Housekeeping and sign-offs

- Second-owner sign-offs (any of O1/O2/O3 + an entry here): K3 rule 10b (Brave key-level locations), K8 (socket in
  `XDG_RUNTIME_DIR`), K4 rule 5 `subscribe_style`, `list_all` in `contracts/store.py`.
- O3: `list()` drops the Custom row; add `gir1.2-gtk-4.0 gir1.2-adw-1` to CI; make `integration` a required check
  (with O2, repo settings).
- O1: review the three newly bundled fonts (M1.2); stale comments (`config/presets.py:156` and `:256`, the
  `fontgen/generator.py` docstring that says reference sources do not exist — `safety/font_references.py` does);
  `to_record` / `from_record` in `presets.py` have no tests and no users: test or remove.
- Repo: decide on the untracked `docs/*.pptx` / `*.tex`; keep `diagrams/` and `diagrams.zip` (≈35 MB of images) out
  of git.

### Suggested order

1. W7 + the W2 font-location blocker (small; unblocks packaging and import).
2. W1 contract change agreed → W1 import; in parallel W3 extension and W5.1 confirm window.
3. W2 RPM + COPR → W4 VM tests on the installed package.
4. W5 UI polish + accessibility audit.
5. W6 user study runs in parallel once a stable build exists (recruiting takes time — start the paperwork early).

### Future work (after Phase III — recorded, not planned)

- **GNOME and other desktops:** keybinds per desktop (GNOME custom shortcuts via gsettings, KDE, Sway); login
  recovery through XDG autostart or a systemd user service; KDE needs its own Qt font adapter (KDE apps do not use
  qt6ct).
- **ML / AI:** preset recommender from opt-in samples (the `recommendation_samples` table and the off-by-default
  "Help improve recommendations" switch exist). Needs consented data, a baseline (Default preset) and a metric from
  W6; start with simple per-user learning before any global model. Present it as recommendation, not detection
  ("dyslexia subtype detection" would need clinical evidence).
- **Phonetics (in the reader, where ScribeSense owns the text):** (1) read aloud with word highlighting — offline TTS
  (espeak-ng or Piper) through speech-dispatcher; (2) syllable view (`re·mem·ber` / alternating colours) from the CMU
  Pronouncing Dictionary with hyphenation rules as fallback; (3) pronunciation on click (IPA via `espeak-ng --ipa`);
  (4) letter-sound highlighting (silent letters, b/d/p/q). Adds `reader/speech.py`, `reader/phonetics.py`, a speech
  contract, error codes (e.g. `TTS_UNAVAILABLE`, `VOICE_MISSING`), settings (voice, rate, syllables on/off) and an
  optional per-user pronunciation-corrections table; text stays in memory and goes only to a local speech process.
- **AT-SPI system-wide text access** (phonetics or reading help inside other apps): large step with privacy impact.
- **Other distros:** AUR (if not done as the W2 stretch), openSUSE Build Service for `.deb` / openSUSE.

## O4 Phase III work (2026-11-19, with the user's go-ahead)

Done (details on the ARCHITECTURE cards M4.6, M4.9, M3.8, K3/K4 and K9 notes of this date):

| Task (Phase III plan) | Result |
|---|---|
| W5.6 Firefox/Zen `prefs.js` revert gap | Closed: the stylesheet pref is a K3 rule 10b key location inside `prefs.js`. Checked with real headless Firefox (throwaway profile): the pref survives Firefox rewriting `prefs.js`, revert removes it, it stays gone after another run. |
| W3.2 native-messaging host installer | `safety/native_host_install.py` (+ setup CLI `python -m scribesense.safety.native_host_install install\|remove\|status`); owns only its own file, never journaled (holds no user data), never overwrites or deletes a foreign file. |
| W3 uninstall | Step 3b in O3's `uninstall.py` (cross-owner, user's go-ahead; **for O3 to review**): removes the host manifests, kept while recovery is pending; the report tells the user to remove the extension. |
| W3.4 §9 recovery scenarios 5 and 6 | Written and passing on both journals (no longer skipped). |
| W7 `list_all` sign-off | **Signed off by O4** as second owner of O3's `PresetRepo.list_all()`. Human step: add to `contracts/store.py` under `PresetRepo.list`: `def list_all(self) -> list[Preset]: ...  # built-ins, the custom row, then user presets by name (font clean-up, K7 rule 3; added 2026-11-19: O3 + O4)`. O3 may then make `list()` drop the custom row. |

Tests after this work: **2224 passed, 0 failed** (unit; 34 new, 43 skipped — 41 of them sandbox-only) · integration
in bubblewrap 35 passed, 0 failed · extension JS 35 passed, 0 failed.

Still open for O4:
- **Human steps:** create the extension key outside the repo + `extension/tools/set_key.py`; run the installer;
  load the extension unpacked in Brave; check YouTube / Twitch restyle and revert.
- **Blocked:** W5.1 service-side API for the confirm window (waits for O2's M2.3 message design + a contract change);
  W1.6 imported fonts in `font_references.py` (waits for W1's contract change + O3's `user_fonts` table); W2 final host
  path and keybind / login-hook packaging step (waits for O3's packaging and the fonts-location fix); W4 live
  `hyprctl reload` + on-screen checks (waits for the VM + RPM).
- **Needs a second owner:** K3 rule 10b (now used by Chromium and Firefox), K8 (socket in `XDG_RUNTIME_DIR`),
  K4 rule 5 `subscribe_style`.
- **New finding (proposal):** a stale Firefox `lock` (dead PID) reads as "browser running", so after a crash Apply
  keeps asking to close the browser. Proposed: treat a native install's lock as stale when its PID no longer exists.

## K11 amendment: web fonts for the extension (2026-11-22, O4; O2 sign-off pending)

**Finding:** in Brave the extension's CSS reached YouTube / Twitch, but the pages drew their own font. Measured in the
page: our installed font fell back on websites (same width as the fallback) but drew on a local `file://` page; with
Brave Shields off it drew on every site. Brave documents its font protection as not applied on Linux, but Brave 158
on Fedora applies it. A font handed to the page as data (FontFace) drew with Shields on.

**Decision (O4; needs O2 as second owner):** K11 gains `font_key` / `styles` in the active `get_style` reply, a
`get_font` request (Latin web-font copy of the active set, one style per message, at most 256 KiB before base64)
and the error `FONT_UNAVAILABLE`. The contract lines were added by a human (patch applied 2026-11-22). The CSS rule of
M4.8 (no link to or embedding of a font file) stays; fonts reach the page as FontFace data. Zen / Firefox are
unaffected (`userContent.css` already works there).

**Built:** `adapters/webfont.py` (O4) · `get_font` route in `controller/service.py` (O2's file, cross-owner with the
user's go-ahead — **for O2 to review**) · native host forwarding · extension 0.1.1 (fetch, store, register before CSS;
also skips unloaded tabs and records why a tab was refused). Tests: **2244 passed, 0 failed** (unit) · extension JS
**44 passed, 0 failed**.

**Next:** automatic setup for browsers and apps on any machine (discovery, per-user setup on first run, Chromium
extension pre-installed by the package) — design to be agreed before building.

## Open browsers no longer block ScribeSense (2026-11-22, O4, user's go-ahead)

Problem seen in daily use: an open browser made Apply say "pending — close the browser", a restore with the browser
open left one item unresolved and then blocked every new apply (A3), and leftover locks made closed browsers look
open. Done now (O4 only):
1. **Stale locks** — `adapters/browser_lock.py`: Flatpak browsers by `flatpak ps`, native ones by the lock's pid;
   anything unclear stays "running". Fixed the Zen and Firefox cases seen today.
2. **Brave / Chrome / Chromium** — a profile styled by the ScribeSense extension (installed + enabled + our host
   installed) is no longer a target, so it never needs closing.
3. **Firefox / Zen** — `must_be_closed` is False: apply writes while the browser runs ("restart needed"); only the
   revert of the `prefs.js` line waits for it to close.

Still open (needs O2): a "finish when closed" watcher in the service (pending applies of the confirmed style and
unresolved reverts finish by themselves when a browser closes), relaxing A3 for items that only wait for an app to
close, and clearer messages ("Zen is open: this finishes when you close it"). An app showing the change only after a
restart is how apps work and is not treated as a problem (user, 2026-11-22).

Tests: **2263 passed, 0 failed** (unit).

## Automatic setup and repair — direction decided by the user (2026-11-22); design to be built (O4 + O2 + O3)

**Confirmed working 2026-11-22:** Brave with Shields on (extension 0.1.1 with web fonts); Zen everywhere (stylesheet).
Extension 0.1.2 also applies its own updates at once (see M4.9).

**Decision (user):** the user must never open a terminal. ScribeSense sets itself up on first start and after every
update, and re-checks before each Apply: what is missing is added by itself; anything that needs root is done in ONE
permission prompt at that moment (graphical, no terminal), covering every browser present — not only Brave.

**Proposed design (for review):**
1. `safety/setup.py` (O4) — a read-only CHECK that lists what is missing on THIS machine, discovered, never hard-coded
   to the dev machine: browsers present (Chromium family: Brave, Chrome, Chromium, Vivaldi, Edge…; Firefox family;
   Flatpak variants), our host manifest per Chromium browser, the extension pre-install file per browser, the
   launcher path still valid (e.g. after a venv rebuild), keybinds + login hook, the service start at login.
2. **User-level repairs run silently** (no prompt): host manifests, launcher, keybinds / login hook, service autostart.
3. **Root-level repairs in one prompt** via `pkexec` (polkit's graphical password dialog; the session needs a polkit
   agent — check it in `doctor`): copy the signed `scribesense.crx` to `/usr/share/scribesense/` and write
   `<extension-id>.json` (`external_crx` + `external_version`) into each present browser's system extensions folder.
   The root step is a small fixed helper with NO free arguments (install / remove only), so root never runs arbitrary
   input. Declined → remember until the next update; the app shows "browser extension not installed" with a button.
4. **The signed `.crx` is a release artifact:** built by the maintainer with the private key (never in the repo) and
   shipped with ScribeSense (it holds only the public key and a signature, so it is safe to distribute).
5. **Packaging (W2) does step 3 at install time** (RPM scriptlets run as root), so a packaged install never prompts;
   the in-app prompt covers repo / development installs and repairs.
6. Uninstall removes the pre-install files too (root, same helper).

**Facts to verify first:** which of the two Brave folders Brave reads (`/usr/share/brave/extensions/`,
`/opt/brave.com/brave/extensions/`); Chrome's (`/opt/google/chrome/extensions/`, `/usr/share/google-chrome/extensions/`
— documented by Google) and Chromium's / Vivaldi's / Edge's folders on Fedora and Arch; whether a polkit agent runs
under Hyprland (ML4W). Flatpak browsers cannot use a host outside their sandbox: reported as unsupported.

### Built (2026-11-22, O4): automatic setup and repair — first version

`safety/setup.py` + `safety/root_helper.py`, wired into the service (on start and after every Keep; cross-owner in
`controller/service.py` and `ui/app.py`, user's go-ahead, for O2 to review). Per-user repairs are silent; the root step
is one polkit prompt per extension version. Details on the M4.9 card. Tests: **2286 passed, 0 failed** (unit) ·
integration 35 passed · extension JS 45 passed.

**Still open for this feature:** a polkit agent on the user's session (`doctor` should check it; ML4W / Hyprland has
none running by default here); the maintainer builds the signed `.crx` into `extension/dist/scribesense.crx` per
release; `scribesense uninstall` should also run `root_helper remove` (one prompt); verify Chrome's / Chromium's
folders on a VM; the app's banner and button for "browser extension not installed" (O2); the RPM does the root step
at install time (O3, W2); keybinds / login hook / service start at login as silent repairs (needs O2's CLI change for
starting the service at login). Rust for the root helper and the native host is a team option after Phase III (the
full app stays Python: the slow parts are external font tools, and the security comes from the design).

### Done (2026-11-22, O4): uninstall removes the extension · doctor checks the polkit agent

`scribesense uninstall` step 3c removes the pre-installed extension (one password prompt; only when our files exist).
`scribesense doctor` reports a missing polkit agent with a fix hint. Unit tests can no longer see real system folders
(sandbox redirect in `tests/conftest.py` + guard test). Cross-owner (O3's `uninstall.py`, `doctor.py`,
`tests/conftest.py`) with the user's go-ahead — for O3 to review. Tests: **2294 passed, 0 failed** (unit) ·
integration 35 passed.
    ---

## W2.1 font location fixed + O1 sign-offs (2026-11-14, O1)

### What was wrong

    `config/bundled.py` located the bundled font tree with a module constant,
    `FONTS_ROOT = Path(__file__).resolve().parents[3] / "fonts"`. In a checkout this file is
    `<repo>/src/scribesense/config/bundled.py`, so three parents reach the repository root. In an
    **installed** package there is no `src/` layer — `uv_build` is a src-layout backend and ships
    `scribesense/` alone — so the same three parents land on the Python library directory
    (`/usr/lib/python3.14`, not `/usr/share/scribesense`). Nothing raised: `base_font_files()` simply
    found no directory and **every** build was refused with `BUNDLED_FONT_MISSING`. An installed RPM
    would have shown the user "Preview unavailable" and "Nothing changed" on first Apply.

    Demonstrated before/after on the dev machine: with the real `fonts/` tree copied to
    `<share>/scribesense/fonts`, the old constant resolved to the repo and found nothing at the
    installed path; the new resolution finds all four families there.

### What changed (`src/scribesense/config/`, `src/scribesense/fontgen/`)

    - **`config/bundled.py`** — the `FONTS_ROOT` constant is **removed** and replaced by
      `fonts_root()`, `candidate_roots()`, `MANIFEST_NAME` and `BUNDLED_FONTS_ENV`.
      Search order: `$SCRIBESENSE_BUNDLED_FONTS` (the only candidate when set, so a test pointing at an
      empty directory cannot fall through to the real fonts) → `$XDG_DATA_DIRS` entries as
      `<dir>/scribesense/fonts`, defaulting to XDG's own `/usr/local/share:/usr/share` → the repository
      tree, which is this card's "the repo path as the development fallback". A candidate counts only if
      it carries `manifest.toml`, so `/usr/share/fonts` cannot be mistaken for ours. `fonts_root()` never
      raises and never returns `None` — when nothing matches it returns the last candidate, so the
      refusal still comes from `base_font_files()` with K2's `BUNDLED_FONT_MISSING` and **the error
      contract is unchanged**. The failure `detail` now names every directory searched (a packaging
      failure and a genuinely unbundled family need opposite fixes).
    - **A function, not a constant,** for the reason every K8 path is a function: the value must be
      re-read so the sandbox (M3.3) and tests can redirect it *after* import. This removes the
      `monkeypatch.setattr(bundled, "FONTS_ROOT", ...)` pattern from the suite.
    - **`fontgen/variants.py`** — re-exports `fonts_root` instead of `FONTS_ROOT` (it was a pure
      re-export; nothing in `fontgen` used the value). No other owner's module referenced it.
    - **Follow-through outside those two folders** (required, or the suite would not import):
      `tests/unit/fontgen/test_config.py` (two sites), `tests/unit/fontgen/test_bundled_fonts.py`,
      `tests/unit/test_font_references.py` — `setattr` on the constant became `setenv` on
      `BUNDLED_FONTS_ENV`, and `test_fonts_root_is_the_repository_fonts_dir` was rewritten (it pinned the
      broken behaviour).
    - **New tests: `src/scribesense/config_and_fontgen_tests/`** (12, all passing), placed beside the code
      at O1's request rather than under `tests/`. The regression test
      `test_an_installed_layout_resolves_the_real_families` copies the real font tree to an installed path
      and proves the pipeline finds every shortlist family there; it fails against the old constant.

### O1 approved — waiting for O3's approval (K8 owner)

    **O1 approved** the fix above and has it building and tested. Three parts touch files O1 does not own.

    **Tasks for O3:**

    1. **Decide where the resolution lives.** W2.1 says "resolve it through K8 paths". `fonts_root()`
       currently sits in `config/bundled.py` because `contracts/` is frozen and agents may not edit it.
       If K8 should own it, move `fonts_root()`/`candidate_roots()` into `contracts/paths.py` and have
       `bundled.py` call them — the name and the search order are what need agreeing, not the mechanism.
    2. **Approve the environment variable `SCRIBESENSE_BUNDLED_FONTS`** and add it to K8's documented
       override list (today: `HOME`, `XDG_CONFIG_HOME`, `SCRIBESENSE_DATA_DIR`, `SCRIBESENSE_FONTS_DIR`).
       Deliberately *not* `SCRIBESENSE_FONTS_ROOT`: `SCRIBESENSE_FONTS_DIR` already means the opposite end
       of the pipeline (where generated fonts are installed), and two variables one letter apart meaning
       opposite things is a defect waiting to be filed.
    3. **Decide the search-order question, and set the variable in the sandbox.** The order above is
       W2.1's as written — installed first, repo last. It only matters when **both** exist, i.e. a
       developer who also has the RPM installed: their unit tests would then silently measure the
       installed fonts. `tests/conftest.py` sets `XDG_DATA_HOME` but not `XDG_DATA_DIRS`, so this is real
       once W2 ships. Either set `SCRIBESENSE_BUNDLED_FONTS` to the repo tree in `tests/conftest.py`, or
       flip the order so a checkout wins (one assertion in
       `test_an_installed_tree_wins_over_the_repository_tree` pins whichever you choose).
    4. **`pyproject.toml` (O3's file):** `testpaths = ["tests"]` means a bare `pytest` run does **not**
       collect `src/scribesense/config_and_fontgen_tests`. Until that path is added, CI does not run these
       tests. Run them meanwhile with `pytest src/scribesense/config_and_fontgen_tests`.
    5. **Packaging, the other half of this fix.** The code looking in `/usr/share/scribesense/fonts` only
       helps if the RPM installs the tree there — `<Family>/*.ttf` plus `manifest.toml`, plus each
       family's `OFL.txt` under `/usr/share/licenses/scribesense/` (W2.3). Agree the exact path with O1
       before the spec file is written. Also still open from W2.1: `extension/` files for the
       native-messaging host installer (same bug shape, O4's module), and whether
       `store/migrations/*.sql` are included in the built package.
    6. **Decide whether a test package should ship.** `uv_build` packages everything under
       `src/scribesense/`, so `config_and_fontgen_tests/` goes into site-packages on install. Harmless,
       but an RPM reviewer will ask. Exclude it in `pyproject.toml` or move it under `tests/`.

    **Note for O3 (not caused by this change, found while verifying):** the `tests/` suite cannot run on
    a Windows development machine at all — `tests/conftest.py:93` hardcodes `dir="/tmp"`, and
    `tests/unit/test_store_core.py:263` calls `os.geteuid()`, which does not exist on Windows.
    `tests/unit/test_import_rules.py` also reads source files without `encoding=`, so it dies on cp1252.
    O1 therefore **could not run the existing suite** to verify the four edited test files; they were
    checked by compilation and by symbol resolution instead, and the new package was checked against
    rules R2/R3/R4 with an equivalent UTF-8 check (clean). Someone on Linux should run the full suite
    before this is merged.

### O1 sign-offs given (2026-11-14)

    - **M1.2 bundled fonts — reviewed and approved by O1.** All four `SHORTLIST` families are bundled with
      OFL-1.1, every `licence_file` exists and carries the "SIL Open Font License, Version 1.1" text, every
      recorded SHA-256 matches the file on disk, and `manifest.toml`'s file set equals what the pipeline
      finds for each family. Every bundled face passes M1.3 eligibility. **Reserved Font Name compliance
      verified by building all four families:** no primary name record (1, 3, 4, 6, 16, 17, 21, 22, 25) of
      any generated artifact carries the source family or its RFN ("Carlito"; Lexend's "RevReading Lexend"),
      so OFL clause 3 is satisfied. Lexend ships Regular + Bold only (no italic upstream) — correct per
      K7 rule 4; it builds 4 artifacts where the others build 8. **This closes "O1: review the three newly
      bundled fonts (M1.2)" in W7 and K2 rule 2's "before bundling, O1 verifies each licence".**
    - **M1.4 "Done when: tests pass for all 4 shortlist fonts" — met and closed by O1.** The condition was
      blocked by an empty `fonts/`; all four now patch and build (8/8/4/8 artifacts). O3's review note (4)
      is answered. **CFF/OTF remains untested** — carried into W1.3 as planned, not claimed as done.
    - **M1.8 integration (2026-10-15, O4) — reviewed and approved by O1.** Verified: `fc_pattern()` escapes
      the dash (`AccessSansUI\-3f9a1c2b7d10`) so fontconfig no longer reads it as family-size; K1 rule 1
      rounding is Python's ties-to-even as the contract writes it (0.01 -> 0.00, 0.03 -> 0.04);
      `fontgen/validate.py` and `fontgen/cache.py` import no `subprocess` and go through `run_cmd`
      (K3 rule 9, import rule R2); `cache.fonts_dir()` delegates to `contracts.paths.fonts_dir()`.
    - **K7 `PreviewFonts` — confirmed by O1.** `generator.PreviewFonts` is a subclass of K7's with
      **identical fields** (`reading_family`, `ui_family`, `files`, `styles`, `temp_dir`), adding only
      `close()`, `__enter__` and `__exit__`, and deliberately **no `__del__`**. `FontBuildResult` keeps K7's
      three fields and adds two defaulted ones. **This closes "confirm `PreviewFonts` fields" in the O1
      checklist.**

### Still open for O1 (not touched by this entry)

    - Reviews O1 owes on other owners' cards: **M2.5** (O2) and **M4.2** (O4).
    - Second-owner sign-offs O1 could give: K3 rule 10b, K8 socket, K4 rule 5 `subscribe_style`,
      `list_all` in `contracts/store.py`.
    - W7 housekeeping in O1's own code: the stale docstrings at `config/presets.py:156` and `:256` and in
      `fontgen/generator.py`; `to_record`/`from_record` have no tests and no users.
    - **W6.5 WCAG word-spacing question** — still O1's to settle, and it is a real defect, not only a test:
      `target_em` is rounded to the *nearest* 0.25 `word_scale` step, so Default's "+0.16 em" resolves to
      +0.130 em on Noto Sans — below the WCAG 1.4.12 floor the preset documents itself as meeting. Decide
      whether the comparison is space-glyph growth or the whole word gap, and whether `target_em` should
      round **up**. Analysis in `docs/research2-conformance-O1.md` §9.1–§9.2.
    - W1 user font import (contract change first), and the matching card notes in `ARCHITECTURE.md`
      (M1.2, M1.4, K2) for this entry — written here only, as instructed.

    ---

## M1.2 bundled-font review + M1.4 "all 4 shortlist fonts" closed (2026-11-14, O1)

    Tasks 1.2 and 1.3 of O1's remaining-work list. Both were *verified* in the 2026-11-14 entry above;
    this entry turns those one-off terminal checks into tests, and records two coverage gaps the review
    actually found. **No defect was found in the implementation** — `config/` and `fontgen/` needed no
    source change for either task.

### Correction to the entry above

    That entry said "**CFF/OTF remains untested**". That is wrong and is withdrawn:
    `tests/unit/fontgen/test_fontgen.py::test_measured_advances_match_the_configuration` is
    parametrized `[("a.ttf", {}), ("a.otf", {"cff": True})]`, so **CFF patching is tested** on a
    synthetic font. What is missing is *real-font* CFF coverage — no bundled family is CFF. The
    accurate statement is in "What bundling did not close" below.

### M1.2 — review of the three newly bundled fonts: **APPROVED by O1**

    Carlito, Atkinson Hyperlegible and Lexend were bundled by O4 on O1's behalf (2026-10-18) and marked
    "for O1 to review". K2 rule 2 puts the licence duty on O1 personally. Reviewed and approved:

    - **Licences.** All four declare OFL-1.1; each `licence_file` exists, carries the real
      "SIL Open Font License, Version 1.1" text, and its grant names both verbs K2 rule 2 requires —
      *modify* and *redistribute*. Checked against the licence text, not the SPDX string in the manifest.
    - **Reserved Font Names.** Carlito reserves "Carlito"; Lexend reserves "RevReading Lexend"; Atkinson
      and Noto Sans reserve nothing. Every bundled face was patched and **no identifying name record
      (1, 3, 4, 6, 16, 17, 21, 22, 25) of any generated artifact carries the source family or its RFN**.
      OFL clause 3 satisfied.
    - **Integrity.** Every manifest SHA-256 matches the file on disk; the manifest's file set equals what
      `base_font_files()` finds for each family; every face passes M1.3 eligibility (the only warnings
      are `ITALIC_ONLY` on italic faces, which is the known per-face noise, not a defect).
    - **Partial styles (K7 rule 4 `[V]`).** Lexend ships Regular + Bold only — upstream has no italic.
      Correct under R2; it plans and builds 4 artifacts where the others build 8, with no refusal and no
      synthesised style.
    - **Measured metrics.** Space widths: Carlito 0.226 em, Atkinson 0.280, Lexend 0.280, Noto Sans
      0.260 — matching what ARCHITECTURE.md M1.2 records.

    **Two coverage gaps found, both now closed:**

    1. **`manifest.toml`'s `reserved_font_name` field was dead documentation** — no test read it. The
       existing check searched for the *family* name (`family.split()[0]`), which covers today's four
       only by coincidence; a future import whose RFN differs from its family name (e.g. "Source Sans 3"
       reserving "Source") would have slipped past. A new test now reads the declaration and enforces it.
    2. **`tests/unit/fontgen/test_bundled_fonts.py` checked name records `(1, 4, 6, 16, 17)` and omitted
       record 3** (`uniqueID`) — **which is the one record every bundled source demonstrably carries its
       family name in** (`'1.104;tyPL;Carlito-Bold'`, `'2.015;GOOG;NotoSans-Regular'`, …). M1.4 *rewrites*
       record 3 rather than dropping it, and it does so correctly, so nothing was leaking; but the check
       that would have caught a regression was not looking there. Records 16/17/21/22/25 are absent from
       every bundled source, so record 3 was the only live exposure. **`PRIMARY_NAME_IDS` widened to
       `(1, 3, 4, 6, 16, 17, 21, 22, 25)`** — the set M1.6 uses — and re-verified against all 14 bundled
       faces. Fixed in the existing file on purpose: it runs in CI, and the new test package does not yet
       (see the `testpaths` task for O3 above).

    **This closes "O1: review the three newly bundled fonts (M1.2)" in W7 and K2 rule 2's licence duty.**

### M1.3 — M1.4 "Done when: tests pass for all 4 shortlist fonts": **MET and closed by O1**

    O3's M1.4 review note (4) said the condition could not be met while `fonts/` was empty. It is met now.
    The card's acceptance criteria are asserted **directly on the patched bytes of every style of every
    bundled family** — advance delta `== round(letter_em × unitsPerEm)` for every Latin glyph, space
    advance `== round(original × word_scale)`, every non-Latin advance byte-identical, no `liga` in GSUB,
    the line box at the requested ratio, the family renamed. Previously every family was known to *build*
    (`status == "built"`), which reaches these criteria only through M1.6; a spacing regression would have
    been reported as a validation failure two modules away.

    Also asserted on the real fonts: byte determinism (patch twice → identical bytes) and Research2 I.4's
    "a zero-spacing configuration must build successfully" (`letter_em` 0.00, `word_scale` 1.0 → every
    advance unchanged).

    **What bundling newly closed, and it is worth noting:** **Carlito is UPM 2048** where the other three
    are 1000. Research2 E.3 warns that "any test asserting a fixed delta must be parameterised by
    unitsPerEm", and E.2.1's worked example is 0.12 em at 2048 = +246 units. Until Carlito was bundled
    that path existed only in a fixture; the shortlist now covers both conventional em values with real
    fonts.

    **What bundling did NOT close — stated rather than claimed:**

    - **Real-font CFF.** Every bundled family is TrueType (`glyf`). CFF patching is covered by a synthetic
      OTF; no bundled font exercises the CFF charstring path.
    - **Variable-font flattening.** Lexend's *upstream* is variable, but `fonts/tools/build_lexend.py`
      flattens it to static Regular and Bold before bundling, so the patcher's `instancer` path never runs
      for the shortlist.
    - **Hinting and real kerning** remain undemonstrated, as O3 noted; both are M1.6 rendering concerns and
      need the VM (W4).

    Two **tripwire tests** record this: they assert that no bundled family is CFF and none is variable, so
    bundling one later *fails the suite* and prompts the card note to be updated, instead of the claim
    quietly widening. Real-font CFF/OTF coverage stays in **W1.3** (user font import), where OTF/CFF
    sources are already listed as needing tests.

### Code changes

    - **`src/scribesense/config/` and `src/scribesense/fontgen/`: none.** Both tasks are reviews; the
      implementation was found correct. The only source-tree change is the widened `PRIMARY_NAME_IDS`
      constant in O1's existing test file.
    - **New: `src/scribesense/config_and_fontgen_tests/test_bundled_font_review.py`** — 33 tests (2 skip:
      the two families with no RFN). Licence grant text, RFN enforcement read from the manifest, the
      record-3 premise, all nine identifying records on real patched fonts, manifest↔pipeline style
      agreement, Lexend's missing italic, per-family eligibility, measured space widths, and the
      per-family Default resolution.
    - **New: `src/scribesense/config_and_fontgen_tests/test_shortlist_patching.py`** — 25 tests. The M1.4
      acceptance criteria per family per style, UPM pinning, byte determinism, zero-spacing neutrality,
      and the two tripwires.
    - **Changed: `tests/unit/fontgen/test_bundled_fonts.py`** — `PRIMARY_NAME_IDS` widened (gap 2 above).
    - New-package total: **68 passed, 2 skipped**. Import rules R2/R3/R4 clean.

### One test deliberately pins a known defect

    `test_default_resolves_per_family_and_three_of_four_miss_the_wcag_floor` asserts today's behaviour:
    Default's "+0.16 em" target resolves to a space-glyph growth of **+0.130 em (Noto Sans), +0.140
    (Atkinson, Lexend), +0.170 (Carlito)** — so **only Carlito clears WCAG 1.4.12's 0.16 em floor**, because
    its narrow 0.226 em space pushes the quotient past the halfway point of K1's 0.25 grid. The other three
    round *down*, below the floor the preset documents itself as meeting.

    This is **W6.5, still open and still O1's**, not a fault of the bundled fonts, so it does not block this
    review. The test exists so that whichever way W6.5 is settled — round `target_em` up, or redefine the
    comparison as the whole word gap — the change is visible at this assertion rather than silent. Analysis:
    `docs/research2-conformance-O1.md` §9.1–§9.2.

### Nothing here needs another owner's approval

    Both tasks are O1's own (K2 rule 2's licence duty; the M1.4 card O1 owns and O3 has already reviewed).
    No contract is touched and no other owner's file is changed. The tasks for O3 listed in the entry above
    — `testpaths`, the K8 decision, the RPM font path — are unchanged and still apply; in particular
    **these 68 tests do not run in CI** until `src/scribesense/config_and_fontgen_tests` is added to
    `testpaths`, which is why gap 2 was fixed in the existing CI-visible file rather than only here.

    ---

## M1.8 integration review + K7 `PreviewFonts` confirmed (2026-11-14, O1)

    Tasks 2.1 and 2.2 of O1's remaining-work list. ARCHITECTURE.md's M1.8 note records nine changes O4
    made to O1's modules on 2026-10-15 and ends "**O1 to review**"; K7's note adds "``build_preview()``
    keeps the provisional `PreviewFonts` shape exactly (**O1 confirms it**)".

    The 2026-11-14 entry above recorded a *spot check* of four of those claims. This is the full review:
    **all nine claims verified, integration approved**, no source change required. One finding on
    `PreviewFonts.styles` needs a K7 decision (below).

### M1.8 integration (2026-10-15, O4) — **APPROVED by O1**

    | # | Claim in the note | Verified |
    |---|---|---|
    | 1 | Modules moved to `src/scribesense/config` and `src/scribesense/fontgen` | Yes — both import and the pipeline runs end to end |
    | 2 | `fc-match` dash bug fixed; patterns go through `naming.fc_pattern()` | Yes — `fc_pattern("AccessSansUI-3f9a1c2b7d10")` → `AccessSansUI\-3f9a1c2b7d10`; both `fontgen/validate.py` and `fontgen/cache.py` call it. **Every** generated family carries a `-` (the key suffix), so this was never an edge case — it was every build |
    | 3 | Contracts used, not copied | Yes — `settings.Configuration/Issue/Range/RANGES/SHORTLIST`, `presets.WordSpacing/PresetValues/Preset/BUILTIN_IDS/ORIGINAL` and `generator.FontSet` are the **identical objects** from `contracts/`, not copies; `FontBuildResult` and `PreviewFonts` subclass K7's |
    | 4 | K1 rule 1 rounding restored to Python's `round()` (ties to even) | Yes — 0.01 → 0.00, 0.03 → 0.04 on the `letter_em` grid, as the contract writes it |
    | 5 | K3 rule 9 / import rule R2: `run_cmd`, not `subprocess` | Yes — neither `validate.py` nor `cache.py` imports `subprocess`; the environment goes in as `env NAME=value …` in front of argv |
    | 6 | K8: `cache.fonts_dir()` delegates to `contracts.paths.fonts_dir()` | Yes — the body is `return paths.fonts_dir()` |
    | 7 | Exceptions are `ScribeSenseError` subclasses, message = bare code, no paths | Yes — checked on all five refusal paths (`BundledFontMissing`, `PatchUnsupported`, `InstallFailed`, `RemoveRefused`, `PreviewUnavailable`): `str(exc)` is the code, no `/` or `\`, diagnosis on `.detail`/`.reason_code` |
    | 8 | K2 `base_font_files()` moved to `config.bundled`, re-exported by `config.presets` and `fontgen.variants` | Yes — all three names are the **same function object** |
    | 9 | `resolve()` measures the bundled Regular face's space width | Yes — Noto Sans reports `measured` 0.26 em and Default resolves to `word_scale` 1.5 (the 0.25 em fallback gave 1.75). The note's "**Open (O1):** no base fonts are bundled yet" is closed |

    **One clarification to claim 7, so the record is accurate:** not every exception in O1's area is a
    `ScribeSenseError`, and that is correct rather than an oversight. `PresetResolutionError`,
    `SpaceWidthUnavailable`, `BuiltinOverwriteError` and `PresetRecordError` are `ValueError`/`LookupError`
    subclasses because they report **caller** errors, not font or environment problems — the same split
    `patch()` makes between `ValueError` (unvalidated config) and `PatchUnsupported` (bad font). Folding
    the first into the second would let a bad configuration masquerade as an unpatchable font, and M1.8
    would report `refused`/`PATCH_UNSUPPORTED` with the real fault invisible.

    **The note's two "behaviour to know" items are confirmed and now tested** (they were prose only):
    tracking reaches Latin letters but not digits or punctuation (script Common), and a line height below
    the font's natural ascent + descent cannot be expressed.

### K7 `PreviewFonts` — **FIELDS CONFIRMED by O1**, one semantic finding raised

    The provisional shape is the right shape and is hereby confirmed: `reading_family`, `ui_family`,
    `files`, `styles`, `temp_dir`, with `generator.PreviewFonts` adding `close()`/`__enter__`/`__exit__`
    and **no `__del__`** (so loaded preview files are never deleted behind the caller's back). **This
    closes "confirm `PreviewFonts` fields" in the O1 checklist.**

    **Finding — `styles` means two different things in one contract. Needs a K7 decision.**

    K7's `PreviewFonts` says *"styles — styles actually generated, in the same order as files"*, so
    `build_preview()` returns one entry per file. Both variants are built, so every style appears twice:

    ```
    files  = (AccessSans-<key>-Regular.ttf, …, AccessSansUI-<key>-Regular.ttf, …)   # 8 for Noto Sans
    styles = ("Regular","Bold","Italic","BoldItalic","Regular","Bold","Italic","BoldItalic")
    ```

    K7's sibling `FontSet.styles` — same contract, same field name — is the **deduplicated** set
    (`("Regular","Bold","Italic","BoldItalic")`, 4 entries). So `len(result.styles)` is 4 or 8 for one
    configuration depending on which object the caller holds.

    Both implementations match their own wording and nothing is broken. It is worth settling now because
    **no caller reads `PreviewFonts.styles` today** — O2's `ui/preview.py` uses `files` and `temp_dir`
    only — so the field is free to change. After someone writes `for style in preview.styles` it is not.

    **O1 approved — waiting for the K7 owner's approval plus one other owner.**

    **Tasks for the K7 owner (O1 is K7's owner, so this needs a second owner — O2, O3 or O4):**

    1. **Decide the shape of `PreviewFonts.styles`.** O1's recommendation: make it the **deduplicated
       set**, identical in meaning to `FontSet.styles`, and change K7's wording from "in the same order as
       files" to match. The per-file mapping is not lost — it is recoverable from the filename, which is
       exactly how M1.6 (`_style_from_filename`) and M1.7 (`_identify`) already recover it.
    2. **If the parallel-array reading is kept instead,** rename the field (e.g. `file_styles`) so the two
       K7 types do not use one name for two shapes.
    3. **Either way, the change is in `contracts/fonts.py`,** which is frozen and which agents may not
       edit — a human applies it. O2 should confirm `ui/preview.py` and `ui/preset_editor.py` are
       unaffected (they should be; neither reads the field today).

### Code changes

    - **`src/scribesense/config/` and `src/scribesense/fontgen/`: none.** Both tasks are reviews and the
      implementation was found correct.
    - **New: `src/scribesense/config_and_fontgen_tests/test_integration_review.py`** — 14 tests pinning the
      claims that were verified by hand but not covered by a test. Specifically:
      - `tests/unit/fontgen/test_config.py::test_vocabulary_is_the_k1_contract_not_a_copy` covers **K1
        only**; K2's and K7's types were never asserted to be the contract's own objects. Now they are.
      - `test_refusals_carry_error_codes` checks `FontBuildResult.reason_code`; nothing checked the
        **errors contract itself** — that a raised exception's message is the bare code with no path.
        That is now parametrized over all five refusal paths. O1's modules handle paths constantly, so a
        path leaking into a log through an exception message is the realistic failure here.
      - The deliberate `ValueError` / `ScribeSenseError` split is now asserted, so "tidying" it into one
        hierarchy fails the suite instead of hiding bad configurations as bad fonts.
      - The fontconfig escaping bug is pinned **at its cause** (every generated family contains a `-`),
        not only at the symptom.
      - The two "behaviour to know" items, on the real bundled font.
      - `PreviewFonts` field shape, and the `styles` finding above.
    - New-package total: **82 passed, 2 skipped**. Import rules R2/R3/R4 clean.

### One test deliberately pins a second known defect

    `test_a_line_height_below_the_fonts_natural_height_is_a_no_op_but_still_keys` asserts both halves of
    the behaviour the M1.8 note mentions only half of. Noto Sans' ascent + descent is 1.362 em and
    `line_gap_units` clamps at zero, so **`ui_line` 1.0, 1.1, 1.2 and 1.3 all produce byte-identical
    artifacts** — and all three built-in presets ship `ui_line` 1.2, inside that band. But `ui_line` is in
    `KEYED_FIELDS`, so those identical artifacts carry **four different content keys**: a false miss, i.e.
    a redundant rebuild and a second install under a new family name.

    Research2 F.1's open item D-08 says the line fields must leave the key exactly when the artifacts are
    byte-identical. M1.5 closed D-08 on "the patcher does write vertical metrics" — true in general, false
    in this band, which is the band every built-in uses. The direction of error is safe (never a wrong
    cache hit), but it was undocumented. Analysis: `docs/research2-conformance-O1.md` §9.3. Not fixed here:
    it is an M1.5 key-composition change and belongs with the W6.5 / key work, not with a review.

    ---

## W7 housekeeping in O1's own code (2026-11-14, O1)

    Task 3 of O1's remaining-work list: the stale docstrings and the untested, uncalled
    `to_record` / `from_record`. All four items are closed. One turned out to be bigger than
    housekeeping and is recorded as an open item below.

### 3.1 `config/presets.py` CUSTOM_ID docstring — already done

    Found already fixed in the working tree: `"""ID of the transient Custom entry."""` now says
    Custom is **persisted** (K9 D3, one row on the reserved id) and explains "stored is not the
    same as listed". Not touched by this entry.

### 3.2 `config/presets.py` REFERENCE_SPACE_WIDTH_EM docstring — corrected

    It said the Default preset's resolved word spacing "is genuinely undetermined until the
    bundled fonts are measured". They have been measured since 2026-10-18. The docstring now
    records the measurements and what they produce:

    | Family | space | `word_scale` | space glyph grows by |
    |---|---|---|---|
    | Carlito | 0.226 em | 1.75 | **+0.170 em** |
    | Noto Sans | 0.260 em | 1.50 | +0.130 em |
    | Atkinson Hyperlegible | 0.280 em | 1.50 | +0.140 em |
    | Lexend | 0.280 em | 1.50 | +0.140 em |

    — i.e. **three of the four land below the 0.16 em WCAG 1.4.12 floor Default is built on**,
    because rounding to the *nearest* 0.25 step rounds down. That is W6.5, not a property of the
    fonts. The constant is no longer reached by any bundled family; it still fires for an
    unreadable file, and is what a W1 imported font would fall back to before its metrics are read.

### 3.3 `fontgen/generator.py` — four stale claims corrected, and one real finding

    The module docstring and two Protocol docstrings asserted that things do not exist. All four
    were wrong:

    | Claimed | Reality |
    |---|---|
    | "**M3.1 does not exist.**" (×2) | Built 2026-10-15; `store/` ships `FontRegistryRepo` |
    | "none of them exists yet" (K7 rule 3's five sources) | O4 built `safety/font_references.py` 2026-10-17 |
    | "**Nothing implements it yet**" (`ReferenceSource`) | `FontReferenceSource` does, and `controller/service.py` passes one to `remove_unused()` at start-up |
    | "`base_font_files`, which does not exist yet (K7 E5)" | It exists; all four families bundled since 2026-10-18 |

    A docstring that is confidently wrong is worse than none — the next reader believes it.

    **Finding while correcting them, and it is not cosmetic: no font registry row is ever
    written.** M3.1 exists, but its `FontRegistryRepo` and M1.8's `FontRegistry` seam are
    **different shapes**:

    - `FontRegistry` (M1.8): `record(Mapping)` / `forget(key)`
    - `FontRegistryRepo` (M3.1): typed `FontRegistryEntry` through `create()` / `set_status()` /
      `touch()` / `mark_removed()`

    Nothing converts between them, and no caller passes `registry=` to `build()`, so `NullRegistry`
    is still the default: `_record()` assembles a full row on every successful build that nobody
    stores. Nothing depends on it for correctness — Research2 I.1 makes the filesystem the
    authority and `installed_sets()` the reconciliation path — but the M1.8 card says this module
    "owns font registry entries", and today it owns entries that go nowhere.

    **O1 approved the correction — the bridge itself needs O3.**

    **Tasks for O3 (M3.1 owner):**

    1. **Decide which shape survives.** O1's `FontRegistry` Protocol was written before
       `FontRegistryRepo` existed and has no claim to be the right one; `FontRegistryRepo` is
       richer (it models the `building -> validated | failed -> removed` status flow that M1.8
       does not express). O1's recommendation: **keep `FontRegistryRepo`** and have M1.8 build a
       `FontRegistryEntry` directly, deleting the `FontRegistry` Protocol and `NullRegistry`.
    2. **Decide who constructs the entry.** `FontRegistryEntry` needs `file_sha256` and timestamps;
       M1.7's manifest already computes the digests, so M1.8 can supply them without re-hashing.
    3. **Decide where it is wired** — `controller/service.py` already builds the `FontReferenceSource`
       at start-up and is the natural place to pass a registry into `build()`.
    4. Until then the status quo is safe, not broken: no row is written, and nothing reads one.

### 3.4 `to_record` / `from_record` — **removed** (W7's "test or remove")

    **Removed, and the duplication was the reason.** W7 offered "test or remove". Investigating
    which, the answer settled itself: **M3.1 already has the same serialiser, and it is the one
    with callers.** `store/serialize.py` implements K9 D9's versioned JSON with the same
    fixed-decimal spelling; `PresetRepo.save()` calls `preset_values_to_json` and `PresetRepo.get()`
    calls `preset_from_row`. O1's pair had **no caller and no test**.

    Checked before removing: the two produce **identical `values` payloads**, field for field,
    including every fixed-decimal string. So nothing was lost and neither was silently wrong —
    the defect was that there were two. Two implementations of one record shape, in two owners'
    modules, is a second authority on what a preset legally contains. `to_record`'s own docstring
    made exactly that argument one level down ("Persisting both would let them drift, and the one
    that drifts is the one the font was built from"); it applies to the record shape itself.

    No contract required them: K2's `PresetApi` protocol lists `BUILTINS`, `resolve` and
    `base_font_files` only.

    Removed: `to_record`, `from_record`, `PresetRecordError`, `RECORD_VERSION`, `_FIELD_DECIMALS`,
    `_WORD_DECIMALS`, `_text`, `_number`, and the now-dead `Mapping` / `Any` / `decimals_of` imports
    — 245 lines, about a third of the module. **Kept:** the preset *model* the store serialises —
    `STORABLE_KINDS`, `RESERVED_IDS`, `CUSTOM_ID`, `is_storable`, `_check_custom`. Recoverable from
    git if the decision is revisited.

    **Loose end for M3.1, not blocking:** `PresetRepo.save()` re-states the storable-kind policy
    inline next to its SQL rather than calling `is_storable()`, which exists for exactly that. One
    predicate is not the duplication problem the serialisers were, but it is the same shape of
    problem one size smaller.

### Code changes

    - **`src/scribesense/config/presets.py`** — 3.2 docstring corrected; 3.4 removal (−289 lines net).
    - **`src/scribesense/fontgen/generator.py`** — 3.3: four stale claims corrected, the registry
      mismatch written down where the seam is defined.
    - **New: `src/scribesense/config_and_fontgen_tests/test_single_authority.py`** — 8 tests guarding
      both kinds of rot: that the serialiser does not come back (parametrized over the four removed
      names), that the surviving store serialiser round-trips, that the preset model is intact, and
      that each corrected docstring's **premise** still holds — M3.1 exists, the registry shapes still
      do not match and nothing bridges them, `FontReferenceSource` is implemented and wired,
      `base_font_files` exists. If one stops being true the suite says so, instead of the prose
      quietly rotting again.
    - **Updated: `test_integration_review.py`** — dropped the reference to the removed
      `PresetRecordError`.
    - New-package total: **92 passed, 2 skipped**. Import rules clean; the whole application still
      imports.

### One mistake worth recording

    The block removal initially took `is_storable` with it — it sat inside the "Storage records"
    section — contradicting the module docstring written in the same change, which says
    `is_storable` stays. Caught by the new `test_what_the_model_still_owns_is_still_here`, which was
    written before the removal and failed on it. Restored.

### Raised for O3: import rule R4 matches on a string prefix

    `tests/unit/test_import_rules.py` applies R4 (config/ must not import ui, store, adapters,
    safety, controller, reader, gi) to any module whose name **starts with** `"scribesense.config"`.
    That catches `scribesense.config_and_fontgen_tests` by accident of the name, and would catch any
    future `scribesense.config_*` module whether or not that is intended. Harmless here — the new
    package genuinely has no static dependency on those layers, and `test_single_authority.py` loads
    them with `importlib` inside test functions precisely so it does not create one (noted in that
    file, so it does not read as dodging the check). Worth making the match a package boundary rather
    than a prefix.

    ---

## W1 user font import — contract proposal, and the two parts that did not need it (2026-11-14, O1)

    Task 4 of O1's remaining-work list. W1.1 says the contract change comes with "a decision entry
    here first", and `contracts/` is frozen to agents, so **the K1/K2 change is proposed below, not
    written**. Two parts of W1 need no contract change and are **built**: the parse budget (W1.3) and
    OTF/CFF coverage (W1.7).

    ---

### Part A — K1 / K2 contract change: **O1 approved, waiting for a second owner**

    **The change.**

    *K1* — `base_family` may name a bundled family **or** an imported one:

    ```
    base_family: str    # a member of SHORTLIST, or an imported family recorded in the store
    ```

    `SHORTLIST` stays exactly what it is: the **bundled** set. It does not grow. Validation keeps
    refusing unknown names — it just gains a second place to look, so `UNKNOWN_FONT` still means "this
    family is neither bundled nor imported" rather than "not one of our four".

    *K2 rule 2* — "Base fonts are BUNDLED in the repo, never taken from the system" becomes:

    > Base fonts are **bundled or imported**. An imported font is **copied** into
    > `data_dir()/user-fonts/<sha256>/`; it is never read in place from the system.
    > `base_font_files(family)` resolves both.

    The "never from the system" half is the part that must not weaken. Copying is what makes a build
    reproducible (the content key hashes the source bytes) and what stops a font disappearing or
    changing underneath an applied configuration.

    **Why `validate()` cannot do the lookup itself.** M1.1 is pure — "no I/O, no clock, no font
    access" — and an imported family lives in the store. Three options, with O1's recommendation:

    | | Approach | Assessment |
    |---|---|---|
    | 1 | `settings.validate()` takes an optional `known_families` argument | **Recommended.** Keeps M1.1 pure: the caller supplies the set, M1.1 only compares. Default `None` means `SHORTLIST`, so every existing call is unchanged |
    | 2 | A module-level registry M1.1 consults | Rejected — gives M1.1 hidden state and makes `validate()` depend on call order |
    | 3 | Validation of imported families moves to K2 `resolve()` | Rejected — splits one rule across two modules; `UNKNOWN_FONT` would fire from two places with different meanings |

    **New error codes (add-only to K8, need approval):** `IMPORT_FAILED`, `IMPORT_NOT_ELIGIBLE`,
    `IMPORTED_FONT_IN_USE`. Note `IMPORT_NOT_ELIGIBLE` is distinct from M1.3's existing refusals on
    purpose: the user needs "this font cannot be used, because it is monospaced", and the detail
    carries which of M1.3's codes it was.

    **Tasks for the second owner (any of O2, O3, O4 — plus the K1/K2 owner, which is O1):**

    1. **Agree or reject the K1 wording**, in particular that `SHORTLIST` keeps meaning "bundled" and
       does not become "allowed".
    2. **Agree or reject the K2 rule 2 wording**, in particular that "copied, never read in place" is
       the invariant that survives.
    3. **Pick the lookup approach** from the table above (O1 recommends 1).
    4. **Approve the three error codes**, or name different ones.
    5. A human applies the result to `contracts/config.py` and `contracts/presets.py`; agents may not.
    6. **O3 additionally:** K9's `user_fonts` table (W1.2) and whether `data_dir()/user-fonts/` needs a
       K8 accessor — the same question as this file's earlier `SCRIBESENSE_BUNDLED_FONTS` entry, and
       worth settling together.

    **Blocked until this is agreed:** W1.4 (the display name for an imported family that collides with
    a bundled one), W1.5 (O2's import UI), W1.6 (O4's reference counting, O3's uninstall), and the
    `base_font_files` resolution half of W1.2.

    ---

### Part B — W1.3's parse budget: **built**

    `MAX_FONT_BYTES` is a size cap, and its own docstring said what that does not cover: *"A small but
    pathological file -- a decompression bomb, or a table whose declared counts force quadratic work --
    can still consume unbounded time inside fontTools."* Research2 D.2/D.6 require a real budget on
    security grounds; W1.3 requires it specifically for imported files, because the font ScribeSense
    generates from one is later loaded by **every application's font engine on the machine**.

    A wall-clock budget needs something that can be killed, which a function call is not. So:

    - **New `src/scribesense/config/fontprobe.py`** — `python -P -m scribesense.config.fontprobe <path>`
      prints the verdict as one JSON line. Follows M4.13's `adapters/pango_probe` pattern exactly: one
      line on stdout, **never a traceback** (it could echo the path, and §4 keeps paths out of logs),
      any failure is a non-zero exit.
    - **`rules.is_eligible(path, *, isolate=False, timeout=PARSE_TIMEOUT_S)`** — keyword-only with a
      default, so the card's one-argument call is unchanged. The existing body is now
      `rules.verdict()`, which both the in-process path and the child call: **one implementation, two
      places to run it.**
    - **`rules.FONT_PARSE_TIMEOUT`** — Research2's `E_PARSE_TIMEOUT`, reachable for real. The module
      previously mapped that condition onto `FONT_TOO_LARGE` and said so in its code table; the table
      now distinguishes them. `variants.error_code_for` folds the new code to `FONT_UNREADABLE` for K7.

    **Isolation is off by default, deliberately.** Measured: 15 ms in process, 383 ms isolated, for one
    bundled face. M1.5 walks `is_eligible` once per style per build, so defaulting it on would add
    roughly 2.5 s to an eight-style build for no gain — the bundled families are pinned by SHA-256 in
    `fonts/manifest.toml`. **W1's import path is the intended caller**, which is exactly what W1.3 asks
    for ("parse *imported* files in a separate process").

    Failure mapping, with a crashed child always a refusal and never an exception: deadline passed →
    `FONT_PARSE_TIMEOUT`; child died, could not start, or printed something unreadable →
    `FONT_UNREADABLE`, the same answer an unparseable font gets in process. The size cap is applied
    **before** anything is spawned, so refusing a 2 GB file still costs one `stat`.

    **One limitation, for W1's UI (O2).** The probe assumes `sys.executable` can import `scribesense` —
    the same assumption M4.13 documents, true for an installed package. When it cannot, the result is
    `FONT_UNREADABLE`: fail-closed and correct, but **indistinguishable from a font that will not
    parse**. W1's import screen should say "could not check this font" rather than "not eligible" when
    the probe itself is unavailable. Noted in `rules._isolated_verdict`.

    ---

### Part C — W1.7's OTF/CFF coverage: **built**

    W1.7 lists "OTF/CFF and variable sources" among the tests W1 needs. Correcting this file's own
    2026-11-14 entry once more: CFF patching *is* covered by a synthetic fixture in
    `tests/unit/fontgen/test_fontgen.py`, but that fixture asserts three characters, and **no bundled
    family is CFF**, so the format gets no coverage from the shortlist at all.

    Added: a fuller CFF/OTF font built with `FontBuilder` (full ASCII, `é`, a Greek alpha so the
    non-Latin invariant has something to protect, varying advances so `is_monospace` does not refuse
    it), run through the M1.4 acceptance criteria — every Latin advance, the space factor, the
    non-Latin invariant, the rename — plus eligibility in and out of process, the `.otf` suffix
    surviving `variant_filename`, and Research2 I.4's zero-spacing neutrality.

    ---

### Code changes

    - **New `src/scribesense/config/fontprobe.py`** (~95 lines).
    - **`src/scribesense/config/rules.py`** — `is_eligible` gains `isolate` / `timeout`; the body is
      extracted to the exported `verdict()`; `FONT_PARSE_TIMEOUT` and `PARSE_TIMEOUT_S` added; the
      `MAX_FONT_BYTES` docstring no longer describes the gap as deferred, because it is not.
    - **`src/scribesense/fontgen/variants.py`** — folds `FONT_PARSE_TIMEOUT` into `FONT_UNREADABLE`.
    - **New `src/scribesense/config_and_fontgen_tests/test_isolated_parse.py`** — 23 tests: the verdict
      is identical in and out of process on all 14 bundled faces; a deliberate refusal survives
      isolation as a *verdict*, not a failure; the deadline kills and refuses; three corrupt-input
      shapes are refusals not exceptions; the size cap runs before any spawn; the probe's protocol
      (one JSON line, empty stderr, bad-argument handling, version mismatch); and the CFF suite.
    - **New-package total: 115 passed, 2 skipped.** Import rules clean; `config/` still imports nothing
      from ui, store, adapters, safety, controller, reader or `gi`.

    **One thing caught while writing the tests, worth keeping:** the first draft imported `subprocess`
    directly to inspect the probe's raw output. That violates import rule R2 ("only
    `contracts/paths.py` imports subprocess — every command goes through `run_cmd`, K3 rule 9") and the
    real checker would have failed on it once `testpaths` includes this package. Rewritten to use
    `run_cmd`, which is also closer to what the production path does. A test is not exempt from the
    rule whose observance it is checking.

### Still open for O1 after this

    - Part A's contract change (above), and W1.4/W1.5/W1.6 behind it.
    - **W6.5**, unchanged and still the most consequential: Default's `target_em` rounds to the nearest
      0.25 step and lands below the WCAG floor on three of four families.
    - Reviews O1 owes: **M2.5** (O2) and **M4.2** (O4).
    - Second-owner sign-offs O1 could give: K3 rule 10b, K8 socket, K4 rule 5 `subscribe_style`,
      `list_all`, and now the `PreviewFonts.styles` question from the 2026-11-14 K7 entry.

    ---

## W6.5 WCAG 1.4.12 — open question settled, defect fixed, check automated (2026-11-14, O1)

    Task 5 of O1's remaining-work list. W6.5 asked for an automated SC 1.4.12 check of the presets
    and named one question to settle first. Both are done, and the question turned out to have a
    defect behind it.

### Decision 1 — word spacing is measured as **space-glyph growth only**

    W6.5's question: *"space-glyph growth only, or the whole gap as CSS letter- + word-spacing would
    produce"*. **Space-glyph growth only.** Measured on Noto Sans at the Default preset:

    ```
    'catdog'  grew by 0.720 em = 6 x L            (six letters, no space)
    'cat dog' grew by 0.850 em = 6 x L + W        (six letters, one space)
    ```

    The width attributable to the **word break** is exactly `W`, the space glyph's growth. The letter
    delta appears once per letter whether or not a space follows, so it is letter spacing. SC 1.4.12
    requires "all of the following" to be set **together**; charging one adjustment to two criteria
    would mean a user who sets both gets one.

    **And the generous reading is not even generous in the right direction.** In CSS, `letter-spacing`
    applies after *every* character including the space, so letter 0.12 + word 0.16 gives a gap of
    `space + 0.16 + 2 x 0.12 = 0.660 em`. ScribeSense gives `space + W + 1 x 0.12 = 0.510 em`, because
    the space glyph is Unicode script Common and `latin_glyphs` excludes it, so it receives no
    tracking — **one L short of the CSS the criterion is written against**. Counting L toward the word
    floor would have hidden that rather than made up for it. (The shortfall is a separate standing
    finding: `docs/research2-conformance-O1.md` §9.4.)

### Decision 2 — `target_em` rounds **up**, and that fixes a real defect

    A `target_em` means "add **at least** this much". Rounding to the nearest 0.25 `word_scale` step
    delivered *less* than asked whenever the quotient fell in the lower half of a step — and the one
    place `target_em` is used is the WCAG floor. Before and after, space-glyph growth:

    | Family | space | before | after | floor 0.16 |
    |---|---|---|---|---|
    | Carlito | 0.226 em | 1.75 → **+0.170** | 1.75 → +0.170 | already cleared |
    | Noto Sans | 0.260 em | 1.50 → **+0.130** | **1.75 → +0.195** | fixed |
    | Atkinson Hyperlegible | 0.280 em | 1.50 → **+0.140** | **1.75 → +0.210** | fixed |
    | Lexend | 0.280 em | 1.50 → **+0.140** | **1.75 → +0.210** | fixed |

    So the Default preset — which documents itself as WCAG-anchored on all three properties — was
    **missing the word floor on three of the four bundled families**. It no longer does.

    Nothing else moved: `target_em` is used by exactly one preset field.

    **Content keys:** Default's key changes for Noto Sans, Atkinson and Lexend (Carlito's is
    unchanged, it was already 1.75). One rebuild, the same class of one-time change as bundling the
    real fonts.

### Conformance, stated precisely

    Two different claims, and the tests keep them apart:

    - **SC 1.4.12 conformance is about reachability** — "no loss of content or functionality occurs
      by **setting** all of the following". That is a property of K1's ranges, and it **holds on every
      bundled family**: letter reaches 0.30, line reaches 2.0, and `word_scale` 3.0 reaches +0.45 to
      +0.56 em depending on the family. This is what ScribeSense can honestly claim.
    - **Preset conformance** is what W6.5 actually asked to automate, and is a different question:
      does a preset land on the floors it claims?

    | Preset | letter ≥ 0.12 | word ≥ 0.16 | line ≥ 1.5 |
    |---|---|---|---|
    | Default | yes | **yes** (was no on 3 of 4) | yes |
    | Reading | yes | **no**, on all four | yes |
    | High Separation | yes | yes | yes |

### Finding — Reading sits below the word floor, and now below Default

    **For the team, not for O1 alone.** Reading's word spacing is K2's `multiplier` 1.5 — a relative
    intent, not a target — so Decision 2 does not touch it. It adds 0.113–0.140 em on the bundled
    families, below the 0.16 em floor.

    That is not a broken promise: K2's table gives Reading "0.12 em / 1.5× / 1.8×" and claims no WCAG
    anchor on the word field. But the consequence is odd enough to raise: **Default now provides more
    word spacing than Reading on every family**, which is not what the names suggest. Research2's own
    Reading preset used +0.16 em.

    Changing it means changing K2's frozen built-in table — the team's call, not O1's, and the M1.2
    card's acceptance test is "values match the K2 table". Pinned as current behaviour in
    `test_reading_meets_the_letter_and_line_floors_but_not_the_word_floor`, so a decision to change it
    shows up there. **Options, if the team wants Reading at the floor:** `multiplier` 1.75, or
    `target_em` 0.16 like Default (which would then track the font, and is what Research2 specified).

### O1 approved — one wording clarification needs a second owner

    K2's `resolve()` comment reads "rounded to the 0.25 step" without saying **which way**. O1 has
    read it as *up* for `target_em`, on the grounds above, and W6.5 explicitly delegated the call
    ("...and, if needed, round target_em up instead of to the nearest step"). The behaviour change is
    real, so the contract text should say so.

    **Task for a second owner (O2, O3 or O4):** confirm the reading, then a human edits
    `contracts/presets.py` so `resolve()`'s comment reads "rounded **up** to the 0.25 step" — agents
    may not edit `contracts/`. Nothing else in K2 changes; `multiplier` values are untouched, and
    `round_to_steps` still rounds to nearest for every field a user sets directly.

### Code changes

    - **`config/settings.py`** — new `snap_up(range, value)` beside `snap()`: ceiling onto the step
      lattice, `STEP_TOLERANCE`-guarded so float noise cannot push an exact value a whole step up, and
      **not clamping** (K1 rule 1 — an out-of-range target stays out of range and `validate` refuses
      it, rather than being quietly reduced to the maximum and reported as granted). Exported.
    - **`config/presets.py`** — `_word_scale_from_target_em` returns `snap_up(RANGES["word_scale"], …)`.
      The step still lives in one place: `snap_up` takes the `Range`, and `resolve`'s `round_to_steps`
      is then a no-op on an already-on-grid value.
    - **New `src/scribesense/config_and_fontgen_tests/test_wcag_text_spacing.py`** — 37 tests: the
      attribution measurement behind Decision 1; the CSS comparison showing ScribeSense is one L
      short; reachability per family; that each floor sits on a step or is reachable by `snap_up`;
      per-preset per-family conformance; `snap_up` vs `snap` on the motivating case; `snap_up`
      idempotence on grid positions; non-clamping; and that every built-in still resolves and
      validates.
    - **Updated:** `tests/unit/fontgen/test_config.py` and `tests/unit/fontgen/test_bundled_fonts.py`
      each asserted `word_scale == 1.5` for Default — they pinned the defect, and now pin the fix with
      the reason. The superseded per-family test in `test_bundled_font_review.py` was removed; its
      subject moved to the new file, which owns the decision that changed it.
    - New-package total: **148 passed, 2 skipped**. Import rules clean; the application still imports.

### What this check does not claim

    Passing is conformance with an **accessibility requirement**, not evidence of reading benefit.
    Research2 is explicit that WCAG numbers are "accessibility requirements about reachability, not
    proofs of benefit", and that letter-spacing effects are positive, null and negative depending on
    population and task. A preset meeting these floors is a preset that meets these floors — the
    reading-outcome question is W6's user study, not this test.

### Remaining for O1 after this

    - The W1 contract change (previous entry) and W1.4–W1.6 behind it.
    - The `PreviewFonts.styles` K7 question, and the K2 wording confirmation above.
    - Reviews O1 owes: **M2.5** (O2) and **M4.2** (O4).
    - Second-owner sign-offs O1 could give: K3 rule 10b, K8 socket, K4 rule 5 `subscribe_style`,
      `list_all`.
    - Standing findings not yet acted on: `ui_line` over-keying (§9.3), digits and punctuation
      untracked (§9.4), the per-face `ITALIC_ONLY` warning (§9.6), the kerning count-not-diff (§9.8),
      and the unwritten font-registry row (W7 entry above).

## Review of O1's 2026-11-14 work and fixes (2026-11-22, O4 for O1, user's go-ahead)

    **Reviewed and good:** the bundled-fonts search (`config/bundled.py`: override → installed data dirs → repo; the
    packaging blocker W2.1), `settings.snap_up` so `target_em` rounds UP (W6.5: Noto's Default +0.130 → +0.195 em; all
    four families now meet 0.16 em), the isolated font parse (`rules.is_eligible(isolate=True)` + `config/fontprobe.py`,
    W1.3), the removal of the unused `to_record` / `from_record` (W7), CFF/OTF support and the corrected docstrings. No
    contract or other owner's file was touched. Merge with O4: only `PLAN_v2.md` conflicts; the merged code passed every
    suite (checked on an exported copy).

    **Fixed here (cross-owner, recorded on the M1.2 card):**
    1. O1's 148 tests moved from `src/scribesense/config_and_fontgen_tests/` (kept beside the code at O1's request; costs
       she documented herself: shipped in the package, not collected by pytest, so not in CI) to
       `tests/unit/config_and_fontgen/`. Under `src/` they also ran WITHOUT the M3.3 sandbox, against the real HOME (no
       damage found). The O3 task "add the directory to testpaths" is therefore no longer needed.
    2. `test_isolated_parse.py`: `SRC_ROOT` follows the new depth; the module is marked `real_subprocess`.
    3. Temp files: `test_wcag_text_spacing.py` / `test_integration_review.py` route `tempfile` into `tmp_path`, and a
       leaked file handle is closed.
    4. `SCRIBESENSE_BUNDLED_FONTS` is pinned to the repo's `fonts/` in `tests/conftest.py` and `harness/sandbox.py`.

    Tests on the O1 branch after the fixes: **2338 passed, 0 failed** (unit, now including O1's 148) · integration
    **35 passed, 0 failed**.

    **Still open:**
    - **O1:** commit `docs/research2-conformance-O1.md` — cited by the code (findings §9.1–§9.4) and five times in
      PLAN_v2, but not in the repository.
    - **O1 + O3:** agree the name and search order of `SCRIBESENSE_BUNDLED_FONTS` (O1 marked it pending a K8 decision);
      bridge `FontRegistry` and `store.repos.FontRegistryRepo` (O1 documented the mismatch).
    - **O3:** `.gitignore` ignores `*.crx` and `dist/`, so the signed extension that automatic setup looks for
      (`extension/dist/scribesense.crx`) would be silently left out of git — add an exception
      (`!extension/dist/scribesense.crx`) or choose another release location.
    - The Default preset now builds a different font with Noto Sans (word_scale 1.75 instead of 1.5): expected; users
      get it at their next Apply.

## Decision (2026-11-22, O3) — W2 font-location fix, all four sub-points closed

    O3 (K8 owner) signs off O1's `config/bundled.py` fix (2026-11-14 entry above) on all four points W2.1 left open:

    1. **Placement: stays in `config/bundled.py`, not moved into `contracts/paths.py`.** `fonts_root()` /
       `candidate_roots()` are not part of K8 — no contract change, so nothing for a human to apply to
       `contracts/paths.py`.
    2. **`SCRIBESENSE_BUNDLED_FONTS` approved as a `config/bundled.py`-local env var.** Because it is not a K8
       function (point 1), it does **not** join K8's documented override list (`HOME`, `XDG_CONFIG_HOME`,
       `SCRIBESENSE_DATA_DIR`, `SCRIBESENSE_FONTS_DIR`) — that list stays unchanged.
    3. **Search order confirmed as implemented:** override → `$XDG_DATA_DIRS` (installed) → repo tree (dev
       fallback). Installed wins over a checkout when both exist on a dev machine; moot for the test suite
       itself, since `tests/conftest.py` and `harness/sandbox.py` already pin the override to the repo
       (2026-11-22 O4 fix, same entry above).
    4. **Packaging path confirmed: `/usr/share/scribesense/fonts`.** Already on the code's own `$XDG_DATA_DIRS`
       search (default `/usr/local/share:/usr/share`) — no new behaviour needed, just committing to it for the
       RPM `.spec` (W2.2/W2.3), alongside each family's `OFL.txt` under `/usr/share/licenses/scribesense/`.

    **Closes:** W2.1's O3 tasks 1–3 and the packaging-path half of task 5. **Still open from that task list:**
    task 5's other half (`extension/` files for the native-messaging host installer — O4's module, same bug
    shape) and task 6 (moot — O4's 2026-11-22 fix already relocated O1's tests out of the shipped package).
    The duplicate ask in the 2026-11-22 entry above ("O1 + O3: agree the name and search order of
    `SCRIBESENSE_BUNDLED_FONTS`") is answered by points 2–3.

