#!/usr/bin/env bash
# NetMonitor – sestavení pro Linux (Debian/Ubuntu/…)
#   ./scripts/build_linux.sh [--skip-tests] [--deb] [--appimage] [--icons]
# Ikony se generují, pokud některá chybí; --icons vynutí regeneraci všech ikon.
# Výstup:
#   dist/NetMonitor/NetMonitor                  (onedir)
#   dist/NetMonitor-<verze>-linux-x86_64.tar.gz   (+ .desktop, ikona, install.sh)
#   dist/netmonitor_<verze>_amd64.deb             (--deb, vyžaduje dpkg-deb)
#   dist/NetMonitor-<verze>-x86_64.AppImage       (--appimage, vyžaduje appimagetool v PATH)
# Systémové závislosti Qt (Debian/Ubuntu): libxcb-cursor0 libxkbcommon-x11-0 libegl1 libgl1
set -euo pipefail

check_qt_xcb_libraries() {
  local bundle=$1 i
  local libraries=(libxcb-cursor.so.0 libxkbcommon-x11.so.0)
  local packages=(libxcb-cursor0 libxkbcommon-x11-0)
  local missing=()
  for i in "${!libraries[@]}"; do
    if [ ! -f "$bundle/${libraries[$i]}" ]; then
      echo "Chyba: v bundlu $bundle chybí ${libraries[$i]}." >&2
      missing+=("${packages[$i]}")
    fi
  done
  if [ ${#missing[@]} -gt 0 ]; then
    echo "Na build hostu doinstalujte: sudo apt install ${missing[*]}" >&2
    echo "Potom build spusťte znovu; distribuční balíčky nebyly vytvořeny." >&2
    return 1
  fi
}

cd "$(dirname "$0")/.."
VER=1.3.4
SKIP_TESTS=0; DEB=0; APPIMAGE=0; ICONS=0
for a in "$@"; do
  case "$a" in --skip-tests) SKIP_TESTS=1;; --deb) DEB=1;; --appimage) APPIMAGE=1;; --icons) ICONS=1;; esac
done

PY=$(command -v python3.13 || command -v python3.12 || command -v python3)
"$PY" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ je vyžadován"'
[ -d .venv-build ] || "$PY" -m venv .venv-build
VPY=.venv-build/bin/python
$VPY -m pip install -q --upgrade pip
$VPY -m pip install -q -r requirements-dev.txt pillow
for icon in netmonitor.png netmonitor-{16,32,48,64,128,256}.png netmonitor.ico netmonitor.icns; do
  if [ ! -f "src/netmonitor/resources/icons/$icon" ]; then ICONS=1; break; fi
done
if [ "$ICONS" -eq 1 ]; then $VPY scripts/make_icons.py; fi
if [ $SKIP_TESTS -eq 0 ]; then QT_QPA_PLATFORM=offscreen $VPY -m pytest -q; fi
$VPY -m PyInstaller packaging/netmonitor.spec --noconfirm --clean
check_qt_xcb_libraries dist/NetMonitor/_internal

ICON=src/netmonitor/resources/icons/netmonitor.png
cp packaging/linux/netmonitor.desktop dist/NetMonitor/
cp "$ICON" dist/NetMonitor/netmonitor.png
cp packaging/linux/install.sh dist/NetMonitor/ && chmod +x dist/NetMonitor/install.sh
tar -C dist -czf "dist/NetMonitor-$VER-linux-x86_64.tar.gz" NetMonitor
echo "==> dist/NetMonitor-$VER-linux-x86_64.tar.gz"

if [ $DEB -eq 1 ]; then
  command -v dpkg-deb >/dev/null || { echo "dpkg-deb chybí"; exit 1; }
  R=build/deb
  rm -rf "$R"
  mkdir -p "$R/DEBIAN" "$R/opt/netmonitor" "$R/usr/bin" "$R/usr/share/applications" "$R/usr/share/icons/hicolor/256x256/apps"
  cp -r dist/NetMonitor/* "$R/opt/netmonitor/"
  rm -f "$R/opt/netmonitor/install.sh"
  ln -s /opt/netmonitor/NetMonitor "$R/usr/bin/netmonitor"
  sed 's#^Exec=.*#Exec=/opt/netmonitor/NetMonitor#' packaging/linux/netmonitor.desktop > "$R/usr/share/applications/netmonitor.desktop"
  cp src/netmonitor/resources/icons/netmonitor-256.png "$R/usr/share/icons/hicolor/256x256/apps/netmonitor.png"
  cat > "$R/DEBIAN/control" <<EOF
Package: netmonitor
Version: $VER
Section: net
Priority: optional
Architecture: amd64
Depends: libxcb-cursor0, libxkbcommon-x11-0, libegl1, libgl1, iputils-ping
Recommends: traceroute
Suggests: network-manager
Maintainer: Marceli Zelina <noreply@zelina.eu>
Description: NetMonitor - desktopovy sitovy monitor (widget)
 Transparentni widget s WAN/LAN informacemi, DNS a paralelnim ICMP monitoringem.
EOF
  dpkg-deb --root-owner-group --build "$R" "dist/netmonitor_${VER}_amd64.deb"
  echo "==> dist/netmonitor_${VER}_amd64.deb"
fi

if [ $APPIMAGE -eq 1 ]; then
  command -v appimagetool >/dev/null || { echo "appimagetool chybí (https://github.com/AppImage/appimagetool)"; exit 1; }
  A=build/NetMonitor.AppDir
  rm -rf "$A"; mkdir -p "$A/usr/bin"
  cp -r dist/NetMonitor/* "$A/usr/bin/"
  cp packaging/linux/netmonitor.desktop "$A/netmonitor.desktop"
  sed -i 's#^Exec=.*#Exec=NetMonitor#' "$A/netmonitor.desktop"
  cp "$ICON" "$A/netmonitor.png"
  # Proměnné a příkazy se mají vyhodnotit až při spuštění vytvořeného AppRun.
  # shellcheck disable=SC2016
  printf '#!/bin/sh\nexec "$(dirname "$(readlink -f "$0")")/usr/bin/NetMonitor" "$@"\n' > "$A/AppRun"
  chmod +x "$A/AppRun"
  ARCH=x86_64 appimagetool "$A" "dist/NetMonitor-$VER-x86_64.AppImage"
  echo "==> dist/NetMonitor-$VER-x86_64.AppImage"
fi
