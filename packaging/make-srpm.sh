#!/bin/sh
# Build the ScribeSense source RPM from this checkout (W2, 2026-11-24).
#   packaging/make-srpm.sh [outdir]          default outdir: packaging/out
# Source0 is `git archive HEAD` (committed files only). The signed extension (Source1) is taken from
# $SCRIBESENSE_CRX, else extension/dist/scribesense.crx. A missing .crx is an ERROR (a release without the extension
# would silently remove it from every user's browsers on upgrade); build without it only when asked:
# SCRIBESENSE_NO_CRX=1 (then bcond crx = 0 is written into the packaged spec, so a rebuild stays consistent).
set -eu
top=$(git rev-parse --show-toplevel)
out=${1:-"$top/packaging/out"}
spec="$top/packaging/scribesense.spec"
version=$(rpmspec -q --srpm --qf '%{version}\n' "$spec")
test -n "$version"
work=$(mktemp -d)
# Removes only the private folder made by mktemp just above.
trap 'rm -rf -- "$work"' EXIT INT TERM
mkdir -p "$out"
git -C "$top" archive --format=tar.gz --prefix="scribesense-$version/" -o "$work/scribesense-$version.tar.gz" HEAD
crx=${SCRIBESENSE_CRX:-"$top/extension/dist/scribesense.crx"}
if [ "${SCRIBESENSE_NO_CRX:-0}" = 1 ]; then
    echo "make-srpm: SCRIBESENSE_NO_CRX=1: building without the pre-installed browser extension" >&2
    sed 's/^%bcond crx 1$/%bcond crx 0/' "$spec" > "$work/scribesense.spec"
elif [ -f "$crx" ]; then
    cp "$crx" "$work/scribesense.crx"
    cp "$spec" "$work/scribesense.spec"
else
    echo "make-srpm: no signed .crx at $crx (set SCRIBESENSE_CRX, or SCRIBESENSE_NO_CRX=1 to build without it)" >&2
    exit 1
fi
# _topdir inside the temporary folder: rpmbuild otherwise creates ~/rpmbuild.
rpmbuild -bs --define "_topdir $work/rpmbuild" --define "_sourcedir $work" --define "_srcrpmdir $out" \
    "$work/scribesense.spec"
