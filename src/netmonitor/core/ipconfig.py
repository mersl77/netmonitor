"""Nastavení IPv4 adresy síťové karty (DHCP / pevná IP, DNS) – validace a sestavení příkazů.

Samotné provedení je v platformních modulech (vyžaduje práva správce):
  * Windows: netsh přes PowerShell skript spuštěný se zvýšenými právy (UAC dotaz)
  * Linux:   nmcli (NetworkManager, oprávnění přes polkit)
  * macOS:   networksetup přes osascript "with administrator privileges"

Všechny hodnoty se před sestavením příkazu validují; do příkazů se nikdy
nevkládá neověřený text (název rozhraní se escapuje pro daný interpret).
"""
from __future__ import annotations

import ipaddress
from dataclasses import asdict, dataclass, field

PROFILE_KEYS = ("name", "dhcp", "ip", "mask", "gateway", "dns_dhcp", "dns")


@dataclass
class IpSettings:
    iface: str
    dhcp: bool = True
    ip: str = ""
    mask: str = "255.255.255.0"   # akceptuje i prefix "24" nebo "/24"
    gateway: str = ""
    dns_dhcp: bool = True
    dns: list[str] = field(default_factory=list)

    # -- odvozené hodnoty -------------------------------------------------
    @property
    def prefix(self) -> int:
        return parse_mask(self.mask)

    @property
    def mask_dotted(self) -> str:
        return str(ipaddress.IPv4Network(f"0.0.0.0/{self.prefix}").netmask)

    @property
    def dns_list(self) -> list[str]:
        return [d.strip() for d in self.dns if d and d.strip()]

    def to_profile(self, name: str) -> dict:
        d = asdict(self)
        d.pop("iface", None)
        d["name"] = name
        return d

    @classmethod
    def from_profile(cls, iface: str, p: dict) -> "IpSettings":
        dns = p.get("dns") or []
        if isinstance(dns, str):
            dns = [dns]
        elif not isinstance(dns, (list, tuple)):
            dns = []
        return cls(iface=iface, dhcp=bool(p.get("dhcp", True)), ip=str(p.get("ip", "")),
                   mask=str(p.get("mask", "255.255.255.0")), gateway=str(p.get("gateway", "")),
                   dns_dhcp=bool(p.get("dns_dhcp", True)), dns=[str(x) for x in dns[:8]])

    # -- validace ---------------------------------------------------------
    def validate(self) -> tuple[list[str], list[str]]:
        """Vrátí (chyby, varování). S chybami se nesmí aplikovat."""
        errs: list[str] = []
        warns: list[str] = []
        if not valid_iface_name(self.iface):
            errs.append("Neplatný název síťové karty")
        if not self.dhcp:
            try:
                ip = ipaddress.IPv4Address(self.ip.strip())
            except ValueError:
                errs.append(f"Neplatná IP adresa: {self.ip!r}")
                ip = None
            try:
                prefix = parse_mask(self.mask)
            except ValueError as e:
                errs.append(str(e))
                prefix = None
            if ip is not None and prefix is not None:
                net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
                    errs.append(f"{ip} nelze použít jako adresu karty")
                elif prefix <= 30 and ip in (net.network_address, net.broadcast_address):
                    errs.append(f"{ip} je adresa sítě nebo broadcast pro /{prefix}")
                if self.gateway.strip():
                    try:
                        gw = ipaddress.IPv4Address(self.gateway.strip())
                        if gw == ip:
                            errs.append("Brána nesmí být stejná jako IP adresa")
                        elif gw not in net:
                            warns.append(f"Brána {gw} není v síti {net} – internet nemusí fungovat")
                    except ValueError:
                        errs.append(f"Neplatná brána: {self.gateway!r}")
                else:
                    warns.append("Bez brány bude dostupná jen lokální síť (žádný internet)")
        if not self.dns_dhcp:
            if not self.dns_list:
                errs.append("Zadejte alespoň jeden DNS server, nebo zvolte DNS automaticky")
            for d in self.dns_list:
                try:
                    ipaddress.IPv4Address(d)
                except ValueError:
                    errs.append(f"Neplatný DNS server: {d!r}")
        elif not self.dhcp:
            warns.append("Pevná IP s automatickými DNS: bez DHCP nebudou DNS servery přiděleny")
        return errs, warns

    def summary(self) -> str:
        if self.dhcp:
            addr = "IP: automaticky (DHCP)"
        else:
            addr = f"IP: {self.ip}/{self.prefix} (maska {self.mask_dotted})\nBrána: {self.gateway or '– žádná –'}"
        dns = "DNS: automaticky" if self.dns_dhcp else "DNS: " + ", ".join(self.dns_list)
        return f"Karta: {self.iface}\n{addr}\n{dns}"


def parse_mask(mask: str) -> int:
    """'255.255.255.0' | '24' | '/24' -> 24. Nespojitá maska => ValueError."""
    m = str(mask).strip().lstrip("/")
    if m.isdigit():
        p = int(m)
        if not 1 <= p <= 32:
            raise ValueError(f"Prefix musí být 1–32: {mask!r}")
        return p
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{m}").prefixlen
    except ValueError:
        raise ValueError(f"Neplatná maska sítě: {mask!r}") from None


def valid_iface_name(name: str) -> bool:
    return bool(name) and len(name) <= 256 and not any(ord(c) < 32 or c in '"`' for c in name)


def sanitize_profiles(items) -> list[dict]:
    out = []
    if not isinstance(items, list):
        return out
    for p in items[:50]:
        if not isinstance(p, dict) or not str(p.get("name", "")).strip():
            continue
        s = IpSettings.from_profile("x", p)
        d = s.to_profile(str(p["name"]).strip()[:48])
        out.append({k: d[k] for k in PROFILE_KEYS})
    return out


# ---------------------------------------------------------------------------
# Windows – PowerShell skript s netsh (spouští se elevovaně přes UAC)
# ---------------------------------------------------------------------------

def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def windows_netsh_args(s: IpSettings) -> list[list[str]]:
    """Seznam volání netsh (každé jako seznam argumentů). Název karty je samostatný argument."""
    cmds: list[list[str]] = []
    base = ["interface", "ipv4"]
    if s.dhcp:
        cmds.append(base + ["set", "address", s.iface, "source=dhcp"])
    else:
        gw = s.gateway.strip() or "none"
        cmds.append(base + ["set", "address", s.iface, "source=static", f"address={s.ip.strip()}",
                            f"mask={s.mask_dotted}", f"gateway={gw}"])
    if s.dns_dhcp:
        cmds.append(base + ["set", "dnsservers", s.iface, "source=dhcp"])
    else:
        dns = s.dns_list
        cmds.append(base + ["set", "dnsservers", s.iface, "source=static", f"address={dns[0]}",
                            "register=primary", "validate=no"])
        for i, d in enumerate(dns[1:], start=2):
            cmds.append(base + ["add", "dnsservers", s.iface, f"address={d}", f"index={i}", "validate=no"])
    return cmds


def windows_script(s: IpSettings, log_path: str) -> str:
    lines = [
        "$ErrorActionPreference = 'Continue'",
        f"$log = {_ps_quote(log_path)}",
        "$fail = 0",
        "function Invoke-Netsh([string[]]$a) {",
        "    $out = [string](& netsh.exe @a 2>&1 | Out-String)",
        "    $code = $LASTEXITCODE",
        "    ('> netsh ' + ($a -join ' ') + \"`r`n\" + $out.Trim() + \"`r`n[exit $code]`r`n\") | "
        "Out-File -FilePath $log -Append -Encoding utf8",
        "    if ($code -ne 0) { $script:fail = $code }",
        "}",
    ]
    for args in windows_netsh_args(s):
        lines.append("Invoke-Netsh @(" + ", ".join(_ps_quote(a) for a in args) + ")")
    lines.append("ipconfig /flushdns | Out-Null")
    lines.append("exit $fail")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Linux – nmcli
# ---------------------------------------------------------------------------

def nmcli_modify_args(s: IpSettings, connection: str) -> list[list[str]]:
    mod = ["nmcli", "connection", "modify", connection]
    if s.dhcp:
        mod += ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", ""]
    else:
        mod += ["ipv4.method", "manual", "ipv4.addresses", f"{s.ip.strip()}/{s.prefix}",
                "ipv4.gateway", s.gateway.strip()]
    if s.dns_dhcp:
        mod += ["ipv4.dns", "", "ipv4.ignore-auto-dns", "no"]
    else:
        mod += ["ipv4.dns", ",".join(s.dns_list), "ipv4.ignore-auto-dns", "yes"]
    return [mod, ["nmcli", "connection", "up", connection]]


# ---------------------------------------------------------------------------
# macOS – networksetup (přes osascript s právy správce)
# ---------------------------------------------------------------------------

def networksetup_args(s: IpSettings, service: str) -> list[list[str]]:
    cmds = []
    if s.dhcp:
        cmds.append(["/usr/sbin/networksetup", "-setdhcp", service])
    else:
        cmds.append(["/usr/sbin/networksetup", "-setmanual", service, s.ip.strip(), s.mask_dotted,
                     s.gateway.strip() or s.ip.strip()])
    cmds.append(["/usr/sbin/networksetup", "-setdnsservers", service] + (["Empty"] if s.dns_dhcp else s.dns_list))
    return cmds


def applescript_admin(cmds: list[list[str]]) -> str:
    import shlex
    shell = " && ".join(" ".join(shlex.quote(a) for a in c) for c in cmds)
    esc = shell.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script "{esc}" with administrator privileges'
