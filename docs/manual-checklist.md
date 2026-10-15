# ScribeSense manual checklist (M3.6)

Owner: O3 · Reviewer: O2 · Source: ARCHITECTURE.md (A1, A3, A5, A14, A15, A17, A18; cards M2.3, M2.4, M2.5, M2.9, M4.12, M4.14)

**Run this only in the VM (M3.5), from a clean snapshot — never on a development or personal desktop.**
Applying a preset changes real settings, so a wrong result on your own machine is not recoverable by the checklist.
Reset the VM to the snapshot between sections marked **[fresh]**.

**Release rule:** any item in sections A–C that fails blocks the release. Sections D–G failures block the release
unless a written decision in PLAN_v2.md accepts the gap.

## Run record

| Field | Value |
|---|---|
| Date | |
| Tester | |
| Commit (`git rev-parse --short HEAD`) | |
| VM snapshot name | |
| `scribesense doctor` output saved? | |
| Screen reader used (name + version) | |

Mark each item **P** (pass), **F** (fail, add a note) or **N/A** (with the reason). Do not leave blanks.

## A. Accessibility — release blocking (M2.4, M2.5, M2.9; F6)

| # | Check | Result | Note |
|---|---|---|---|
| A1 | Every window (main, preview, confirm, reader) can be fully operated with the keyboard only: Tab / Shift+Tab reach every control, Enter/Space activate, Escape closes dialogs. | | |
| A2 | Keyboard focus is always visible, and focus order follows reading order. | | |
| A3 | With a screen reader running, every control has a spoken label (buttons, dropdowns, sliders, the Keep/Revert buttons). Record anything announced as "unlabelled" or by a raw widget name. | | |
| A4 | The screen reader announces the countdown or its state change in the confirm window, so a non-sighted user can decide before the 30 s deadline. | | |
| A5 | Text size set to large (system scale ≥ 150 % or large-text setting): no text is cut off, overlaps, or needs horizontal scrolling in any window. | | |
| A6 | Nothing relies on colour alone (status, errors, selected preset). | | |
| A7 | Options are described as choices, not clinical recommendations (A5). | | |

## B. Confirm flow and recovery — release blocking (A3, A15; M2.3, M4.12) **[fresh]**

| # | Check | Result | Note |
|---|---|---|---|
| B1 | Apply a preset → a separate confirm window opens showing text in the new font ("Can you read this? Keep (30)…"). | | |
| B2 | Press **Keep** → the window closes, the preset stays after relaunching an app, `scribesense` shows it as active. | | |
| B3 | Apply again, press **Revert** → everything returns to the previous state. | | |
| B4 | Apply again, do nothing for 30 s → automatic revert. Inactivity before the countdown starts does not trigger it. | | |
| B5 | Apply, then kill the confirm window (`kill` its PID) → revert happens without further action. | | |
| B6 | Apply, then kill the **service** while the confirm window is open → at next login, `recover --login` reverts the unconfirmed change (log out/in or reboot the VM). | | |
| B7 | Apply, reboot the VM at the confirmation stage → after login everything is reverted. | | |
| B8 | A confirmed configuration is **not** reverted by login recovery after a reboot. | | |
| B9 | Press Keep after the rollback already happened (late confirmation) → rejected, state unchanged. | | |
| B10 | Launching `scribesense` a second time focuses the existing window instead of starting a second instance (A1). | | |

## C. Reset and keybinds — release blocking (A18; M4.12, M4.14) **[fresh]**

| # | Check | Result | Note |
|---|---|---|---|
| C1 | Press **Ctrl+Alt+Shift+Super+Backspace** after a kept preset → everything reverts; the report lists restored / skipped / failed counts. | | |
| C2 | Same keybind while the app window and service are **not running** → still reverts (A2: no GUI needed). | | |
| C3 | Same keybind while the confirm window is open → reverts and the confirm window closes. | | |
| C4 | Edit a managed setting by hand after Apply, then reset → that item is **skipped and reported**, not overwritten. | | |
| C5 | **Super+Alt+P** cycles to the next preset (confirm flow starts as in B1). | | |
| C6 | **Super+Alt+R** with text selected opens the reader with that text. | | |
| C7 | The ScribeSense keybind file is separate and included from the Hyprland config; unrelated lines in the user's config are unchanged (`diff` against the pre-install copy). | | |
| C8 | A deliberately conflicting existing bind is reported, not silently replaced (`KEYBIND_CONFLICT`). | | |
| C9 | After reset, `hyprctl reload` succeeds and the keybinds still work. | | |

## D. Apps and coverage (A14; M2.6, M4.x) **[fresh]**

A passing configuration check is not proof that an app draws the font (A14). Look at the screen.

| # | Check | Result | Note |
|---|---|---|---|
| D1 | A GTK app (e.g. a file manager) visibly uses the new UI font after relaunch. | | |
| D2 | A Qt app (if `qt6ct` installed) visibly uses it; if not installed, the coverage report says Qt is not covered (not "OK"). | | |
| D3 | A Chromium-family browser shows the new font on a plain web page after the extension is active; code blocks and icon fonts are unchanged. | | |
| D4 | Firefox/Zen: the new font appears **after a browser restart**, and the restart notice was shown. | | |
| D5 | A Flatpak app (if available) visibly changes; one that cannot be changed appears as "not covered" in the coverage report. | | |
| D6 | The coverage report matches what you saw in D1–D5 (no app marked covered that did not change). | | |
| D7 | Running browsers were never closed, killed or restarted by ScribeSense. | | |

## E. Preview and reader (A5, A17; M2.5, M2.9, M2.10)

| # | Check | Result | Note |
|---|---|---|---|
| E1 | Preview shows the built-in sample passage in the chosen font before Apply; changing spacing updates it. | | |
| E2 | Text typed into the preview is not saved anywhere after the app is closed. | | |
| E3 | Reader opens with clipboard text (`read --clipboard`). | | |
| E4 | `read --selection` with **nothing selected** → "nothing selected"; it never reads a whole document. | | |
| E5 | Text over 200 000 characters is rejected with an explicit message, not truncated. | | |

## F. Install, doctor, uninstall (M3.7, M3.8) **[fresh]**

| # | Check | Result | Note |
|---|---|---|---|
| F1 | `scribesense doctor` on a VM missing `pango`, `qt6ct`, `flatpak`: required items marked MISSING with the dnf command; optional items marked OPTIONAL; exit code 1 only when a required item is missing. | | |
| F2 | `doctor` installs nothing (check `rpm -qa --last` before/after). | | |
| F3 | Install steps from the README work from a clean checkout. | | |
| F4 | Install → Apply → Uninstall: every touched file is byte-identical to its pre-install copy (except items reported as skipped). | | |
| F5 | Uninstall keeps fonts still referenced by skipped/unresolved settings, and prints "these apps may fall back to another font and their appearance may change". `--remove-fonts-anyway` removes them. | | |
| F6 | Uninstall without `--delete-data` keeps the data dir; with it, asks for explicit confirmation first. | | |
| F7 | After uninstall: no keybind file or include line, no login-check `exec-once` line, no generated fonts (except F5 cases). | | |

## G. Privacy (AGENTS.md §6)

| # | Check | Result | Note |
|---|---|---|---|
| G1 | After using the reader with a distinctive phrase, `grep -r` for that phrase in the data dir, logs and `~/.cache` finds nothing. | | |
| G2 | Logs contain error codes and categories only — no window titles or file paths. | | |
| G3 | During a full session, no outbound connection is made by ScribeSense (VM network monitor or `ss -tp` shows none). | | |

## Sign-off

| Result | |
|---|---|
| Blocking failures (A–C) | |
| Other failures and decisions | |
| Reviewed by (O2) | |
