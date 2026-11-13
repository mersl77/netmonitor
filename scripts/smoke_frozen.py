"""Launch the packaged Windows app with isolated data before releasing it."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

exe = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix="netmonitor-smoke-") as tmp:
    image = Path(tmp) / "widget.png"
    env = dict(os.environ, NETMONITOR_HOME=tmp, QT_QPA_PLATFORM="offscreen")
    subprocess.run([str(exe), "--screenshot", str(image)], env=env,
                   check=True, timeout=45)
    if not image.is_file() or image.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("Packaged app did not render a PNG")
print("Packaged app started and rendered successfully")
