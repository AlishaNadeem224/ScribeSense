# Installing and uninstalling ScribeSense (M3.8)

ScribeSense targets **Fedora with Hyprland, English only**. It is a B.Tech PBL project in development, not a released
product. This page says what works **today**; the last section lists what is not wired yet.

## 1. Install from a checkout

1. **System packages** (the commands `scribesense doctor` prints for anything missing):
   ```bash
   sudo dnf install python3-gobject gobject-introspection pango fontconfig glib2 gsettings-desktop-schemas
   # optional, each widens coverage:  qt6ct (Qt apps) · flatpak (Flatpak apps) · lua (Hyprland Lua configs) · hyprland
   ```
   PyGObject comes from the system packages, never from pip.
2. **Python environment** (needs Python ≥ 3.11; the GUI runs on the system Python):
   ```bash
   uv venv --python /usr/bin/python3 --system-site-packages
   uv sync
   ```
3. **Check the machine** — read-only, installs nothing:
   ```bash
   uv run scribesense doctor
   ```
   Exit code 0 means every required dependency is present; `OPTIONAL` lines only narrow coverage.
4. **Run it:** `uv run scribesense`.

Nothing is installed outside the project folder by these steps. ScribeSense writes to the desktop only when you press Apply.

## 2. What ScribeSense keeps on your machine

| What | Where (default) | Removed by |
|---|---|---|
| Records: history, presets, settings, backups of the files it changed | `~/.local/share/scribesense/scribesense.db` | `uninstall --delete-data` |
| Log (error codes only, never text) | `~/.local/share/scribesense/logs/` | `uninstall --delete-data` |
| Generated fonts, one folder per font set | `~/.local/share/fonts/scribesense/<key>/` | `uninstall` |
| Keybinds file + one include line in your Hyprland config; the login check (`recover --login`) | your Hyprland config folder | `uninstall` |
| Changes to GTK / fontconfig / Qt / Flatpak / browser settings | those apps' own config files | `uninstall` (restored to the originals) |

`SCRIBESENSE_DATA_DIR` and `SCRIBESENSE_FONTS_DIR` override the first and third location.

## 3. Uninstall

```bash
scribesense uninstall [--delete-data] [--remove-fonts-anyway]
```

In this order:

1. **Reset.** Every setting ScribeSense changed goes back to what it was before ScribeSense first touched it. A setting
   **you changed afterwards is skipped and left exactly as you set it** — the report lists it. If restoring one item
   fails, the rest continue and the report says so; run `scribesense reset` to try again.
2. **Generated fonts are removed, except** fonts still used by a skipped or unrestored setting, or whose use could not
   be determined. The report then says: *these apps may fall back to another font and their appearance may change.*
   `--remove-fonts-anyway` removes them regardless.
3. **Keybinds and the login check are removed.** A keybind block you edited by hand is left in place and reported.
4. **Your data is kept.** With `--delete-data` it asks you to type `delete` first (without a terminal the answer is
   no). If something is still unrestored, only presets, settings and samples are deleted and the recovery records stay.
   Deleting removes ScribeSense's records; it is not secure erasure, and afterwards ScribeSense can no longer restore
   the originals.

It ends with one line: `restored N · skipped N · failed N`.

Exit codes: `0` finished (skipped items are described, not errors) · `1` something failed · `3` another ScribeSense
operation is running — try again. It never needs the background service or the window.

Removing the program itself: delete the project folder (and its `.venv`). Uninstall first, so the settings are restored.

## 4. Status of this feature (2026-10-15)

| Part | State |
|---|---|
| `scribesense doctor` | works |
| Uninstall flow (`src/scribesense/uninstall.py`) | built and tested against fake desktops, a fake font folder and the real font cache in the sandbox |
| `scribesense uninstall` / `reset` / `recover` on the command line | **not wired yet**: `cli.py` (O2) must call `uninstall.run_uninstall(...)`, and it needs the SQLite journal (M4.10, O4) that sits on `store.JournalTables` |
| Full check on a real desktop (install → apply → uninstall → every file byte-identical) | **not run**: needs the test VM (M3.5) |
