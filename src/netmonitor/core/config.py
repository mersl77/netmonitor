"""Konfigurace aplikace – dataclassy, validace, JSON load/save, import/export.

Konfigurace je lokální JSON (schema_version). Neznámé klíče jsou ignorovány,
chybějící a chybně typovaná pole doplněna výchozími hodnotami, číselné hodnoty
oříznuty do mezí. Neplatné položky seznamu cílů se vynechávají.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from ..utils.local_json import quarantine_json, read_json_object
from ..utils.paths import config_dir
from ..utils.validation import host_kind

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
CONFIG_FILENAME = "config.json"

ICON_CHOICES = ("auto", "globe", "server", "dns", "wifi", "router", "modem", "box", "cloud")
MODE_DESKTOP = "desktop"
MODE_ONTOP = "ontop"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


@dataclass
class PingTarget:
    name: str
    host: str
    enabled: bool = True
    interval_s: float = 2.0
    timeout_s: float = 1.0
    retries: int = 3            # počet po sobě jdoucích selhání před vyhlášením výpadku
    icon: str = "auto"
    show_rtt: bool = True       # False => zobrazí "OK" místo ms (jako ve schváleném návrhu)
    internet_probe: str = "auto"  # auto | yes | no – zda se cíl počítá do stavu internetu
    id: str = field(default_factory=new_id)
    kind: str = "target"          # target | section (nadpis skupiny – nic se nepinguje)
    mac: str = ""                 # MAC adresa pro Wake-on-LAN (volitelné)
    wol_broadcast: str = ""       # vlastní broadcast pro WoL (např. jiná podsíť; prázdné = automaticky)

    @property
    def is_section(self) -> bool:
        return self.kind == "section"

    def normalized(self) -> "PingTarget":
        t = copy.copy(self)
        t.kind = "section" if t.kind == "section" else "target"
        if t.kind == "section":
            t.name = str(t.name).strip()[:48] or "Sekce"
            t.host = ""
            t.id = str(t.id or new_id())
            return t
        t.name = str(t.name).strip()[:64] or t.host
        t.mac = str(t.mac or "").strip()[:32]
        t.wol_broadcast = str(t.wol_broadcast or "").strip()[:15]
        t.host = str(t.host).strip()
        t.enabled = bool(t.enabled)
        t.interval_s = _clamp(t.interval_s, 1, 60, 2.0)
        t.timeout_s = _clamp(t.timeout_s, 0.2, 10, 1.0)
        t.retries = int(_clamp(t.retries, 1, 20, 3))
        t.icon = t.icon if t.icon in ICON_CHOICES else "auto"
        t.show_rtt = bool(t.show_rtt)
        t.internet_probe = t.internet_probe if t.internet_probe in ("auto", "yes", "no") else "auto"
        t.id = str(t.id or new_id())
        return t

    def validate(self) -> list[str]:
        errs = []
        if not self.name.strip():
            errs.append("Název nesmí být prázdný")
        if self.is_section:
            return errs
        if host_kind(self.host) == "invalid":
            errs.append(f"Neplatná IP adresa nebo doména: {self.host!r}")
        if self.mac:
            from .wol import parse_mac
            try:
                parse_mac(self.mac)
            except ValueError as e:
                errs.append(str(e))
        if self.wol_broadcast and host_kind(self.wol_broadcast) != "ipv4":
            errs.append(f"Neplatná broadcast adresa pro WoL: {self.wol_broadcast!r}")
        if not (1 <= self.interval_s <= 60):
            errs.append("Interval musí být 1–60 s")
        if not (0.2 <= self.timeout_s <= 10):
            errs.append("Timeout musí být 0,2–10 s")
        if self.timeout_s > self.interval_s:
            errs.append("Timeout by neměl být delší než interval")
        return errs


@dataclass
class Thresholds:
    green_max_ms: float = 50.0
    orange_max_ms: float = 150.0


@dataclass
class StatusColors:
    ok: str = "#3dff6e"
    warn: str = "#ffaa2b"
    bad: str = "#ff4d5e"
    unknown: str = "#8a9bb0"


@dataclass
class GeneralConfig:
    language: str = "cs"
    autostart: bool = False
    start_minimized: bool = False
    minimize_to_tray: bool = True
    check_updates: bool = True
    update_url: str = "https://github.com/mersl77/netmonitor/releases"   # GitHub repo nebo JSON manifest
    auto_update: bool = False     # instalovat nové verze bez ptaní (jen nainstalovaná verze pro Windows)
    skipped_version: str = ""


@dataclass
class AppearanceConfig:
    mode: str = MODE_DESKTOP
    opacity: float = 1.0          # průhlednost celého okna (text i panel)
    glass_alpha: float = 1.0      # krytí pozadí panelu: 1 = neprůhledné, 0 = zcela průhledné
    width: int = 600
    height: int = 850
    x: int | None = None
    y: int | None = None
    screen: str = ""
    locked: bool = False
    click_through: bool = False
    neon_effects: bool = True
    font_scale: float = 1.0
    compact: bool = False
    colors: StatusColors = field(default_factory=StatusColors)


@dataclass
class NetworkConfig:
    adapter_auto: bool = True
    adapter_name: str = ""
    external_api: bool = True       # hlavní vypínač všech externích dotazů (WAN IP, ISP)
    wan_interval_s: int = 300
    isp_detection: bool = True
    isp_provider: str = "ipinfo"    # ipinfo | ripestat
    isp_cache_hours: int = 24
    show_ipv6: bool = False
    refresh_s: int = 5
    gateway_probe: bool = True
    linux_prefer_xwayland: bool = True   # na Waylandu spustit přes XWayland (pozice/režim okna fungují)
    ip_profiles: list = field(default_factory=list)   # uložené profily nastavení IP (viz core/ipconfig.py)


@dataclass
class PingConfig:
    max_concurrent: int = 16
    thresholds: Thresholds = field(default_factory=Thresholds)
    targets: list[PingTarget] = field(default_factory=list)


@dataclass
class NotificationConfig:
    enabled: bool = True
    target_down: bool = True
    target_up: bool = True
    wan_change: bool = True
    internet_down: bool = True
    internet_up: bool = True
    high_latency: bool = False
    delay_s: int = 10                 # prodleva před upozorněním (stav musí trvat)
    high_latency_ms: float = 150.0
    high_latency_duration_s: int = 120
    cooldown_s: int = 300             # min. odstup opakovaných upozornění pro stejný cíl/typ


@dataclass
class HistoryConfig:
    enabled: bool = True
    retention_days: int = 90


@dataclass
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    appearance: AppearanceConfig = field(default_factory=AppearanceConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    ping: PingConfig = field(default_factory=PingConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    schema_version: int = SCHEMA_VERSION

    # --- serializace -------------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppConfig":
        if data is not None and not isinstance(data, dict):
            raise ValueError("Konfigurace musí být JSON objekt")
        data = migrate_dict(dict(data or {}))
        cfg = cls()
        cfg.general = _load_dc(GeneralConfig, data.get("general", {}), "general")
        ap = data.get("appearance")
        cfg.appearance = _load_dc(AppearanceConfig, data.get("appearance", {}), "appearance")
        cfg.appearance.colors = _load_dc(StatusColors, ap.get("colors", {}) if isinstance(ap, dict) else {},
                                         "appearance.colors")
        cfg.network = _load_dc(NetworkConfig, data.get("network", {}), "network")
        pg = data.get("ping")
        cfg.ping = _load_dc(PingConfig, data.get("ping", {}), "ping")
        cfg.ping.thresholds = _load_dc(Thresholds, pg.get("thresholds", {}) if isinstance(pg, dict) else {},
                                      "ping.thresholds")
        raw_targets = pg.get("targets") if isinstance(pg, dict) else None
        cfg.ping.targets = targets_from_list(raw_targets) if isinstance(raw_targets, list) else default_targets()
        cfg.notifications = _load_dc(NotificationConfig, data.get("notifications", {}), "notifications")
        cfg.history = _load_dc(HistoryConfig, data.get("history", {}), "history")
        return cfg.normalized()

    def normalized(self) -> "AppConfig":
        a = self.appearance
        a.mode = a.mode if a.mode in (MODE_DESKTOP, MODE_ONTOP) else MODE_DESKTOP
        a.opacity = _clamp(a.opacity, 0.2, 1.0, 1.0)
        a.glass_alpha = _clamp(a.glass_alpha, 0.0, 1.0, 1.0)
        a.width = int(_clamp(a.width, 260, 2000, 600))
        a.height = int(_clamp(a.height, 340, 2400, 850))
        a.font_scale = _clamp(a.font_scale, 0.6, 2.0, 1.0)
        n = self.network
        n.wan_interval_s = int(_clamp(n.wan_interval_s, 30, 86400, 300))
        n.refresh_s = int(_clamp(n.refresh_s, 2, 300, 5))
        n.isp_cache_hours = int(_clamp(n.isp_cache_hours, 1, 24 * 30, 24))
        n.isp_provider = n.isp_provider if n.isp_provider in ("ipinfo", "ripestat") else "ipinfo"
        from .ipconfig import sanitize_profiles
        profiles = []
        raw_profiles = n.ip_profiles if isinstance(n.ip_profiles, list) else []
        for index, profile in enumerate(raw_profiles[:50]):
            try:
                profiles.extend(sanitize_profiles([profile]))
            except (TypeError, ValueError):
                log.warning("Neplatné pole network.ip_profiles[%d] – profil vynechávám", index)
        n.ip_profiles = profiles
        p = self.ping
        p.max_concurrent = int(_clamp(p.max_concurrent, 1, 128, 16))
        t = p.thresholds
        t.green_max_ms = _clamp(t.green_max_ms, 1, 10000, 50)
        t.orange_max_ms = _clamp(t.orange_max_ms, t.green_max_ms, 20000, max(150, t.green_max_ms))
        p.targets = [x.normalized() for x in p.targets]
        nt = self.notifications
        nt.delay_s = int(_clamp(nt.delay_s, 0, 3600, 10))
        nt.cooldown_s = int(_clamp(nt.cooldown_s, 0, 86400, 300))
        nt.high_latency_duration_s = int(_clamp(nt.high_latency_duration_s, 10, 86400, 120))
        self.history.retention_days = int(_clamp(self.history.retention_days, 1, 3650, 90))
        self.general.language = self.general.language if self.general.language in ("cs", "en") else "cs"
        return self

    def copy(self) -> "AppConfig":
        return copy.deepcopy(self)

    def target_by_id(self, tid: str) -> PingTarget | None:
        return next((t for t in self.ping.targets if t.id == tid), None)

    def real_targets(self) -> list[PingTarget]:
        """Jen skutečné cíle (bez nadpisů sekcí) – pro monitoring, historii a stav připojení."""
        return [t for t in self.ping.targets if not t.is_section]


def _valid_field_type(default: Any, value: Any) -> bool:
    # Číselné meze řeší normalized(); x/y musí vejít do Qt signed int.
    if default is None:
        return value is None or (type(value) is int and -(2**31) <= value < 2**31)
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return type(value) in (int, float) and -float("inf") < value < float("inf")
    return isinstance(value, type(default))


def _load_dc(cls, data, prefix: str):
    obj = cls()
    if not isinstance(data, dict):
        log.warning("Neplatné pole %s – používám výchozí hodnotu", prefix)
        return obj
    for f in fields(cls):
        default = getattr(obj, f.name)
        if f.name not in data or hasattr(default, "__dataclass_fields__"):
            continue
        value = data[f.name]
        if not _valid_field_type(default, value):
            log.warning("Neplatné pole %s.%s – používám výchozí hodnotu", prefix, f.name)
            continue
        setattr(obj, f.name, value)
    return obj


def targets_from_list(items: Any) -> list[PingTarget]:
    out: list[PingTarget] = []
    seen_ids: set[str] = set()
    if not isinstance(items, list):
        log.warning("Neplatné pole ping.targets – seznam cílů nelze načíst")
        return out
    names = {f.name for f in fields(PingTarget)}
    defaults = PingTarget(name="", host="")
    for index, it in enumerate(items):
        if not isinstance(it, dict):
            log.warning("Neplatné pole ping.targets[%d] – položku vynechávám", index)
            continue
        invalid_fields = [k for k in names if k in it and not _valid_field_type(getattr(defaults, k), it[k])]
        if invalid_fields:
            for name in invalid_fields:
                log.warning("Neplatné pole ping.targets[%d].%s – položku vynechávám", index, name)
            continue
        is_section = it.get("kind") == "section"
        if not is_section and not it.get("host"):
            log.warning("Neplatné pole ping.targets[%d].host – položku vynechávám", index)
            continue
        kw = {k: v for k, v in it.items() if k in names}
        kw.setdefault("name", it.get("host") or "Sekce")
        kw.setdefault("host", "")
        t = PingTarget(**kw).normalized()
        if t.id in seen_ids:  # duplicitní ID -> nové, záznamy se NESLUČUJÍ
            t.id = new_id()
        seen_ids.add(t.id)
        out.append(t)
    return out


def default_targets() -> list[PingTarget]:
    """Výchozí seznam ze specifikace (5.1). Stejná IP u dvou zařízení je povolená."""
    spec = [
        ("Zelina", "zelina.eu", "globe", True),
        ("Seznam", "seznam.cz", "globe", True),
        ("O2 DNS Primár", "160.218.161.60", "dns", True),
        ("O2 DNS Sekundár", "194.228.211.33", "dns", True),
        ("Google DNS", "8.8.8.8", "dns", True),
        ("Huawei anténa", "192.168.1.1", "wifi", False),
        ("Greenpacket H5", "192.168.0.1", "wifi", False),
        ("SB3 router", "192.168.0.1", "router", False),
        ("O2 modem", "10.0.0.138", "modem", False),
        ("Smart Box", "10.0.1.138", "box", False),
    ]
    return [PingTarget(name=n, host=h, icon=i, show_rtt=r) for n, h, i, r in spec]


def new_section(name: str) -> PingTarget:
    return PingTarget(name=name, host="", kind="section").normalized()


def migrate_dict(data: dict) -> dict:
    """Migrace starších verzí konfigurace (připraveno pro budoucí schémata)."""
    ver = data.get("schema_version", 1)
    if not isinstance(ver, int) or ver < 1:
        ver = 1
    if ver < 2:   # 1.0.1: lepší čitelnost na světlé tapetě – staré výchozí krytí skla 0.55 -> 0.75
        ap = data.get("appearance")
        if isinstance(ap, dict) and ap.get("glass_alpha") == 0.55:
            ap["glass_alpha"] = 0.75
        ping = data.get("general")
        if isinstance(ping, dict) and not ping.get("update_url"):
            ping.pop("update_url", None)          # doplní se nový výchozí odkaz na GitHub
    if ver < 3:   # 1.2.0: výchozí neprůhledné pozadí; kontrola aktualizací z GitHubu
        ap = data.get("appearance")
        if isinstance(ap, dict) and ap.get("glass_alpha") in (0.55, 0.75):
            ap["glass_alpha"] = 1.0
        gen = data.get("general")
        if isinstance(gen, dict):
            url = str(gen.get("update_url") or "")
            if not url or url.startswith("https://github.com/mersl77/netmonitor"):
                gen.pop("update_url", None)
                gen["check_updates"] = True
    data["schema_version"] = SCHEMA_VERSION
    return data


# --- soubory -------------------------------------------------------------

def default_config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def load_config(path: Path | None = None) -> AppConfig:
    path = path or default_config_path()
    try:
        data = read_json_object(path)
    except FileNotFoundError:
        return AppConfig.from_dict({})
    except (OSError, ValueError) as e:
        quarantine_json(path, e)
        return AppConfig.from_dict({})
    return AppConfig.from_dict(data)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    path = path or default_config_path()
    # Když při load selhalo odložení (např. oprávnění), pozdější autosave
    # nesmí originál přepsat. Kontrolujeme jen čitelnost JSON objektu,
    # nikoli typy jeho polí. Chrání i soubor poškozený až za běhu aplikace.
    try:
        read_json_object(path)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        if not quarantine_json(path, e):
            raise OSError(f"Konfiguraci {path} nelze uložit bez zálohy původního souboru") from e
    atomic_write_json(path, cfg.normalized().to_dict())


def export_config(cfg: AppConfig, path: Path) -> None:
    atomic_write_json(path, cfg.normalized().to_dict())


def import_config(path: Path, current: AppConfig | None = None) -> AppConfig:
    """Import celé konfigurace, nebo jen seznamu cílů ([{...}] / {"targets": [...]}).

    Při importu samotných cílů zůstane zbytek nastavení z `current`.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    targets_only = None
    if isinstance(data, list):
        targets_only = data
    elif isinstance(data, dict) and "targets" in data and "ping" not in data:
        targets_only = data["targets"]
    if targets_only is not None:
        targets = targets_from_list(targets_only)
        if not targets:
            raise ValueError("Soubor neobsahuje žádné platné cíle")
        cfg = (current or AppConfig.from_dict({})).copy()
        cfg.ping.targets = targets
        return cfg.normalized()
    if not isinstance(data, dict):
        raise ValueError("Neplatný formát souboru")
    return AppConfig.from_dict(data)
