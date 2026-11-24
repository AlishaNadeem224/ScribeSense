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
   no). If the ScribeSense service is running it is then asked to quit; if it cannot be confirmed stopped, **nothing is
   deleted** and you are told to run `scribesense quit` and try again (exit code 1). If something is still unrestored, only presets, settings and samples are deleted and the recovery records stay.
   Deleting removes ScribeSense's records; it is not secure erasure, and afterwards ScribeSense can no longer restore
   the originals.

It ends with one line: `restored N · skipped N · failed N`.

Exit codes: `0` finished (skipped items are described, not errors) · `1` something failed · `3` another ScribeSense
operation is running — try again. It never needs the background service or the window.

Removing the program itself: delete the project folder (and its `.venv`). Uninstall first, so the settings are restored.

## 4. Fedora package (RPM / COPR) — added 2026-11-24 (W2)

The package is built from `packaging/scribesense.spec`:

    packaging/make-srpm.sh [outdir]        # source RPM from `git archive HEAD` (+ the signed .crx if present)
    rpmbuild --rebuild <outdir>/scribesense-*.src.rpm    # or let COPR build it ("make srpm": .copr/Makefile)

The signed browser extension is a release artifact: put it at `extension/dist/scribesense.crx` (or set
`SCRIBESENSE_CRX`) before `make-srpm.sh`; for COPR, attach it to the GitHub release `v<version>`. A missing `.crx` stops
the build (a release without it would remove the extension from users' browsers on upgrade); build without it only on
purpose: `SCRIBESENSE_NO_CRX=1 packaging/make-srpm.sh` (COPR: `CRX=0`). The build also checks that the `.crx` is signed
with the key pinned in `extension/manifest.json`.

**Moving from a development checkout to the package:** if you ever let the app set up the extension from a checkout
(one password prompt), files from that setup may stay in Brave's / Chrome's own folders. The packaged app detects them
and offers **Clean up…** in its banner (one password prompt); `scribesense uninstall` removes them too.

What the package installs: the Python package, `/usr/bin/scribesense`, the bundled fonts in
`/usr/share/scribesense/fonts`, the extension's files in `/usr/share/scribesense/extension`, the signed `.crx` and the
browsers' pointer files (`/usr/share/chromium/extensions` — read by Chromium **and** Brave — and
`/usr/share/google-chrome/extensions`), a desktop entry with a **Restore original** action, AppStream metainfo and an
icon. It installs nothing in any user's home: keybinds, the login hook and the browser's native-messaging host are set
up by the app for each user. A packaged install never shows a password prompt; the package manager owns the extension.

**Before `sudo dnf remove scribesense`:** every user who used ScribeSense runs **Restore original** (or
`scribesense uninstall`). The package manager runs as root and cannot restore per-user settings. If the package is
removed first, the login hook no longer runs (Hyprland keeps loading its config) and the fontconfig rule keeps pointing
at the spaced fonts in the user's font folder until they are removed by hand — harmless, but not reverted.

**Open:** ScribeSense's own licence is not chosen yet (`License: LicenseRef-TODO` in the spec); decide it before any
public COPR build. The package has not been built and install-tested end to end yet (planned: a COPR or container
build).

## 5. Status of this feature (updated 2026-11-24)

| Part | State |
|---|---|
| `scribesense doctor` | works |
| `scribesense uninstall` / `reset` / `recover --login` | wired and tested (unit + bubblewrap sandbox) |
| Fedora package | spec, desktop entry, metainfo, icon and COPR Makefile written; static checks pass (see §4) |
| Full check on a real desktop | not part of the plan any more (VM checks dropped 2026-11-24) |
