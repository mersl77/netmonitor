<#
  NetMonitor – sestavení pro Windows 10/11 x64
  Spuštění (PowerShell v kořeni projektu):
      powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
  Parametry:
      -SkipTests      nespouštět pytest
      -NoInstaller    nevytvářet instalátor ani když je Inno Setup k dispozici
      -Icons          vynutit regeneraci všech ikon (jinak jen pokud některá chybí)
  Výstup:
      dist\NetMonitor\NetMonitor.exe            (přenosná verze – celá složka)
      dist\NetMonitor-1.3.4-win64.zip
      dist\NetMonitor-1.3.4-setup.exe           (pokud je nainstalovaný Inno Setup 6)
  Nemění nic mimo složku projektu (venv v .venv-build).
#>
param([switch]$SkipTests, [switch]$NoInstaller, [switch]$Icons)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Find-Python {
    foreach ($v in @("3.13", "3.12")) {
        try { $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    try {
        $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -and [version]$ver -ge [version]"3.12") { return (& python -c "import sys; print(sys.executable)").Trim() }
    } catch {}
    throw "Python 3.12+ nenalezen. Nainstalujte z https://www.python.org/downloads/windows/ (zaškrtněte 'Add to PATH')."
}

$py = Find-Python
Write-Host "==> Python: $py" -ForegroundColor Cyan
if (-not (Test-Path .venv-build)) { & $py -m venv .venv-build }
$vpy = ".\.venv-build\Scripts\python.exe"
& $vpy -m pip install --upgrade pip | Out-Null
& $vpy -m pip install -r requirements-dev.txt pillow
if ($LASTEXITCODE -ne 0) { throw "pip install selhal" }

$iconFiles = @("netmonitor.png", "netmonitor-16.png", "netmonitor-32.png", "netmonitor-48.png",
               "netmonitor-64.png", "netmonitor-128.png", "netmonitor-256.png", "netmonitor.ico", "netmonitor.icns")
$missingIcons = $iconFiles | Where-Object { -not (Test-Path -Path "src/netmonitor/resources/icons/$_" -PathType Leaf) }
if ($Icons -or $missingIcons) {
    Write-Host "==> Ikony" -ForegroundColor Cyan
    & $vpy scripts\make_icons.py
    if ($LASTEXITCODE -ne 0) { throw "Generování ikon selhalo" }
}

if (-not $SkipTests) {
    Write-Host "==> Testy" -ForegroundColor Cyan
    & $vpy -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Testy selhaly (pro přeskočení: -SkipTests)" }
}

Write-Host "==> PyInstaller" -ForegroundColor Cyan
& $vpy -m PyInstaller packaging\netmonitor.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller selhal" }

$zip = "dist\NetMonitor-1.3.4-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\NetMonitor\* -DestinationPath $zip
Write-Host "==> Přenosná verze: dist\NetMonitor\NetMonitor.exe  |  $zip" -ForegroundColor Green

if (-not $NoInstaller) {
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($iscc) {
        & $iscc packaging\windows\netmonitor.iss
        Write-Host "==> Instalátor: dist\NetMonitor-1.3.4-setup.exe" -ForegroundColor Green
    } else {
        Write-Host "Inno Setup 6 nenalezen – instalátor přeskočen (winget install JRSoftware.InnoSetup)" -ForegroundColor Yellow
    }
}
