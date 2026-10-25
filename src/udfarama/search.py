import errno
import logging
import lzma
import os
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET
from typing import Callable, NamedTuple, Optional, Tuple


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


def _pango_escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("'", "&apos;")
        .replace('"', "&quot;")
    )


def find_normalized_span(text, needle):
    if not text or not needle:
        return None
    needle_len = len(needle)
    n = len(text)
    for i in range(n):
        for j in range(i + 1, n + 1):
            cand = normalize_search_text(text[i:j])
            if cand == needle:
                return i, j
            if len(cand) >= needle_len:
                break
    return None


def extract_snippet(text, term, max_len=80):
    needle = normalize_search_text(term)
    if not needle:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        span = find_normalized_span(stripped, needle)
        if span is None:
            continue
        start, end = span
        if len(stripped) <= max_len:
            return stripped
        match_len = end - start
        if match_len >= max_len:
            return stripped[start:start + max_len - 1] + "…"
        ideal = start - (max_len - match_len) // 2
        ideal = max(0, min(ideal, len(stripped) - max_len))
        if start < ideal:
            ideal = start
        if end > ideal + max_len:
            ideal = max(0, end - max_len)
        if ideal > 0 and start <= ideal:
            ideal = max(0, start - 1)
        if ideal + max_len < len(stripped) and end >= ideal + max_len:
            ideal = max(0, min(ideal, end - (max_len - 1)))
            ideal = min(ideal, len(stripped) - max_len)
        chunk = stripped[ideal:ideal + max_len]
        if ideal > 0:
            chunk = "…" + chunk[1:]
        if ideal + max_len < len(stripped):
            chunk = chunk[:-1] + "…"
        return chunk
    return ""


def format_snippet_markup(snippet, term):
    if not snippet:
        return ""
    needle = normalize_search_text(term)
    if not needle:
        return _pango_escape(snippet)
    span = find_normalized_span(snippet, needle)
    if span is None:
        return _pango_escape(snippet)
    start, end = span
    return (
        _pango_escape(snippet[:start])
        + "<b>"
        + _pango_escape(snippet[start:end])
        + "</b>"
        + _pango_escape(snippet[end:])
    )


def make_match_item(folder, path, text="", term=""):
    snippet = extract_snippet(text, term) if text and term else ""
    return MatchItem(
        path=path,
        relative_path=os.path.relpath(path, folder),
        basename=os.path.basename(path),
        snippet=snippet,
    )


def format_search_status_parts(outcome: SearchOutcome) -> Tuple[str, str]:
    n = len(outcome.matches)
    if outcome.cancelled:
        primary = f"Arama durduruldu: {n} sonuç."
    else:
        incomplete = outcome.failed or outcome.file_failures or outcome.directory_errors
        primary = (
            f"Arama kısmen tamamlandı: {n} sonuç."
            if incomplete else f"Arama tamamlandı: {n} sonuç."
        )
    details = []
    if outcome.missing_content:
        details.append(f"{outcome.missing_content} dosyada content.xml yok.")
    if outcome.bad_archive:
        details.append(f"{outcome.bad_archive} dosya bozuk veya açılamadı.")
    if outcome.permission_errors:
        details.append(f"{outcome.permission_errors} dosyaya izin yok.")
    if outcome.parse_errors:
        details.append(f"{outcome.parse_errors} dosyada XML çözümlenemedi; düz metin arandı.")
    if outcome.directory_errors:
        details.append(f"{outcome.directory_errors} klasör taranamadı.")
    if outcome.failed:
        details.append("Beklenmeyen bir hata nedeniyle tarama durdu; yeniden deneyin.")
    return primary, " ".join(details)


def format_search_status(outcome: SearchOutcome):
    primary, detail = format_search_status_parts(outcome)
    if detail:
        return f"{primary} {detail}"
    return primary


def should_apply_search_generation(current_generation, result_generation):
    return current_generation == result_generation


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
        if on_progress is not None and (scanned == 1 or scanned % 25 == 0):
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
                    raw = data.decode("utf-8", errors="replace")
                    if lowered not in normalize_search_text(raw):
                        scanned += 1
                        report_progress()
                        continue
                    text = raw
                    try:
                        root = ET.fromstring(raw)
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
