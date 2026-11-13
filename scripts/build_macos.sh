#!/usr/bin/env bash
# NetMonitor – sestavení pro macOS (Intel i Apple Silicon – sestavuje se pro architekturu použitého Pythonu)
#   ./scripts/build_macos.sh [--skip-tests] [--dmg]
# Výstup: dist/NetMonitor.app, volitelně dist/NetMonitor-<verze>-macos-<arch>.dmg
# Podpis a notarizace: viz docs/DISTRIBUTION.md (vyžaduje Apple Developer ID – nelze provést bez účtu).
set -euo pipefail
cd "$(dirname "$0")/.."
VER=1.4.0b1
SKIP_TESTS=0; DMG=0
for a in "$@"; do case "$a" in --skip-tests) SKIP_TESTS=1;; --dmg) DMG=1;; esac; done

PY=$(command -v python3.13 || command -v python3.12 || command -v python3)
"$PY" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ je vyžadován"'
[ -d .venv-build ] || "$PY" -m venv .venv-build
VPY=.venv-build/bin/python
$VPY -m pip install -q --upgrade pip
$VPY -m pip install -q -r requirements-dev.txt pillow
$VPY scripts/make_icons.py
if [ $SKIP_TESTS -eq 0 ]; then QT_QPA_PLATFORM=offscreen $VPY -m pytest -q; fi
$VPY -m PyInstaller packaging/netmonitor.spec --noconfirm --clean
ARCH=$(uname -m)
echo "==> dist/NetMonitor.app ($ARCH)"

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --deep --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" dist/NetMonitor.app
  echo "==> Podepsáno: $CODESIGN_IDENTITY"
fi

if [ $DMG -eq 1 ]; then
  STAGE=build/dmg; rm -rf "$STAGE"; mkdir -p "$STAGE"
  cp -R dist/NetMonitor.app "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "NetMonitor" -srcfolder "$STAGE" -ov -format UDZO "dist/NetMonitor-$VER-macos-$ARCH.dmg"
  echo "==> dist/NetMonitor-$VER-macos-$ARCH.dmg"
  if [ -n "${NOTARY_PROFILE:-}" ]; then
    xcrun notarytool submit "dist/NetMonitor-$VER-macos-$ARCH.dmg" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "dist/NetMonitor-$VER-macos-$ARCH.dmg"
  fi
fi
