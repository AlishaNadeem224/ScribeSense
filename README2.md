# ScribeSense

**System-wide reading spacing for Linux: choose how text looks once, and every supported app follows.**

ScribeSense is a local, single-user desktop tool for **Linux (Hyprland on Wayland), English only**. You pick a
reading preset — font, letter spacing, word spacing, line height and text size — and ScribeSense applies it
across the desktop: GTK/GNOME apps, Qt apps, Firefox and Chromium-family browsers, and Flatpak apps.
Apps it cannot change (canvas apps such as Google Docs, PDF viewers) are covered by a pop-up reader.

> Status: **Phase II — implementation in progress.** The safety core and the first adapters exist and are
> tested; the full apply flow and user interface are not finished yet. See *Project status* below.

## Why

Spacing affects how comfortably people read, and the effect differs from person to person
(Zorzi et al., 2012; WCAG 2.1 SC 1.4.12). Linux lets you change the font and size, but **not letter or word
spacing**, and the settings that do exist are scattered across toolkits and apps. ScribeSense makes one
choice apply everywhere it can reach.

ScribeSense describes reading options as **choices you make**, not clinical recommendations. It does not
detect or diagnose dyslexia.

## How it works

1. **Choose** a preset (Default, Reading, High Separation, or your own) and preview it.
2. **Generate fonts:** ScribeSense builds renamed copies of open-licensed fonts (Noto Sans, Carlito,
   Atkinson Hyperlegible, Lexend) with the spacing built into the glyphs. Two variants per preset: a
   *reading* font, and a *UI* font with smaller line height so menus don't cut off text. Monospace and icon
   fonts are never changed; only Latin letters get extra spacing.
3. **Apply** through per-app adapters: fontconfig rules, GNOME settings, Qt (via qt6ct), Flatpak permissions,
   Firefox CSS, and a small Chromium extension.
4. **Confirm:** a fresh window asks "Can you read this?" — no answer within 30 seconds reverts everything.

### Safety by design

- **Every change is journaled before it is made** and can be undone. Recovery works without the GUI:
  `scribesense reset`, or the keybind **Ctrl + Alt + Shift + Super + Backspace**.
- **An unconfirmed change is reverted at the next login.**
- **Your own later changes are never overwritten**: if you changed a setting after ScribeSense did, reset
  skips it and tells you.
- **Uninstall** reverts everything ScribeSense changed.

### Privacy

- Fully local: no network, no account, no telemetry.
- No document text, window titles or file paths are stored or logged. Logs contain error codes only.
- Text opened in the pop-up reader stays in memory for the session only.

## Project status

| Area | State |
|---|---|
| Architecture, contracts (K1–K11), decisions | Done — see `ARCHITECTURE.md` (K9 store contract awaiting sign-off) |
| Test isolation, contract tests, CI (Python 3.11 + 3.14) | Done |
| Safety core: journal (in-memory for now), revert, recovery, cross-process lock, local IPC, privacy-safe logging | Done, tested |
| Adapters: fontconfig, GTK; font verification (fontconfig + Pango) | Done, unit-tested; real-desktop integration tests pending |
| Font generator, presets, UI, apply controller, reader, store | In progress |
| Qt, Flatpak, Firefox, Chromium adapters, browser extension, keybinds | Planned |
| Full-system tests in a VM | Planned |

Planned later (not in the first version): recommendations, text-to-speech, GNOME support.

## Development setup

Requires Fedora (or another Linux) with GTK 4, libadwaita and PyGObject installed from system packages,
and [uv](https://docs.astral.sh/uv/).

```bash
sudo dnf install uv python3-gobject gtk4 libadwaita
uv venv --python /usr/bin/python3 --system-site-packages   # PyGObject comes from the system
uv sync
uv run pytest                                               # unit tests; never touch your real desktop
```

Unit tests run inside an isolation layer (temporary home folder, real programs blocked). Tests that need
real desktop tools are marked `integration` and run only inside a sandbox runner.

## Documentation

- `ARCHITECTURE.md` — scope, decisions, the shared contracts and every module card (source of truth).
- `PLAN_v2.md` — decision history and the live implementation checklist.

## Team

| Name | Area |
|---|---|
| Alisha Nadeem (Team Lead) | <!-- O1 / O2 / O3 --> |
| Sukhmani Kaur Ahluwalia | <!-- O1 / O2 / O3 --> |
| Rayan Rahat Afzal | <!-- O1 / O2 / O3 --> |
| Shivaansh Gusain | O4 — System integration & safety (adapters, journal, recovery) |

Areas: **O1** configuration & fonts · **O2** user experience (UI, apply flow, reader, CLI) ·
**O3** data & platform (store, tests, packaging) · **O4** system integration & safety.

**Mentor:** (Prof.) Dr. Vikas Tripathi
**Team ID:** OSDBMS-V-2026-T003 · Department of Computer Science & Engineering, Graphic Era (Deemed to be University), Dehradun

## References

- Zorzi, M. et al. (2012). Extra-large letter spacing improves reading in dyslexia. *PNAS*.
- W3C. *Web Content Accessibility Guidelines (WCAG) 2.1*, Success Criterion 1.4.12 Text Spacing.
- British Dyslexia Association. *Dyslexia Style Guide*.
- Kuster, S. M. et al. (2018). Dyslexie font does not benefit reading in children with or without dyslexia.
  *Annals of Dyslexia*.
- Rello, L. and Baeza-Yates, R. (2013). Good fonts for dyslexia. *ACM ASSETS*.
