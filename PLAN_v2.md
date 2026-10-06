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
