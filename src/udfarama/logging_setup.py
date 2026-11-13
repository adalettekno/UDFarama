import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level=logging.WARNING):
    root = logging.getLogger()
    root.setLevel(level)
    if root.handlers:
        return
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(stream)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local/state"
    directory = base / "com.adalettekno.UDFarama"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        file = directory / "udfarama.log"
        handler = RotatingFileHandler(file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file.chmod(0o600)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    except OSError:
        root.warning("Günlük dosyası oluşturulamadı")
