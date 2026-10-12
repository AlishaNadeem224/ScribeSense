# AGENTS.md — rules for coding agents on ScribeSense

Private to the team (git-ignored). Every coding agent working in this repo must follow these rules.
If a rule here conflicts with a request, **stop and ask the human** — do not work around it.

## 1. What this project is

ScribeSense is a **system-wide reading tool for Linux (Hyprland, English only)**. The user picks a
preset (letter / word / line spacing, font, text size). ScribeSense generates spaced fonts and applies
them across the desktop through per-app adapters. Every change is journaled and reversible.
Apps that can't be changed use a fallback reader.

## 2. Source of truth

- **`ARCHITECTURE.md`** is the specification: contracts K1–K11 (§5), decisions A1–A20 and R2 (§3),
  conventions (§4), seams (§6), module cards (§7), dependency order (§9).
- `PLAN_v2.md` is the history log. If the two disagree, `ARCHITECTURE.md` wins.
- Never invent behaviour that the module card does not specify. If the card is silent or unclear,
  **ask the module owner**.

## 3. Ownership — only work inside your owner's area

| Owner | Packages | Cards |
|---|---|---|
| O1 Configuration & Fonts | `config/`, `fontgen/`, `fonts/` | M1.1–M1.8 |
| O2 User Experience | `controller/`, `ui/`, `reader/`, `cli.py` | M2.1–M2.11 |
| O3 Data & Platform | `store/`, `doctor.py`, `tests/conftest.py`, `harness/`, `vm/`, `docs/`, `pyproject.toml` | M3.1–M3.9 |
| O4 System Integration & Safety | `adapters/`, `safety/`, `extension/`, `log.py` | M4.1–M4.15 |
| Shared, frozen | `src/scribesense/contracts/` | K1–K11 |

Do not edit another owner's package. Helper work on adapters (§8 of the architecture) only with an
assigned card.

## 4. Contracts are frozen

- **Never edit `src/scribesense/contracts/`.** A change needs the contract owner + one other owner
  (K3/K4: O4 + one other) and a decision entry in `PLAN_v2.md`.
- Code against the contracts exactly: same names, same types, same error codes.
- If a contract looks wrong or incomplete, stop and report it. Do not patch around it locally.
- K9 (store) is a **draft** until O3 signs off — do not build the store, journal or revert against it before then.

## 5. Safety rules — never touch the real desktop

- **Never** run code that writes to the real system: no `gsettings set/reset`, no writes to
  `~/.config/fontconfig`, `~/.config/qt6ct`, browser profiles, `flatpak override`, `hyprctl`,
  `~/.config/hypr/`, or `~/.local/share/fonts`.
- All tests run in the sandbox (M3.3): unit tests use **fake adapters only** with `subprocess` blocked;
  integration tests run inside bubblewrap; full-system tests run only in the VM.
- Paths come **only** from K8 (`data_dir()`, `fonts_dir()`, `config_home()`, `lock_path()`, `socket_path()` …).
  Never hard-code `~` or `/home/...`.
- External commands go **only** through `run_cmd()` (K8) with a timeout.
- Never close, kill or restart the user's applications.

## 6. Privacy rules

- Never write reader/document text, window titles or user file paths to disk, logs or the store.
- Logs contain error **codes** and categories only (K8, M4.15). Log only through
  `from scribesense.log import log`; strings only in identifier fields (see the M4.15 card).
- No network access of any kind (no CDN, no remote fonts, no telemetry).

## 7. Code rules

- **Python ≥ 3.11 syntax only** in core modules (no `type X = …` statements, no PEP 695 generics,
  no 3.12+ f-string nesting). GUI code runs on the system Python (3.14) but follow the same rule.
- Errors: raise `ScribeSenseError(ErrorCode.X)` (K8). New codes are add-only and need approval.
- Import rules (enforced by tests):
  - `safety/recovery.py`, `cli.py` recovery commands, `doctor.py` → **never** import `Gtk` or `Adw` (A2).
  - Only `store/` touches SQLite.
  - Adapters never call the store or journal and never show UI.
  - `config/` and `contracts/` import nothing from UI, store or adapters.
- Every module card's **acceptance tests** must exist and pass before the card is "done".

## 8. Environment

```bash
uv venv --python /usr/bin/python3 --system-site-packages   # PyGObject comes from the system
uv sync                                                     # fontTools + pytest
uv run pytest                                               # GUI tests excluded by default
```

## 9. Git

- **Agents never commit, push, create branches, or open issues/PRs.** Humans do all git operations.
- Leave changes in the working tree for the human to review.
