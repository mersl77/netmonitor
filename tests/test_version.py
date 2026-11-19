"""Regrese: ručně udržované verze balíčků musí odpovídat verzi projektu."""
import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
with (ROOT / "pyproject.toml").open("rb") as project_file:
    VERSION = tomllib.load(project_file)["project"]["version"]


@pytest.mark.parametrize("relative_path, pattern, count", [
    ("src/netmonitor/__init__.py", r'__version__\s*=\s*"([^"]+)"', 1),
    ("packaging/netmonitor.spec", r'"CFBundle(?:ShortVersionString|Version)":\s*"([^"]+)"', 2),
    ("packaging/windows/netmonitor.iss", r'#define\s+AppVersion\s+"([^"]+)"', 1),
    ("packaging/windows/version_info.txt", r"StringStruct\('(?:File|Product)Version',\s*'([^']+)'\)", 2),
    ("scripts/build_linux.sh", r"^VER=([^\s]+)$", 1),
    ("scripts/build_macos.sh", r"^VER=([^\s]+)$", 1),
    # Včetně názvů výstupů v nápovědě a hlášení instalátoru.
    ("scripts/build_windows.ps1", r'NetMonitor-([^\s"\\]+)-(?:win64\.zip|setup\.exe)', 4),
])
def test_version_strings_match_project(relative_path: str, pattern: str, count: int):
    text = (ROOT / relative_path).read_text(encoding="utf-8-sig")
    versions = re.findall(pattern, text, re.MULTILINE)
    assert len(versions) == count, f"{relative_path}: očekáváno {count} verzí, nalezeno {versions}"
    assert versions == [VERSION] * count, f"{relative_path}: {versions} != {VERSION}"


@pytest.mark.parametrize("field", ["filevers", "prodvers"])
def test_windows_numeric_versions_match_project(field: str):
    text = (ROOT / "packaging/windows/version_info.txt").read_text(encoding="utf-8")
    matches = re.findall(rf"\b{field}\s*=\s*\(([^)]+)\)", text)
    assert len(matches) == 1, f"{field}: očekávána právě jedna číselná verze"
    version = tuple(int(part.strip()) for part in matches[0].split(","))
    expected = (*map(int, VERSION.split(".")), 0)
    assert version == expected, f"{field}: {version} != {expected}"
