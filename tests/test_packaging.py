"""Regrese L25: wheel musí obsahovat všechny zdroje včetně fontů a jejich licencí."""
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "netmonitor"


def test_package_data_covers_all_resources():
    with (ROOT / "pyproject.toml").open("rb") as source:
        patterns = tomllib.load(source)["tool"]["setuptools"]["package-data"]["netmonitor"]

    covered: set[Path] = set()
    for pattern in patterns:
        matches = {path for path in PACKAGE.glob(pattern) if path.is_file()}
        assert matches, f"Prázdný vzor package-data: {pattern}"
        covered.update(matches)

    resources = {path for path in (PACKAGE / "resources").rglob("*") if path.is_file()}
    missing = sorted(str(path.relative_to(PACKAGE)) for path in resources - covered)
    assert not missing, f"Zdroje chybějící v package-data: {missing}"
