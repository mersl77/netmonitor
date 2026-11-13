import hashlib
import io

import pytest

from netmonitor.core import updater as u

GH = {
    "tag_name": "v1.2.0", "html_url": "https://github.com/mersl77/netmonitor/releases/tag/v1.2.0", "body": "## Novinky",
    "assets": [
        {"name": "NetMonitor-1.2.0-win64.zip", "browser_download_url": "https://x/zip", "digest": "sha256:" + "a" * 64},
        {"name": "NetMonitor-1.2.0-setup.exe", "size": 123,
         "browser_download_url": "https://github.com/mersl77/netmonitor/releases/download/v1.2.0/NetMonitor-1.2.0-setup.exe",
         "digest": "sha256:" + "b" * 64},
    ],
}


@pytest.mark.parametrize("src", ["https://github.com/mersl77/netmonitor/releases",
                                 "https://github.com/mersl77/netmonitor/releases/tag/",
                                 "https://github.com/mersl77/netmonitor",
                                 "https://github.com/mersl77/netmonitor.git"])
def test_github_source_normalized(src):
    assert u.api_url_for(src) == ("github", "https://api.github.com/repos/mersl77/netmonitor/releases/latest")


def test_manifest_and_invalid_sources():
    assert u.api_url_for("https://zelina.eu/netmonitor/latest.json") == ("manifest",
                                                                          "https://zelina.eu/netmonitor/latest.json")
    with pytest.raises(u.UpdateError):
        u.api_url_for("http://zelina.eu/x.json")
    with pytest.raises(u.UpdateError):
        u.api_url_for("")


def test_parse_github_picks_setup_and_digest():
    i = u.parse_github(GH, "win32")
    assert (i.version, i.asset_name, i.sha256, i.size) == ("1.2.0", "NetMonitor-1.2.0-setup.exe", "b" * 64, 123)
    assert i.can_auto_install
    lin = u.parse_github(GH, "linux")
    assert lin.version == "1.2.0" and not lin.can_auto_install and lin.page_url.endswith("v1.2.0")
    no_digest = dict(GH, assets=[dict(GH["assets"][1], digest=None)])
    assert not u.parse_github(no_digest, "win32").can_auto_install      # bez otisku nic neinstalujeme
    with pytest.raises(u.UpdateError):
        u.parse_github({"message": "Not Found"}, "win32")


def test_parse_manifest():
    m = {"version": "1.3.0", "url": "https://zelina.eu/projekty.html", "notes": "x",
         "windows": {"url": "https://zelina.eu/dl/NetMonitor-1.3.0-setup.exe", "sha256": "C" * 64}}
    i = u.parse_manifest(m, "win32")
    assert i.version == "1.3.0" and i.asset_name == "NetMonitor-1.3.0-setup.exe" and i.sha256 == "c" * 64


def test_check_uses_fetcher():
    seen = []

    def fetch(url):
        seen.append(url)
        return GH
    i = u.check("https://github.com/mersl77/netmonitor/releases", fetch, "win32")
    assert i.version == "1.2.0" and seen == ["https://api.github.com/repos/mersl77/netmonitor/releases/latest"]


def test_version_compare():
    assert u.is_newer("1.2.0", "1.1.0")
    assert u.is_newer("v1.10.0", "1.9.9")
    assert not u.is_newer("1.1.0", "1.1.0")
    assert not u.is_newer("1.0.9", "1.1.0")


class FakeResp(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _info(data, sha=None, name="NetMonitor-1.2.0-setup.exe"):
    return u.UpdateInfo(version="1.2.0", page_url="", asset_name=name, asset_url="https://x/" + name,
                        sha256=sha or hashlib.sha256(data).hexdigest(), size=len(data))


def test_download_verifies_sha256(tmp_path):
    data = b"MZ" + b"x" * 700_000
    prog = []
    p = u.download(_info(data), tmp_path, progress=lambda d, t: prog.append((d, t)), opener=lambda r, timeout: FakeResp(data))
    assert p.read_bytes() == data and prog[-1] == (len(data), len(data))


def test_download_rejects_tampered_file(tmp_path):
    data = b"MZ evil"
    with pytest.raises(u.UpdateError, match="SHA-256"):
        u.download(_info(data, sha="0" * 64), tmp_path, opener=lambda r, timeout: FakeResp(data))
    assert list(tmp_path.iterdir()) == []                     # nic nezůstalo


def test_download_rejects_unexpected_name_and_cancel(tmp_path):
    with pytest.raises(u.UpdateError):
        u.download(_info(b"x", name="evil.exe"), tmp_path, opener=lambda r, timeout: FakeResp(b"x"))
    with pytest.raises(u.UpdateError, match="zrušeno"):
        u.download(_info(b"abc"), tmp_path, cancelled=lambda: True, opener=lambda r, timeout: FakeResp(b"abc"))


def test_installer_args_silent():
    a = u.installer_args(u.Path("C:/x/NetMonitor-1.2.0-setup.exe"))
    assert "/VERYSILENT" in a and "/SUPPRESSMSGBOXES" in a and "/CLOSEAPPLICATIONS" in a


def test_config_migration_v3():
    from netmonitor.core.config import AppConfig
    old = AppConfig.from_dict({"schema_version": 2, "appearance": {"glass_alpha": 0.75},
                               "general": {"update_url": "https://github.com/mersl77/netmonitor/releases/tag/",
                                           "check_updates": False}})
    assert old.appearance.glass_alpha == 1.0
    assert old.general.update_url == u.DEFAULT_SOURCE and old.general.check_updates is True
    custom = AppConfig.from_dict({"schema_version": 2, "appearance": {"glass_alpha": 0.3},
                                  "general": {"update_url": "https://zelina.eu/m.json"}})
    assert custom.appearance.glass_alpha == 0.3 and custom.general.update_url == "https://zelina.eu/m.json"


def test_beta_update_channel_is_opt_in():
    from netmonitor.core.config import AppConfig
    assert AppConfig.from_dict({}).general.receive_beta_updates is False
    cfg = AppConfig.from_dict({"general": {"receive_beta_updates": True}})
    assert cfg.general.receive_beta_updates is True


def test_beta_uses_releases_list_and_orders_versions_correctly():
    stable = dict(GH, tag_name="v1.3.4", draft=False, prerelease=False)
    beta = dict(GH, tag_name="v1.4.0b2", draft=False, prerelease=True)
    old = dict(GH, tag_name="v1.4.0b1", draft=False, prerelease=True)
    draft = dict(GH, tag_name="v1.9.0", draft=True, prerelease=True)
    seen = []

    def fetch(url):
        seen.append(url)
        return [stable, beta, old, draft] if "?per_page=" in url else stable

    src = "https://github.com/mersl77/netmonitor/releases"
    assert u.check(src, fetch, "win32").version == "1.3.4"
    assert u.check(src, fetch, "win32", allow_beta=True).version == "1.4.0b2"
    assert seen[-1].endswith("/releases?per_page=30")
    assert u.is_newer("1.4.0b2", "1.4.0b1")
    assert u.is_newer("1.4.0", "1.4.0b2")
    assert not u.is_newer("1.4.0b1", "1.4.0")
    assert not u.is_newer("1.4.0b1", "1.4.0b2")


def test_beta_channel_handles_no_releases():
    with pytest.raises(u.UpdateError, match="žádné"):
        u.check("https://github.com/mersl77/netmonitor/releases",
                lambda url: [], "win32", allow_beta=True)
