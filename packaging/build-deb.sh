#!/usr/bin/env bash
# Build the Debian binary package, and leave the checkout exactly as it was.
#
#   packaging/build-deb.sh OUTPUT_DIRECTORY
#
# Exactly one .deb appears in OUTPUT_DIRECTORY, and nothing is written anywhere
# else: the staging tree is a mktemp directory that is removed on exit. A build
# artifact left in a source tree lands in the next archive, and a stray
# directory called `web` or `dist` at the root would confuse the very front-end
# search this package exists to satisfy.
#
# dpkg-deb --build is called directly rather than through dpkg-buildpackage/dh.
# The layout here is a vendored-virtualenv application, not a Python module for
# /usr/lib/python3/dist-packages, so debhelper's Python sequence has nothing to
# contribute; debian/control and debian/changelog carry the metadata and this
# script assembles the tree. debian/rules is therefore absent on purpose, and
# `dpkg-buildpackage` is not a supported entry point.
#
# What ends up installed, and why each piece is where it is:
#
#   /usr/lib/rhizome-graph/{rhizome_graph,daemon}  the Python sources, as plain
#       files beside the virtualenv rather than inside it. That is what lets
#       /usr/bin/rhi-hook import them under the SYSTEM interpreter with no
#       virtualenv on its path -- see packaging/rhi-hook.py.
#   /usr/lib/rhizome-graph/venv                    websockets, and nothing else.
#       Created with --system-site-packages so watchdog and gi come from the
#       distribution. Built without pip: nothing installs into it afterwards.
#   /usr/lib/rhizome-graph/web                     the built front end, at the
#       path rhizome_graph/assets.py resolves on an installed system. It is
#       gitignored build output, so this script refuses to build a package
#       without it -- a daemon that serves no page starts, binds and reports
#       itself healthy, which is the silent failure this whole file guards.
#   /usr/bin/rhi, /usr/bin/rhi-hook                the two commands.
#   /usr/share/doc/rhizome-graph/                  README, licence, changelog,
#       and the settings fragment a user copies into the OBSERVED project.
set -euo pipefail

# Never leave bytecode behind: this script runs python3 with the checkout on
# PYTHONPATH, which would otherwise drop __pycache__ directories into it.
export PYTHONDONTWRITEBYTECODE=1

PACKAGE=rhizome-graph
PREFIX="/usr/lib/${PACKAGE}"
DOC_DIR="/usr/share/doc/${PACKAGE}"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "${here}/.." && pwd)"

die() {
    echo "build-deb: $*" >&2
    exit 1
}

output="${1:-}"
[ -n "${output}" ] || die "usage: $(basename "$0") OUTPUT_DIRECTORY"
mkdir -p "${output}"
output="$(cd "${output}" && pwd)"

for tool in dpkg-deb dpkg fakeroot python3; do
    command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not installed"
done
python3 -m venv --help >/dev/null 2>&1 || die "python3 -m venv is unavailable; install python3-venv"
python3 -m pip --version >/dev/null 2>&1 || die "python3 -m pip is unavailable; install python3-pip"

[ -f "${repo}/debian/control" ] || die "debian/control is missing"
[ -f "${repo}/debian/changelog" ] || die "debian/changelog is missing"
[ -d "${repo}/web/dist" ] || die "web/dist is not built; a package without it serves a blank page"

version="$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "${repo}/debian/changelog")"
[ -n "${version}" ] || die "no version on the first line of debian/changelog"
maintainer="$(sed -n 's/^Maintainer: *//p' "${repo}/debian/control" | head -n 1)"
[ -n "${maintainer}" ] || die "debian/control declares no Maintainer"
architecture="$(dpkg --print-architecture)"

# The virtualenv is bound to the minor version of the interpreter that builds
# it, so the dependency bound is derived from that interpreter rather than
# trusted from the authored file: a bound copied from an older release reads as
# correct in review and installs a virtualenv the system Python cannot run.
python_minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
python_floor="3.${python_minor}"
python_ceiling="3.$((python_minor + 1))"

stage="$(mktemp -d "${TMPDIR:-/tmp}/rhizome-graph-deb.XXXXXX")"
trap 'rm -rf "${stage}"' EXIT

install -d -m 0755 "${stage}${PREFIX}"

# --- the Python sources ----------------------------------------------------
for tree in rhizome_graph daemon; do
    cp -R "${repo}/${tree}" "${stage}${PREFIX}/"
done
find "${stage}${PREFIX}" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "${stage}${PREFIX}" -name '*.py[co]' -type f -delete

# ...and then put the bytecode back, deliberately. /usr/lib is root-owned and
# the hook runs as the user whose agent fired it, so without a shipped
# __pycache__ the interpreter recompiles rhizome_graph/hook.py on EVERY tool
# call and the write that would end that fails silently. This is the one place
# in the project where the interpreter cannot fix it for itself.
#
# unchecked-hash, not the default: timestamp bytecode is discarded when the
# installed .py's mtime or size disagrees with what was recorded here, and dpkg
# stamps mtimes from the archive while source and bytecode are stamped by
# different steps above -- a fallback that is silent, and lands back on the
# recompiling this exists to end while the package still contains bytecode.
# checked-hash instead re-reads and re-hashes the whole source on every import
# to catch edits to a dpkg-managed file, which is `dpkg -V`'s job, not the
# agent loop's. Unchecked matches the real ownership: source and bytecode are
# only ever replaced together, by the package manager.
#
# Run by the same python3 the venv is built with, so the .pyc tag is the
# cpython-3N the declared Depends range brackets. This must stay after the
# deletion above (which would take the bytecode with it) and before
# Installed-Size is measured below (which would otherwise under-declare).
# compileall ignores PYTHONDONTWRITEBYTECODE, which is why that export needs no
# change here.
python3 -m compileall -q --invalidation-mode unchecked-hash "${stage}${PREFIX}" \
    || die "byte-compiling the Python sources failed"

# --- the built front end ---------------------------------------------------
cp -R "${repo}/web/dist" "${stage}${PREFIX}/web"

# --- the vendored virtualenv: websockets, and nothing else -----------------
venv="${stage}${PREFIX}/venv"
python3 -m venv --system-site-packages --without-pip "${venv}"
site="${venv}/lib/python${python_floor}/site-packages"
[ -d "${site}" ] || die "the virtualenv has no ${site}"
python3 -m pip install --quiet --no-compile --target "${site}" 'websockets>=13'
[ -d "${site}/websockets/asyncio" ] || die "the virtualenv holds no websockets.asyncio, which the daemon imports"
find "${venv}" -name '__pycache__' -type d -prune -exec rm -rf {} +

# The staging path is baked into the activation scripts and into the `command`
# line of pyvenv.cfg. Rewriting it costs one sed and keeps an installed
# virtualenv self-consistent; symlinks are skipped, since sed -i would replace
# bin/python with a regular file.
while IFS= read -r file; do
    [ -L "${file}" ] && continue
    sed -i "s|${stage}||g" "${file}"
done < <(find "${venv}/bin" -maxdepth 1 -type f; echo "${venv}/pyvenv.cfg")

# --- the two commands ------------------------------------------------------
install -D -m 0755 "${repo}/packaging/rhi.sh" "${stage}/usr/bin/rhi"
install -D -m 0755 "${repo}/packaging/rhi-hook.py" "${stage}/usr/bin/rhi-hook"

# --- documentation, including the block that turns attribution on ----------
install -D -m 0644 "${repo}/README.md" "${stage}${DOC_DIR}/README.md"
install -D -m 0644 "${repo}/LICENSE" "${stage}${DOC_DIR}/copyright"
gzip -9 -n -c "${repo}/debian/changelog" > "${stage}${DOC_DIR}/changelog.Debian.gz"
chmod 0644 "${stage}${DOC_DIR}/changelog.Debian.gz"

# Generated from rhizome_graph.hookinstall rather than written out by hand, so
# the fragment a user copies cannot drift from the matcher the code recognises.
# Without this block every event arrives with an empty agent, an empty agent
# never creates an actor, and the graph updates with nobody on camera.
PYTHONPATH="${repo}" python3 - "${stage}${DOC_DIR}/claude-settings.json" <<'PYTHON'
import json
import sys

from rhizome_graph.hookinstall import hook_block

with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"hooks": hook_block("/usr/bin/rhi-hook")}, handle, indent=2)
    handle.write("\n")
PYTHON
chmod 0644 "${stage}${DOC_DIR}/claude-settings.json"

# --- the binary control ----------------------------------------------------
# Derived from the authored debian/control, so the dependency decisions under
# review are the ones that ship. Architecture is concrete here (the virtualenv
# may hold a compiled extension) where the source says `any`, the python3 bound
# is the building interpreter's, and Version, Maintainer and Installed-Size come
# from outside the binary stanza.
install -d -m 0755 "${stage}/DEBIAN"
installed_size="$(du -s -k --exclude=DEBIAN "${stage}" | cut -f1)"

binary_fields() {
    awk -v package="Package: ${PACKAGE}" '
        $0 == package { inside = 1 }
        inside && /^$/ { exit }
        inside && /^Description:/ { exit }
        inside && /^Architecture:/ { next }
        inside { print }
    ' "${repo}/debian/control"
}

binary_description() {
    awk -v package="Package: ${PACKAGE}" '
        $0 == package { found = 1 }
        found && /^Description:/ { inside = 1 }
        inside && /^$/ { exit }
        inside { print }
    ' "${repo}/debian/control"
}

{
    binary_fields | sed \
        -e "s|python3 (>= 3\.[0-9][0-9]*)|python3 (>= ${python_floor})|" \
        -e "s|python3 (<< 3\.[0-9][0-9]*)|python3 (<< ${python_ceiling})|"
    printf 'Architecture: %s\n' "${architecture}"
    printf 'Version: %s\n' "${version}"
    printf 'Maintainer: %s\n' "${maintainer}"
    printf 'Installed-Size: %s\n' "${installed_size}"
    binary_description
} > "${stage}/DEBIAN/control"
chmod 0644 "${stage}/DEBIAN/control"

grep -q '^Package: ' "${stage}/DEBIAN/control" || die "the generated control has no Package field"
grep -q '^Depends:' "${stage}/DEBIAN/control" || die "the generated control has no Depends field"

# --- the archive -----------------------------------------------------------
# fakeroot, so every entry is recorded as root/root: a package built as an
# ordinary user installs files owned by a uid the target machine may not have.
deb="${output}/${PACKAGE}_${version}_${architecture}.deb"
fakeroot dpkg-deb --build "${stage}" "${deb}" >/dev/null

echo "${deb}"
