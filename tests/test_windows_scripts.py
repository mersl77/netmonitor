"""Windows PowerShell 5.1 čte .ps1 bez BOM jako ANSI => české znaky / pomlčky rozbijí syntaxi."""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_windows_text_files_have_utf8_bom():
    files = list((ROOT / "scripts").glob("*.ps1")) + list((ROOT / "packaging" / "windows").glob("*.iss"))
    assert files
    for f in files:
        data = f.read_bytes()
        if any(b > 127 for b in data):
            assert data.startswith(b"\xef\xbb\xbf"), f"{f.name}: chybí UTF-8 BOM"


@pytest.mark.parametrize("command", [
    "& $py -m venv .venv-build",
    "& $vpy -m pip install --upgrade pip | Out-Null",
    "& $vpy -m pip install -r requirements-dev.txt pillow",
    r"& $vpy scripts\make_icons.py",
    "& $vpy -m pytest -q",
    r"& $vpy -m PyInstaller packaging\netmonitor.spec --noconfirm --clean",
    r"& $iscc packaging\windows\netmonitor.iss",
])
def test_windows_build_checks_native_exit_codes(command):
    """Regrese L21: chyba externího nástroje musí zastavit sestavení i v PowerShellu 5.1."""
    script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8-sig")
    assert re.search(re.escape(command) + r'\s+if \(\$LASTEXITCODE -ne 0\) \{ throw "[^"\n]+" \}', script)


def test_windows_build_checks_installer_before_success():
    """Regrese L21: samotný úspěšný návrat ISCC nestačí bez souboru instalátoru."""
    script = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8-sig")
    installer = script.split(r"& $iscc packaging\windows\netmonitor.iss", 1)[1]
    assert re.search(
        r'^\s+if \(\$LASTEXITCODE -ne 0\) \{ throw "[^"\n]+" \}'
        r'\s+if \(-not \(Test-Path -LiteralPath \$installer -PathType Leaf\)\) \{ throw "[^"\n]+" \}'
        r'\s+Write-Host "==> Instalátor: \$installer"', installer)
    assert re.search(r'\$installer = "dist\\NetMonitor-[^"\n]+-setup\.exe"', script)
