# ScribeSense

**System-wide reading support for dyslexic users on Linux.**

ScribeSense lets a reader choose how they need text spaced — letter, word, and line
spacing, plus text size — and applies that choice across the desktop: browsers, GTK,
Qt, Flatpak applications, and anything else that asks the system for a font. Where an
application cannot be reached, a pop-up reader shows selected or copied text in the
same spacing.

The spacing is carried by **generated fonts**: ScribeSense builds modified copies of
open-licensed fonts with the spacing built into the glyphs and the font metrics, then
points the system and each reachable application at them. Word spacing — which Linux
does not expose as a setting — is delivered this way.

Every change is journaled, verified, and reversible. A 30-second keep-or-revert prompt
follows each Apply, an automatic revert runs at the next login if a change was never
confirmed, and a recovery command works without the GUI.

> **This is a B.Tech PBL project. It is in active development. It is not a released product.**

---

## Status

| Item | State |
|---|---|
| Contracts | K1–K8, K10, K11 frozen 2026-10-09 · K9 (store) drafted, pending O3 sign-off |
| Architecture decisions | A1–A20 closed 2026-10-08 |
| Foundation test suite | 58 passed, 8 skipped, 0 failed (`pytest`, isolated) |
| Skips | 5 recovery scenarios (waiting for journal) · 2 browser-extension tests · 1 bubblewrap integration test |
| Formal validation | Pending |

For detail see `ARCHITECTURE.md` (build specification), and `PLAN_v2.md` (decision log).

---

## The core mechanism

Linux lets you change the font and the text size. It does not let you change letter
spacing, word spacing, or line height system-wide. ScribeSense works around that by
building the spacing into the font itself:

- **Letter spacing** — the advance width of each Latin glyph is widened.
- **Word spacing** — the space glyph is widened.
- **Line height** — the font's vertical metrics are raised.

Two font variants are built per configuration:

| Variant | Line height | Used for |
|---|---|---|
| **Reading** | full (user's value) | documents, web page content |
| **UI** | capped, recommended 1.2–1.3× | menus, buttons, window chrome |

The UI variant exists because tall line metrics truncated text in Qt menus during the
team's feasibility testing. A single configuration therefore installs two font families:
`AccessSans-<key>` and `AccessSansUI-<key>`.

**Never modified:** monospace fonts, icon fonts, and non-Latin glyphs. Terminals and
code editors keep their alignment; icons stay intact; only Latin text gets the spacing.

Text size is handled separately through the desktop's `text-scaling-factor`, not by the
font.

---

## What it reaches

Each application family reads its font from a different place, so each needs a small
"adapter". Statuses below come from feasibility tests on the development machine
(Fedora, Hyprland, GTK 4.22). They are per-target results, not a blanket promise.

| Target | How | Tested |
|---|---|---|
| fontconfig (everything that asks the system) | `conf.d` rule | works |
| GTK | gsettings font keys + `text-scaling-factor` | works |
| LibreOffice | via fontconfig | works |
| Flatpak apps | per-app `flatpak override --user` | works |
| Qt (via qt6ct) | `qt6ct.conf` font by name | main windows work · menus need the UI variant |
| Firefox / Zen | `userContent.css` + `user.js` per profile | works |
| Brave / Chrome (defaults) | `Preferences` `webkit.webprefs.fonts.*.Zyyy` | works |
| Brave / Chrome (web pages) | own Manifest V3 extension | **extension not yet built** |
| Google Docs, PDFs (canvas) | fallback pop-up reader | by design |
| Terminals, code editors | excluded on purpose | — |

If a change cannot be applied or verified, that application is reported as
**not covered** and its text can still be opened in the fallback reader.

---

## Safety

ScribeSense edits other programs' configuration. A bad change could leave a reader
with an unreadable desktop, so the safety rules are strict:

- Nothing is written before its current value is journaled.
- Every Apply is a transaction; the transaction state is persisted in SQLite (WAL,
  `synchronous=FULL`) before any target is modified.
- A 30-second keep-or-revert prompt runs in a **fresh process** launched by the
  background service; the service is the watchdog until the transaction ends.
- A crash or reboot before confirmation is reverted at the next login.
- The recovery keybind `Ctrl+Alt+Shift+Super+Backspace` and the command
  `scribesense reset` work without the GUI.
- A value the user changed after ScribeSense wrote it is **skipped and reported**,
  never overwritten.
- Applications are never closed or restarted by ScribeSense; the user is asked.
- Text from the fallback reader stays in memory for the session only — never written
  to disk, never logged.
- No network access. No server, no CDN, no downloaded fonts.
- Uninstall reverts everything and keeps the user's presets unless they explicitly
  ask to delete them.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python ≥ 3.11 (GUI validated on the system Python, e.g. 3.14) |
| Tooling | `uv` |
| Fonts | `fontTools` |
| Desktop UI | GTK4 + libadwaita via PyGObject (system packages, not pip) |
| Storage | SQLite (stdlib `sqlite3`) |
| Tests | `pytest`, with an enforced isolation layer |
| Browser extension | Manifest V3, plain JavaScript, no libraries |

The extension is the one non-Python component. Per architecture decision A4, no
additional Python dependency is added without team approval.

---

## Repository layout

```
ScribeSense/
├── src/scribesense/
│   ├── config/            # O1 — settings, presets, font eligibility (K1, K2)
│   ├── fontgen/           # O1 — patcher, variants, validation, cache (K7)
│   ├── controller/        # O2 — apply controller, coverage (K6)
│   ├── ui/                # O2 — GTK4 window, presets, preview, confirm
│   ├── reader/            # O2 — capture, fallback reader
│   ├── cli/               # O2 — command dispatch (K10)
│   ├── store/             # O3 — SQLite schema, migrations, repositories (K9)
│   ├── harness/           # O3 — test sandbox, render checks
│   ├── adapters/          # O4 — fontconfig, gtk, flatpak, qt, browsers
│   └── safety/            # O4 — journal, revert, recovery (K4, K5)
├── fonts/                 # bundled open-licensed base fonts (O1)
├── tests/                 # unit · sandbox · render · VM
├── extension/             # Manifest V3 browser extension (O4)
├── ARCHITECTURE.md        # build specification (contracts, module cards)
├── PLAN_v2.md             # decision log
└── ScribeSense_Architecture.html   # visual overview
```

*(Directory names beyond `config/`, `fontgen/`, `controller/`, `ui/`, `reader/`,
`store/`, `harness/`, `adapters/`, `safety/` are indicative, matching the layout
recorded in `PLAN_v2.md` Step 5. Verify against the repo before relying on a specific
path.)*

---

## Running it

Requires Fedora Linux with Hyprland. PyGObject / GTK4 / libadwaita must already be
installed as system packages.

```bash
# Set up the environment (system Python so PyGObject is visible)
uv venv --python /usr/bin/python3 --system-site-packages
uv sync

# Run tests (unit + isolated)
uv run pytest

# Run the app (once the UI shell exists)
uv run scribesense

# Check dependencies
uv run scribesense doctor
```

Commands exposed by the CLI (per contract K10, once implemented):

| Command | Purpose |
|---|---|
| `scribesense` | open or focus the app |
| `scribesense apply <preset-id>` | apply a preset |
| `scribesense preset next` | switch to the next preset (keybind) |
| `scribesense read --selection` | open selection in the reader |
| `scribesense read --clipboard` | open clipboard in the reader |
| `scribesense reset` | revert everything (recovery) |
| `scribesense recover --login` | login-time recovery |
| `scribesense uninstall` | remove, reverting every change |
| `scribesense doctor` | report missing dependencies |
| `scribesense confirm <tx>` | started by the service; runs the confirm window |
| `scribesense quit` | stop the background service |

---

## Documentation

| Document | Role |
|---|---|
| `ARCHITECTURE.md` | The build specification. Contracts K1–K11, the 14-step apply sequence, 43 module cards, safety rules. Treat this as authoritative. |
| `PLAN_v2.md` | Decision log. Later entries override earlier ones. History, not current plan. |
| `ScribeSense_Architecture.html` | Visual overview for the mentor and for new readers. |

If the documents disagree, the latest explicitly confirmed team decision in
`ARCHITECTURE.md` wins.

---

## What is **not** in this project

To prevent confusion with earlier proposals:

- **No conversational layer.** No chat, no assistant.
- **No passive typing or interaction monitoring.** Nothing is captured without an
  explicit user action.
- **No dyslexia sub-type classification.** No ML model of any kind is used in the MVP.
- **No phonetic search correction.**
- **No voice / TTS mode.** Parked, not planned for the MVP.
- **No network component.** No accounts, no servers, no telemetry, no downloaded
  fonts.
- **No Flatpak distribution of ScribeSense itself.** A sandboxed app cannot write the
  host's fontconfig, gsettings, browser preferences, or Flatpak overrides — which is
  the entire product. ScribeSense installs natively.

These were considered in Phase-I and dropped in the Phase-II pivot. They are not
"scheduled for later"; the only items marked Later in the current plan are: shadow
mode, drift detection, Electron support, GNOME support, a recommender, and a voice
assistant — see `ScribeSense_Architecture.html` "After the MVP".

---

## Team

| Name | Role |
|---|---|
| Alisha Nadeem | Team Lead · O2 — User Experience |
| Sukhmani Kaur Ahluwalia | O1 — Configuration & Fonts |
| Rayan Rahat Afzal | O3 — Data & Platform |
| Shivaansh Gusain | O4 — System Integration & Safety |

**Mentor:** (Prof.) Dr. Vikas Tripathi

**Team ID:** OSDBMS-V-2026-T003 · Department of Computer Science & Engineering,
Graphic Era (Deemed to be University), Dehradun

**Repository:** github.com/AlishaNadeem224/ScrbeSense
oursework produced for the PBL programme. Bundled base fonts are
redistributed under their own open licences (SIL OFL or equivalent); each family's
licence file lives next to it under `fonts/`.
