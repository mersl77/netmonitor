"""Kontrola a stažení aktualizací.

Zdroj aktualizací (Nastavení → Obecné → URL aktualizací) může být:
  * odkaz na GitHub repozitář / jeho releases, např. https://github.com/mersl77/netmonitor/releases
    -> GitHub REST API /repos/{owner}/{repo}/releases/latest (funguje jen pro VEŘEJNÝ repozitář)
  * vlastní JSON manifest (např. na zelina.eu):
      {"version": "1.2.0", "url": "https://…/stranka-vydani", "notes": "…",
       "windows": {"url": "https://…/NetMonitor-1.2.0-setup.exe", "sha256": "…"}}

Stažený instalátor se vždy ověří proti SHA-256 (GitHub ho uvádí u každého souboru jako "digest").
Bez otisku se automatická instalace neprovede – nabídne se jen stránka ke stažení.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import __version__

GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})(?:/.*)?$")
WIN_ASSET_RE = re.compile(r"^NetMonitor-[0-9][0-9A-Za-z.\-]*-setup\.exe$")
MAX_DOWNLOAD = 400 * 1024 * 1024
DEFAULT_SOURCE = "https://github.com/mersl77/netmonitor/releases"


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    page_url: str
    notes: str = ""
    asset_name: str = ""
    asset_url: str = ""
    sha256: str = ""
    size: int = 0

    @property
    def can_auto_install(self) -> bool:
        return bool(self.asset_url and self.sha256 and self.asset_url.startswith("https://"))


class UpdateError(Exception):
    pass


_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?$", re.I)


def version_key(v: str) -> tuple:
    """Řazení stabilních a PEP-440-like předběžných verzí.

    1.4.0a1 < 1.4.0b1 < 1.4.0rc1 < 1.4.0.
    """
    m = _VERSION_RE.fullmatch(str(v).strip())
    if m is None:
        raise ValueError("Nepodporovaný formát verze")
    major, minor, patch, stage, serial = m.groups()
    rank = {"a": 0, "b": 1, "rc": 2, None: 3}[stage.lower() if stage else None]
    return int(major), int(minor), int(patch or 0), rank, int(serial or 0)


def is_newer(latest: str, current: str = __version__) -> bool:
    try:
        return version_key(latest) > version_key(current)
    except ValueError:
        return False


def api_url_for(source: str) -> tuple[str, str]:
    """('github'|'manifest', URL dotazu). Neplatný zdroj => UpdateError."""
    s = (source or "").strip()
    if not s.startswith("https://"):
        raise UpdateError("URL aktualizací musí začínat https://")
    m = GITHUB_RE.match(s)
    if m:
        owner, repo = m.group(1), m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return "github", f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return "manifest", s


def parse_github(data: dict, platform: str = sys.platform) -> UpdateInfo:
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError(str(data.get("message") if isinstance(data, dict) else "") or "Neplatná odpověď GitHubu")
    ver = str(data["tag_name"]).lstrip("vV")
    asset = {}
    if platform == "win32":
        for a in data.get("assets") or []:
            if WIN_ASSET_RE.match(str(a.get("name", ""))):
                asset = a
                break
    digest = str(asset.get("digest") or "")
    sha = digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else ""
    return UpdateInfo(version=ver, page_url=str(data.get("html_url") or ""), notes=str(data.get("body") or "")[:4000],
                      asset_name=str(asset.get("name", "")), asset_url=str(asset.get("browser_download_url", "")),
                      sha256=sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", size=int(asset.get("size") or 0))


def parse_manifest(data: dict, platform: str = sys.platform) -> UpdateInfo:
    if not isinstance(data, dict) or not data.get("version"):
        raise UpdateError("Manifest neobsahuje 'version'")
    win = data.get("windows") if platform == "win32" and isinstance(data.get("windows"), dict) else {}
    sha = str(win.get("sha256", "")).lower()
    url = str(win.get("url", ""))
    return UpdateInfo(version=str(data["version"]).lstrip("vV"), page_url=str(data.get("url") or ""),
                      notes=str(data.get("notes") or "")[:4000],
                      asset_name=url.rsplit("/", 1)[-1] if url else "", asset_url=url,
                      sha256=sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "", size=int(win.get("size") or 0))


def _get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"NetMonitor/{__version__}",
                                               "Accept": "application/vnd.github+json, application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 – jen https (ověřeno výše)
            return json.loads(r.read(1_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("Zdroj aktualizací nenalezen (404) – soukromý repozitář nebo žádné vydání") from None
        raise UpdateError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise UpdateError(f"Kontrola aktualizací selhala: {e}") from None


def check(source: str, fetch: Callable[[str], dict] = _get_json,
          platform: str = sys.platform, allow_beta: bool = False) -> UpdateInfo:
    kind, url = api_url_for(source)
    if kind != "github" or not allow_beta:
        data = fetch(url)
        return parse_github(data, platform) if kind == "github" else parse_manifest(data, platform)

    # /releases/latest beta vydání záměrně ignoruje; seznam je obsahuje.
    releases_url = url.removesuffix("/latest") + "?per_page=30"
    releases = fetch(releases_url)
    if not isinstance(releases, list):
        raise UpdateError("GitHub nevrátil seznam vydání")
    candidates = [
        release for release in releases
        if isinstance(release, dict) and not release.get("draft")
        and version_key_safe(str(release.get("tag_name", ""))) is not None
    ]
    if not candidates:
        raise UpdateError("GitHub neobsahuje žádné kompatibilní vydání")
    newest = max(candidates, key=lambda item: version_key(str(item["tag_name"])))
    return parse_github(newest, platform)


def version_key_safe(value: str) -> tuple | None:
    try:
        return version_key(value)
    except ValueError:
        return None


def download(info: UpdateInfo, dest_dir: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None, opener=urllib.request.urlopen) -> Path:
    """Stáhne instalátor a ověří SHA-256. Při neshodě soubor smaže a vyhodí UpdateError."""
    if not info.can_auto_install:
        raise UpdateError("Pro tuto verzi není k dispozici ověřitelný instalátor")
    if not WIN_ASSET_RE.match(info.asset_name):
        raise UpdateError(f"Neočekávaný název souboru: {info.asset_name!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / info.asset_name
    tmp = final.with_suffix(".part")
    h = hashlib.sha256()
    done = 0
    req = urllib.request.Request(info.asset_url, headers={"User-Agent": f"NetMonitor/{__version__}"})
    try:
        with opener(req, timeout=30) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.size or 0)
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Stahování zrušeno")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_DOWNLOAD:
                    raise UpdateError("Soubor je neočekávaně velký")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        tmp.unlink(missing_ok=True)
        raise
    except (OSError, urllib.error.URLError) as e:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"Stahování selhalo: {e}") from None
    if h.hexdigest() != info.sha256:
        tmp.unlink(missing_ok=True)
        raise UpdateError("Kontrolní součet SHA-256 nesouhlasí – soubor byl poškozen nebo podvržen, neinstaluji")
    tmp.replace(final)
    return final


def installer_args(path: Path) -> list[str]:
    """Tichá instalace Inno Setup; aplikaci po instalaci spustí sám instalátor ([Run] … Check: WizardSilent)."""
    return [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/SP-"]


def can_self_install() -> bool:
    """Jen nainstalovaná verze pro Windows (EXE z instalátoru – vedle leží unins000.exe)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).parent / "unins000.exe").exists()
