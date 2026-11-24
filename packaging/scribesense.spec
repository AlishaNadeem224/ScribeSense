# ScribeSense — Fedora RPM (PLAN_v2 W2; written 2026-11-24 by O4 with the user's go-ahead, for O3 to review).
#
# Build the source RPM from a checkout:  packaging/make-srpm.sh [outdir]   (or COPR's "make srpm": .copr/Makefile)
# Without the signed extension:          SCRIBESENSE_NO_CRX=1 packaging/make-srpm.sh   (rpmbuild: --without crx)
#
# Layout (what the code searches for — config/bundled.py, adapters/extension_host.py, safety/root_helper.py):
#   %%{python3_sitelib}/scribesense/                 the Python package (incl. store/migrations/*.sql)
#   %%{_datadir}/scribesense/fonts/                  bundled base fonts + manifest.toml (O1's K2 tree)
#   %%{_datadir}/scribesense/extension/              the browser extension's files (manifest.json = pinned ID)
#   %%{_datadir}/scribesense/scribesense.crx         the signed extension (root_helper.CRX_TARGET)
#   %%{_datadir}/scribesense/packaged                marker: the package owns the extension files (no root prompts)
#   %%{_datadir}/chromium/extensions/<id>.json       pointer read by Chromium AND Brave (non-Google Chromium builds)
#   %%{_datadir}/google-chrome/extensions/<id>.json  pointer read by Google Chrome
# Pointers are package FILES, not scriptlet writes (Fedora precedent: fedora-chromium-config-gnome); nothing under /opt.
# The package installs no per-user files: keybinds, the login hook and the native-messaging host are set up by the
# app for each user (Settings / automatic setup).
#
# Before removing the package, users run "Restore original" or `scribesense uninstall` (per-user settings cannot be
# restored by rpm, which runs as root). See the description and docs/INSTALL.md.

%bcond crx 1

Name:           scribesense
Version:        0.1.0
Release:        1%{?dist}
Summary:        System-wide reading spacing for Linux (Hyprland), with safe revert

# TODO(license): ScribeSense's own code has no license yet (team decision pending, 2026-11-24). Replace
# LicenseRef-TODO with its SPDX id before any public build. Bundled fonts: Noto Sans and Atkinson Hyperlegible are
# OFL-1.1; Carlito and Lexend declare Reserved Font Names (OFL-1.1-RFN).
License:        LicenseRef-TODO AND OFL-1.1 AND OFL-1.1-RFN
URL:            https://github.com/AlishaNadeem224/ScribeSense
# A `git archive` of the tagged release (the PyPI-style sdist does not contain fonts/ or extension/).
Source0:        %{name}-%{version}.tar.gz
%if %{with crx}
# The signed extension: a release artifact built by the maintainer with the private key (never in the repo).
Source1:        scribesense.crx
%endif

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
# %%check imports the package (GI modules import gi at module level)
BuildRequires:  python3-gobject
BuildRequires:  gtk4
BuildRequires:  libadwaita
BuildRequires:  pango
BuildRequires:  gobject-introspection

# Non-Python runtime needs (the Python ones — fontTools — come from the automatic generator). Source of truth:
# src/scribesense/doctor.py and the commands passed to run_cmd(); package names verified on Fedora 44.
Requires:       python3-gobject
Requires:       gtk4
Requires:       libadwaita
# pango-view and the Pango / PangoCairo typelibs
Requires:       pango
# the cairo-1.0 typelib that Gdk / Pango / PangoCairo reference — not pulled in by any of the above
Requires:       gobject-introspection
# fc-cache, fc-match, fc-list
Requires:       fontconfig
# gsettings + the org.gnome.desktop.interface schema (GTK adapter)
Requires:       glib2
Requires:       gsettings-desktop-schemas
# wl-paste: "read the selection" (reader)
Requires:       wl-clipboard
Recommends:     qt6ct
Recommends:     flatpak
# pkexec is used only by a development checkout's root step and the one-time "Clean up…" of an earlier development
# setup's leftovers, never by the package itself. Not a dependency: a weak Recommends on a polkit agent could pull in
# a whole desktop shell on Hyprland (review 2026-11-24).
Suggests:       polkit
# Hyprland is not in the official Fedora repos (COPR only): a weak dependency, never a hard one
Suggests:       hyprland

# The bundled fonts are the app's private source fonts, never system fonts: no font(...) provides from them.
%global __provides_exclude_from ^%{_datadir}/%{name}/fonts/.*$

%description
ScribeSense applies reading spacing (letter, word and line spacing, base font and text size) to the whole desktop:
GTK and Qt apps, Flatpak apps and the browsers, through generated spaced fonts and per-app settings. Every change is
journaled, shown first and kept only when confirmed, and "Restore original" (or `scribesense reset`) undoes it.

Before removing this package, run "Restore original" or `scribesense uninstall` for each user, so their settings
are restored: the package manager runs as root and cannot restore per-user settings.

%prep
%autosetup -n %{name}-%{version}
# Fedora 44 ships uv_build 0.10.x / 0.13.x; the upstream development pin (>=0.11.29,<0.12) is for uv's own builds.
sed -i 's/"uv_build>=0.11.29,<0.12"/"uv_build>=0.10"/' pyproject.toml
grep -q '"uv_build>=0.10"' pyproject.toml

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files scribesense

# Bundled base fonts: manifest.toml + one folder per family (names contain spaces); not fonts/tools (build scripts).
install -d -m 0755 %{buildroot}%{_datadir}/%{name}/fonts
install -p -m 0644 fonts/manifest.toml %{buildroot}%{_datadir}/%{name}/fonts/
install -d -m 0755 %{buildroot}%{_licensedir}/%{name}
for family in fonts/*/; do
    family="${family%/}"
    name="${family#fonts/}"
    [ "$name" = tools ] && continue
    install -d -m 0755 "%{buildroot}%{_datadir}/%{name}/fonts/$name"
    install -p -m 0644 "$family"/*.ttf "$family/OFL.txt" "%{buildroot}%{_datadir}/%{name}/fonts/$name/"
    install -p -m 0644 "$family/OFL.txt" "%{buildroot}%{_licensedir}/%{name}/OFL-$name.txt"
done

# The browser extension's files (not extension/test, extension/tools, keys).
install -d -m 0755 %{buildroot}%{_datadir}/%{name}/extension
install -p -m 0644 extension/manifest.json extension/background.js extension/popup.html extension/popup.js \
    %{buildroot}%{_datadir}/%{name}/extension/

%if %{with crx}
install -p -m 0644 %{SOURCE1} %{buildroot}%{_datadir}/%{name}/scribesense.crx
ext_id="$(%{python3} -I packaging/crx_pointer.py id %{SOURCE1})"
# The .crx must be the release of THIS manifest's key (the pinned ID), or the browsers would load another extension.
manifest_id="$(%{python3} -I -c 'import base64,hashlib,json,sys; k=json.load(open(sys.argv[1]))["key"]; print(hashlib.sha256(base64.b64decode(k)).hexdigest()[:32].translate(str.maketrans("0123456789abcdef","abcdefghijklmnop")))' extension/manifest.json)"
test "$ext_id" = "$manifest_id"
for folder in chromium google-chrome; do
    install -d -m 0755 %{buildroot}%{_datadir}/$folder/extensions
    %{python3} -I packaging/crx_pointer.py pointer %{SOURCE1} > %{buildroot}%{_datadir}/$folder/extensions/$ext_id.json
    chmod 0644 %{buildroot}%{_datadir}/$folder/extensions/$ext_id.json
done
%endif
# Every build carries the marker, with or without the .crx: a packaged install never runs the development root step.
printf '%%s\n' "This file marks a packaged install: the scribesense package owns the browser extension files." \
    > %{buildroot}%{_datadir}/%{name}/packaged

install -D -p -m 0644 packaging/io.github.scribesense.ScribeSense.desktop \
    %{buildroot}%{_datadir}/applications/io.github.scribesense.ScribeSense.desktop
install -D -p -m 0644 packaging/io.github.scribesense.ScribeSense.metainfo.xml \
    %{buildroot}%{_metainfodir}/io.github.scribesense.ScribeSense.metainfo.xml
install -D -p -m 0644 packaging/io.github.scribesense.ScribeSense.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.scribesense.ScribeSense.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.scribesense.ScribeSense.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.scribesense.ScribeSense.metainfo.xml
# Smoke test: everything that must import without a display (A2: recovery never needs GTK).
%pyproject_check_import -e 'scribesense.ui*' -e 'scribesense.reader.popup' -e 'scribesense.reader'
# The installed search paths really find the packaged files.
XDG_DATA_DIRS=%{buildroot}%{_datadir} PYTHONPATH=%{buildroot}%{python3_sitelib} %{python3} -I -c '
import os, sys
sys.path.insert(0, os.environ["PYTHONPATH"])
from scribesense.config import bundled
from scribesense.adapters import extension_host
assert str(bundled.fonts_root()) == os.path.join(os.environ["XDG_DATA_DIRS"], "scribesense", "fonts"), bundled.fonts_root()
assert set(bundled.base_font_files("Noto Sans")) == {"Regular", "Bold", "Italic", "BoldItalic"}
assert str(extension_host.extension_dir()).startswith(os.environ["XDG_DATA_DIRS"]), extension_host.extension_dir()
extension_host.extension_id_from_manifest()
'

%files -f %{pyproject_files}
%license %{_licensedir}/%{name}/OFL-*.txt
%dir %{_licensedir}/%{name}
%doc README.md
%{_bindir}/scribesense
%{_datadir}/%{name}/
%{_datadir}/applications/io.github.scribesense.ScribeSense.desktop
%{_metainfodir}/io.github.scribesense.ScribeSense.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.scribesense.ScribeSense.svg
%if %{with crx}
# Co-owned with the browsers' own config packages (fedora-chromium-config-gnome owns them when installed).
%dir %{_datadir}/chromium
%dir %{_datadir}/chromium/extensions
%dir %{_datadir}/google-chrome
%dir %{_datadir}/google-chrome/extensions
%{_datadir}/chromium/extensions/*.json
%{_datadir}/google-chrome/extensions/*.json
%endif

%changelog
* Tue Nov 24 2026 ScribeSense team - 0.1.0-1
- First package (W2): Python package, bundled fonts, browser extension pre-installed for Chromium, Brave and Chrome,
  desktop file with a "Restore original" action, AppStream metainfo.
