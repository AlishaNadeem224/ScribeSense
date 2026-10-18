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
- **K9 D3:** confirm `custom` is stored as one row with the reserved id `custom` (the code in `config/presets.py` already matches).
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

