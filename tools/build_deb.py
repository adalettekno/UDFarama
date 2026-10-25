import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ICON_NAME = "com.adalettekno.UDFarama"
APP_LIB = "/usr/lib/udfarama"


def project_version():
    text = (ROOT / "src/udfarama/__init__.py").read_text()
    match = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', text, re.M)
    if match is None:
        raise ValueError("Uygulama sürümü bulunamadı.")
    return match.group(1)


def build(version=None):
    version = project_version() if version is None else version
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Sürüm numarası 1.0.0 biçiminde olmalıdır.")
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    output = ROOT / "dist" / arch
    output.mkdir(parents=True, exist_ok=True)
    target = output / "UDFarama.deb"
    with tempfile.TemporaryDirectory(prefix="udfarama-deb-") as folder:
        stage = Path(folder)
        app = stage / "usr/lib/udfarama"
        app.mkdir(parents=True)
        shutil.copytree(
            ROOT / "src/udfarama",
            app / "src/udfarama",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        icon_src = ROOT / "data/app-icons/hicolor/scalable/apps" / f"{ICON_NAME}.svg"
        icon = stage / "usr/share/icons/hicolor/scalable/apps" / f"{ICON_NAME}.svg"
        icon.parent.mkdir(parents=True)
        shutil.copyfile(icon_src, icon)
        metadata = stage / "usr/share/metainfo" / f"{ICON_NAME}.metainfo.xml"
        metadata.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "packaging" / f"{ICON_NAME}.metainfo.xml", metadata)
        launcher = (
            "#!/usr/bin/python3 -I\n"
            "import os\n"
            "import sys\n"
            f'os.environ["PYTHONPATH"] = "{APP_LIB}/src"\n'
            'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"\n'
            "sys.dont_write_bytecode = True\n"
            f'sys.path.insert(0, "{APP_LIB}/src")\n'
            "from udfarama.__main__ import main\n"
            "raise SystemExit(main())\n"
        )
        desktop = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=UDFarama\n"
            "Comment=UYAP Doküman Editörü dosyalarında içerik arama programı\n"
            "Exec=udfarama\n"
            f"Icon={ICON_NAME}\n"
            "Terminal=false\n"
            "Categories=Office;Utility;\n"
            "StartupNotify=true\n"
            f"StartupWMClass={ICON_NAME}\n"
        )
        files = {
            "usr/bin/udfarama": launcher,
            f"usr/share/applications/{ICON_NAME}.desktop": desktop,
        }
        for name, content in files.items():
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        (stage / "usr/bin/udfarama").chmod(0o755)
        control = stage / "DEBIAN"
        control.mkdir()
        size = sum(p.stat().st_size for p in (stage / "usr").rglob("*") if p.is_file())
        (control / "control").write_text(
            f"Package: udfarama\nVersion: {version}\nArchitecture: {arch}\n"
            "Maintainer: Adalet Tekno\nHomepage: https://www.adalettekno.com/\n"
            "Section: utils\nPriority: optional\n"
            f"Installed-Size: {(size + 1023) // 1024}\n"
            "Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1\n"
            "Description: UYAP UDF belgelerinde hızlı içerik araması\n"
            " UDFarama, UYAP Doküman Editörü .udf dosyalarının\n"
            " içeriğinde klasör ve alt klasörler dahil arama yapar.\n"
        )
        checksums = []
        for path in sorted((stage / "usr").rglob("*")):
            if path.is_file():
                checksums.append(
                    hashlib.md5(path.read_bytes()).hexdigest()
                    + "  "
                    + str(path.relative_to(stage))
                )
        (control / "md5sums").write_text("\n".join(checksums) + "\n")
        for path in stage.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
            elif not path.stat().st_mode & 0o111:
                path.chmod(0o644)
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "--build", str(stage), str(target)],
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": "1792108800"},
        )
    print(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=None)
    build(parser.parse_args().version)
