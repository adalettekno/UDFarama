import os
from pathlib import Path
import sys


APP_ID = "com.adalettekno.UDFarama"
SETTING_NAME = "start-at-login"


def config_directory():
    override = os.environ.get("UDFARAMA_CONFIG_DIR")
    if override:
        return Path(override)
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / APP_ID


def autostart_directory():
    override = os.environ.get("UDFARAMA_AUTOSTART_DIR")
    if override:
        return Path(override)
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "autostart"


def read_start_at_login(directory, default=True):
    path = Path(directory) / SETTING_NAME
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if value == "1":
        return True
    if value == "0":
        return False
    return default


def write_start_at_login(directory, enabled):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SETTING_NAME).write_text(("1" if enabled else "0") + "\n", encoding="utf-8")


def quote_exec_arg(value):
    if any(char in value for char in ' \t"\\\n'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def background_command():
    argv0 = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if argv0 and argv0.is_file() and argv0.name == "udfarama":
        return quote_exec_arg(str(argv0)) + " --background"
    source = Path(__file__).resolve().parents[1]
    python = sys.executable or "/usr/bin/python3"
    return (
        "/usr/bin/env "
        + quote_exec_arg("PYTHONPATH=" + str(source))
        + " PYTHONDONTWRITEBYTECODE=1 "
        + quote_exec_arg(python)
        + " -m udfarama --background"
    )


def desktop_contents(exec_line):
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=UDFarama\n"
        "Comment=UYAP Doküman Editörü dosyalarında içerik arama programı\n"
        f"Exec={exec_line}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "Categories=Office;Utility;\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def apply_start_at_login(autostart_dir, enabled, exec_line):
    path = Path(autostart_dir) / f"{APP_ID}.desktop"
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desktop_contents(exec_line), encoding="utf-8")
    return path


def sync_start_at_login(config_dir=None, autostart_dir=None, exec_line=None):
    config_dir = Path(config_dir) if config_dir is not None else config_directory()
    autostart_dir = Path(autostart_dir) if autostart_dir is not None else autostart_directory()
    enabled = read_start_at_login(config_dir, default=True)
    if not (config_dir / SETTING_NAME).exists():
        write_start_at_login(config_dir, enabled)
    apply_start_at_login(autostart_dir, enabled, exec_line or background_command())
    return enabled


def set_start_at_login(enabled, config_dir=None, autostart_dir=None, exec_line=None):
    config_dir = Path(config_dir) if config_dir is not None else config_directory()
    autostart_dir = Path(autostart_dir) if autostart_dir is not None else autostart_directory()
    write_start_at_login(config_dir, enabled)
    apply_start_at_login(autostart_dir, enabled, exec_line or background_command())
    return enabled
