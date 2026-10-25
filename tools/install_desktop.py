import os
from pathlib import Path
import shlex
import shutil

root = Path(__file__).resolve().parents[1]
base = Path(os.environ.get("XDG_DATA_HOME", ""))
if not base.is_absolute():
    base = Path.home() / ".local/share"
icons = base / "icons/hicolor/512x512/apps"
assets = base / "udfarama"
applications = base / "applications"
for directory in (icons, assets, applications):
    directory.mkdir(parents=True, exist_ok=True)

icon_name = "com.adalettekno.UDFarama"
shutil.copyfile(
    root / "data/app-icons/hicolor/512x512/apps/udfarama.png",
    icons / f"{icon_name}.png",
)
shutil.copyfile(root / "data/app-icons/udfarama.ico", assets / "udfarama.ico")

launcher = assets / "launch"
launcher.write_text(
    "#!/bin/sh\nexec /usr/bin/env PYTHONPATH="
    + shlex.quote(str(root / "src"))
    + " PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m udfarama\n"
)
launcher.chmod(0o755)

command = (
    str(launcher)
    .replace("\\", "\\\\")
    .replace('"', '\\"')
    .replace("`", "\\`")
    .replace("$", "\\$")
    .replace("%", "%%")
)
desktop = applications / f"{icon_name}.desktop"
desktop.write_text(
    "[Desktop Entry]\n"
    "Type=Application\n"
    "Name=UDFarama\n"
    "Comment=UYAP Doküman Editörü dosyalarında içerik arama programı\n"
    f'Exec="{command}"\n'
    f"Icon={icon_name}\n"
    "Terminal=false\n"
    "Categories=Office;Utility;\n"
    "StartupNotify=true\n"
    f"StartupWMClass={icon_name}\n"
)
print("Menü kısayolu:", desktop)
print("PNG simgesi:", icons / f"{icon_name}.png")
print("ICO kaynağı:", assets / "udfarama.ico")
