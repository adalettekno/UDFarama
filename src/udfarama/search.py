import errno
import logging
import lzma
import os
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET
from typing import Callable, NamedTuple, Optional


class MatchItem(NamedTuple):
    path: str
    relative_path: str
    basename: str
    snippet: str = ""


class SearchOutcome(NamedTuple):
    matches: list
    missing_content: int
    bad_archive: int
    permission_errors: int
    parse_errors: int
    directory_errors: int
    failed: bool
    cancelled: bool

    @property
    def file_failures(self):
        return self.missing_content + self.bad_archive + self.permission_errors


def normalize_search_text(text):
    return unicodedata.normalize("NFC", text).translate(str.maketrans({"I": "ı", "İ": "i"})).casefold()


def extract_snippet(text, term, max_len=80):
    needle = normalize_search_text(term)
    if not needle:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if needle in normalize_search_text(stripped):
            if len(stripped) <= max_len:
                return stripped
            return stripped[: max_len - 1] + "…"
    return ""


def make_match_item(folder, path, text="", term=""):
    snippet = extract_snippet(text, term) if text and term else ""
    return MatchItem(
        path=path,
        relative_path=os.path.relpath(path, folder),
        basename=os.path.basename(path),
        snippet=snippet,
    )


def format_search_status(outcome: SearchOutcome):
    matches = outcome.matches
    if outcome.cancelled:
        message = f"Arama durduruldu: {len(matches)} belge bulundu."
    else:
        incomplete = outcome.failed or outcome.file_failures or outcome.directory_errors
        message = (
            f"Arama kısmen tamamlandı: {len(matches)} belge bulundu."
            if incomplete else f"Arama tamamlandı: {len(matches)} belge bulundu."
        )
    if outcome.missing_content:
        message += f" {outcome.missing_content} dosyada content.xml yok."
    if outcome.bad_archive:
        message += f" {outcome.bad_archive} dosya bozuk veya açılamadı."
    if outcome.permission_errors:
        message += f" {outcome.permission_errors} dosyaya izin yok."
    if outcome.parse_errors:
        message += f" {outcome.parse_errors} dosyada XML çözümlenemedi; düz metin arandı."
    if outcome.directory_errors:
        message += f" {outcome.directory_errors} klasör taranamadı."
    if outcome.failed:
        message += " Beklenmeyen bir hata nedeniyle tarama durdu; yeniden deneyin."
    return message


def search_udf_folder(
    folder,
    term,
    cancel_event=None,
    on_progress: Optional[Callable[[int, int], None]] = None,
):
    matches = []
    missing_content = 0
    bad_archive = 0
    permission_errors = 0
    parse_errors = 0
    directory_errors = 0
    failed = False
    cancelled = False
    scanned = 0

    def on_walk_error(error):
        nonlocal directory_errors
        directory_errors += 1

    def report_progress():
        if on_progress is not None and (scanned == 1 or scanned % 5 == 0):
            on_progress(scanned, len(matches))

    def count_read_failure(exc):
        nonlocal bad_archive, permission_errors
        if isinstance(exc, PermissionError):
            permission_errors += 1
        elif isinstance(exc, OSError) and getattr(exc, "errno", None) in (
            errno.EACCES, errno.EPERM,
        ):
            permission_errors += 1
        else:
            bad_archive += 1

    try:
        lowered = normalize_search_text(term)
        for base, dirs, files in os.walk(folder, followlinks=False, onerror=on_walk_error):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
            for name in files:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                if not name.lower().endswith(".udf"):
                    continue
                path = os.path.join(base, name)
                try:
                    with zipfile.ZipFile(path) as archive:
                        try:
                            data = archive.read("content.xml")
                        except KeyError:
                            missing_content += 1
                            scanned += 1
                            report_progress()
                            continue
                    text = data.decode("utf-8", errors="replace")
                    try:
                        root = ET.fromstring(text)
                        text = "\n".join(t for t in root.itertext() if t)
                    except ET.ParseError:
                        parse_errors += 1
                    if lowered in normalize_search_text(text):
                        matches.append(make_match_item(folder, path, text, term))
                except (OSError, zipfile.BadZipFile, RuntimeError,
                        EOFError, zlib.error, lzma.LZMAError) as exc:
                    count_read_failure(exc)
                scanned += 1
                report_progress()
            if cancelled:
                break
    except Exception:
        logging.exception("UDF araması tamamlanamadı")
        failed = True
    return SearchOutcome(
        matches,
        missing_content,
        bad_archive,
        permission_errors,
        parse_errors,
        directory_errors,
        failed,
        cancelled,
    )
