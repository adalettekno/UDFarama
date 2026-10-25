import errno
import logging
import lzma
import os
import re
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


class UdfDocument(NamedTuple):
    path: str
    text: str
    missing_content: int
    bad_archive: int
    permission_error: int
    parse_error: int


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


_TR_FOLD = str.maketrans({"I": "ı", "İ": "i"})


def normalize_search_text(text):
    return unicodedata.normalize("NFC", text).translate(_TR_FOLD).casefold()


LIVE_SEARCH_MIN_CHARS = 4


def should_live_search(term, minimum=LIVE_SEARCH_MIN_CHARS):
    return len((term or "").strip()) >= minimum


def prepare_query(term):
    if "*" not in term:
        return ("literal", normalize_search_text(term))
    parts = [normalize_search_text(part) for part in term.split("*")]
    pattern = ".*?".join(re.escape(part) for part in parts)
    return ("wildcard", re.compile(pattern, re.DOTALL))


def text_matches(text, term):
    return text_matches_query(text, prepare_query(term))


def text_matches_query(text, query):
    normalized = normalize_search_text(text)
    kind, value = query
    if kind == "literal":
        return value in normalized
    return value.search(normalized) is not None


def normalize_with_map(text):
    if not text:
        return "", [], []
    nfc_chars = []
    nfc_origin = []
    buf = []
    buf_start = 0
    index = 0
    while index < len(text):
        buf.append(text[index])
        index += 1
        if index < len(text) and unicodedata.combining(text[index]):
            continue
        for char in unicodedata.normalize("NFC", "".join(buf)):
            nfc_chars.append(char)
            nfc_origin.append(buf_start)
        buf = []
        buf_start = index
    norm_chars = []
    starts = []
    ends = []
    for idx, char in enumerate(nfc_chars):
        folded = char.translate(_TR_FOLD).casefold()
        origin = nfc_origin[idx]
        origin_end = nfc_origin[idx + 1] if idx + 1 < len(nfc_origin) else len(text)
        for out_char in folded:
            norm_chars.append(out_char)
            starts.append(origin)
            ends.append(origin_end)
    return "".join(norm_chars), starts, ends


def find_wildcard_span(text, term):
    parts = [normalize_search_text(part) for part in term.split("*")]
    normalized, starts, ends = normalize_with_map(text)
    if not any(parts):
        return (0, len(text)) if text else None
    match = re.search(".*?".join(re.escape(part) for part in parts), normalized, flags=re.DOTALL)
    if match is None:
        return None
    start, end = match.span()
    if start == end or not starts:
        return (0, 0)
    return starts[start], ends[end - 1]


def find_match_span(text, term):
    if not text or not term:
        return None
    if "*" not in term:
        return find_normalized_span(text, normalize_search_text(term))
    return find_wildcard_span(text, term)


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


def _clip_snippet(text, start, end, max_len):
    if len(text) <= max_len:
        return text
    match_len = end - start
    if match_len >= max_len:
        return text[start:start + max_len - 1] + "…"
    ideal = start - (max_len - match_len) // 2
    ideal = max(0, min(ideal, len(text) - max_len))
    if start < ideal:
        ideal = start
    if end > ideal + max_len:
        ideal = max(0, end - max_len)
    if ideal > 0 and start <= ideal:
        ideal = max(0, start - 1)
    if ideal + max_len < len(text) and end >= ideal + max_len:
        ideal = max(0, min(ideal, end - (max_len - 1)))
        ideal = min(ideal, len(text) - max_len)
    chunk = text[ideal:ideal + max_len]
    if ideal > 0:
        chunk = "…" + chunk[1:]
    if ideal + max_len < len(text):
        chunk = chunk[:-1] + "…"
    return chunk


def _snippet_from_text(text, term, max_len):
    span = find_match_span(text, term)
    if span is None:
        return ""
    start, end = span
    if start == end:
        return ""
    return _clip_snippet(text, start, end, max_len)


def extract_snippet(text, term, max_len=80):
    if not text or not term:
        return ""
    if "*" not in term and not normalize_search_text(term):
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        snippet = _snippet_from_text(stripped, term, max_len)
        if snippet:
            return snippet
    if "\n" not in text:
        return ""
    compact = " ".join(part.strip() for part in text.splitlines() if part.strip())
    return _snippet_from_text(compact, term, max_len)


def format_snippet_markup(snippet, term):
    if not snippet:
        return ""
    span = find_match_span(snippet, term)
    if span is None:
        return _pango_escape(snippet)
    start, end = span
    if start == end:
        return _pango_escape(snippet)
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
        incomplete = outcome.failed or outcome.directory_errors
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
        query = prepare_query(term)
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
                    if not text_matches_query(raw, query):
                        scanned += 1
                        report_progress()
                        continue
                    text = raw
                    try:
                        root = ET.fromstring(raw)
                        text = "\n".join(t for t in root.itertext() if t)
                    except ET.ParseError:
                        parse_errors += 1
                    if text_matches_query(text, query):
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


def _permission_error(exc):
    if isinstance(exc, PermissionError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM)


def read_udf_document(path):
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                data = archive.read("content.xml")
            except KeyError:
                return UdfDocument(path, "", 1, 0, 0, 0)
        raw = data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, zlib.error, lzma.LZMAError) as exc:
        if _permission_error(exc):
            return UdfDocument(path, "", 0, 0, 1, 0)
        return UdfDocument(path, "", 0, 1, 0, 0)
    text = raw
    parse_error = 0
    try:
        root = ET.fromstring(raw)
        text = "\n".join(part for part in root.itertext() if part)
    except ET.ParseError:
        parse_error = 1
    return UdfDocument(path, text, 0, 0, 0, parse_error)


def search_records(
    records,
    folder,
    term,
    cancel_event=None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    directory_errors=0,
):
    matches = []
    missing_content = 0
    bad_archive = 0
    permission_errors = 0
    parse_errors = 0
    failed = False
    cancelled = False
    scanned = 0
    query = prepare_query(term)

    def report_progress():
        if on_progress is not None and (scanned == 1 or scanned % 25 == 0):
            on_progress(scanned, len(matches))

    try:
        for record in records:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            missing_content += record.missing_content
            bad_archive += record.bad_archive
            permission_errors += record.permission_error
            parse_errors += record.parse_error
            readable = not (record.missing_content or record.bad_archive or record.permission_error)
            name_match = text_matches_query(os.path.basename(record.path), query)
            content_match = readable and text_matches_query(record.text, query)
            if name_match or content_match:
                matches.append(make_match_item(
                    folder,
                    record.path,
                    record.text if content_match else "",
                    term if content_match else "",
                ))
            scanned += 1
            report_progress()
    except Exception:
        logging.exception("Hafızadaki UDF araması tamamlanamadı")
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
