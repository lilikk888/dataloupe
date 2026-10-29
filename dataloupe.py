#!/usr/bin/env python3
"""dataloupe — see through any data file.

Zero dependencies. Single file. One command.

Reads CSV / TSV / JSONL / JSON / XLSX, figures out the encoding, the delimiter,
what each column actually contains, and what is wrong with it. Prints a terminal
summary or writes a standalone HTML report.

    python dataloupe.py data.csv
    python dataloupe.py data.csv -o report.html
"""

from __future__ import annotations

import argparse
import codecs
import csv
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
import zipfile
from array import array
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator
from xml.etree import ElementTree as ET

__version__ = "1.4.0"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Values treated as "no data". Compared case-insensitively after stripping.
MISSING_TOKENS = {
    "", "na", "n/a", "n.a.", "null", "none", "nil", "nan", "nil",
    "-", "--", "?", "??", "unknown", "n/a", "\\n",
}

#: How many non-empty values to look at when guessing a column's type.
TYPE_SAMPLE = 2000

#: A column is typed T when at least this share of sampled values parse as T.
TYPE_TOLERANCE = 0.95

#: Upper bound on values kept per column for quantile / histogram work.
MAX_VALUES = 200_000

#: Rows embedded in the HTML preview.
PREVIEW_ROWS = 100

#: Histogram bucket count for numeric columns.
HIST_BUCKETS = 24

#: Hygiene checks (stray spaces, full-width chars, control chars) run on a
#: sample only. Scanning every cell of a huge file costs seconds and changes
#: nothing: these are rare-character detections, not exact counts.
HYGIENE_SAMPLE = 5000

#: Values kept per numeric column for pairwise correlation.
CORRELATION_SAMPLE = 5000

#: Correlation is O(n^2) over columns; cap how many numeric columns we compare.
MAX_CORRELATION_COLUMNS = 40

#: |r| below this is not worth reporting.
CORRELATION_REPORT_THRESHOLD = 0.5

#: |r| above this means the two columns carry essentially the same information.
CORRELATION_REDUNDANT_THRESHOLD = 0.98

#: Upper bound on distinct values tracked per column. Past this the count is
#: reported as "at least N" rather than scanning a 10-million-key set.
DISTINCT_CAP = 100_000

#: Upper bound on rows remembered when hunting for duplicate rows.
DEDUP_CAP = 200_000

#: Raw text values kept per column to hunt for sensitive patterns (emails,
#: phone numbers, ID cards...). Bounded, so it costs the same on a 10-row file
#: as on a 10-million-row one.
PII_SAMPLE_CAP = 200

#: Upper bound on distinct row fingerprints held per side when comparing the
#: rows of two exports. Two sets of 500k 64-bit keys cost roughly 60 MB; past
#: that the answer is reported as approximate instead of eating the machine.
ROW_DIFF_CAP = 500_000

#: A numeric column is called "shifted" when its mean moved by at least this
#: many of the *old* standard deviations. One sigma is unambiguous movement.
DISTRIBUTION_SHIFT_EFFECT = 1.0

#: ... or when the mean moved by at least this share of its old value, which
#: catches large relative moves in columns whose spread dwarfs their mean.
DISTRIBUTION_SHIFT_RELATIVE = 0.5

#: Column samples are drawn with a fixed seed so two runs on the same file
#: always print the same numbers.
RESERVOIR_SEED = 0x0DA7A10

#: How each inferred column type maps onto a SQL type, per dialect.
#: SQLite has no date or boolean storage class, so those become TEXT/INTEGER.
SQL_TYPES = {
    "postgres": {
        "int": "BIGINT", "float": "DOUBLE PRECISION", "bool": "BOOLEAN",
        "date": "DATE", "datetime": "TIMESTAMP", "text": "TEXT", "empty": "TEXT",
    },
    "mysql": {
        "int": "BIGINT", "float": "DOUBLE", "bool": "TINYINT(1)",
        "date": "DATE", "datetime": "DATETIME", "text": "TEXT", "empty": "TEXT",
    },
    "sqlite": {
        "int": "INTEGER", "float": "REAL", "bool": "INTEGER",
        "date": "TEXT", "datetime": "TEXT", "text": "TEXT", "empty": "TEXT",
    },
}

#: Words that have to be quoted to work as column names.
SQL_RESERVED = {
    "all", "and", "as", "asc", "between", "by", "case", "cast", "check",
    "column", "constraint", "create", "cross", "current_date", "current_time",
    "default", "delete", "desc", "distinct", "drop", "else", "end", "exists",
    "false", "from", "full", "group", "having", "in", "index", "inner",
    "insert", "into", "is", "join", "key", "left", "like", "limit", "not",
    "null", "offset", "on", "or", "order", "outer", "primary", "references",
    "right", "select", "set", "table", "then", "true", "union", "unique",
    "update", "user", "using", "values", "when", "where", "with",
}

INT_RE = re.compile(r"^[+-]?\d{1,20}$")
FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

#: Unambiguous year-first date shapes. Handled without strptime.
ISO_LIKE_RE = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)

#: Explicit date shapes only — no bare "20240101" (ambiguous with IDs).
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y年%m月%d日",
    "%Y-%m",
)

BOOL_TOKENS = {"true", "false", "yes", "no", "t", "f"}

#: Excel's epoch. Serial 1 == 1900-01-01 in its (slightly broken) calendar.
XLSX_EPOCH = datetime(1899, 12, 30)
XLSX_DATE_FMT_IDS = set(range(14, 23)) | {45, 46, 47}
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """Terminal columns occupied by *text*.

    CJK characters render two cells wide; combining marks render zero. Using
    ``len()`` here is what makes most CLI tables look ragged in Chinese.
    """
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad *text* to *width* display columns."""
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def truncate(text: str, width: int) -> str:
    """Cut *text* to *width* display columns, adding an ellipsis if needed."""
    if display_width(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


ENCODING_LABELS = {
    "utf-8-sig": "UTF-8 (BOM)",
    "utf-8": "UTF-8",
    "utf-16": "UTF-16",
    "gb18030": "GB18030",
    "gbk": "GBK",
    "big5": "Big5",
    "cp1252": "CP1252",
    "latin-1": "Latin-1",
}


def encoding_label(encoding: str) -> str:
    return ENCODING_LABELS.get(encoding.lower(), encoding.upper())


def is_missing(value: str) -> bool:
    """True for empty strings and the usual null markers.

    The length check is a fast path: any value longer than the longest marker
    cannot be one, and this runs once per cell.
    """
    text = value.strip()
    if not text:
        return True
    if len(text) > 8:
        return False
    return text.lower() in MISSING_TOKENS


def looks_like_formula(text: str) -> bool:
    """True if a spreadsheet would try to execute this cell.

    Excel, LibreOffice and Google Sheets all treat a leading ``=`` or ``@`` as
    the start of a formula, and ``+`` / ``-`` as a legacy way to write one. A
    CSV exported from a web form and then opened in Excel can therefore run
    ``=cmd|'/C calc'!A0`` on the reader's machine — CSV injection.

    Plain numbers start with ``+`` or ``-`` too, so those are excluded: the
    test is "starts like a formula and is not a number".
    """
    head = text[:1]
    if not head:
        return False
    if head in ("=", "@"):
        return True
    if head in ("+", "-"):
        return _to_number(text) is None
    return False


#: Sensitive-pattern checks. Named once here so detection and masking can never
#: disagree about what counts as an email address.
PII_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PII_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$|^\d{15}$")
PII_BANK_CARD_RE = re.compile(r"^(?:\d[ -]?){13,19}$")
PII_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
PII_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
PII_URL_RE = re.compile(r"^(?:https?://|www\.)", re.IGNORECASE)

#: Each entry is (label, matcher); the label is what gets reported to the user.
PII_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("邮箱", PII_EMAIL_RE),
    ("身份证号", PII_ID_CARD_RE),
    ("银行卡号", PII_BANK_CARD_RE),
    ("IPv4 地址", PII_IPV4_RE),
    ("手机号", PII_PHONE_RE),
    ("URL", PII_URL_RE),
]


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — the one real test that separates a card number from a
    random 16-digit string, so it keeps false positives down."""
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def detect_pii(sample: list[str]) -> dict[str, float]:
    """Return PII labels mapped to the share of sampled cells that match.

    Only ``sample`` (at most PII_SAMPLE_CAP raw values) is tested, so the cost
    is flat regardless of file size. A label is reported only when it clears
    a high bar (80% of the sample), because a stray email in a name column is
    not the same as a column that *is* email addresses.
    """
    if not sample:
        return {}
    counts: dict[str, int] = {}
    for cell in sample:
        compact = re.sub(r"[ -]", "", cell) if any(c in cell for c in " -") else cell
        matched_id = False
        for label, pattern in PII_PATTERNS:
            if label == "银行卡号":
                # Already proven to be an ID card (or similar 18-digit string):
                # a 1-in-10 coincidence passes Luhn, so don't double-flag it.
                if matched_id or not compact.isdigit() or not (13 <= len(compact) <= 19):
                    continue
                if _luhn_ok(compact):
                    counts[label] = counts.get(label, 0) + 1
            elif pattern.match(cell):
                if label == "身份证号":
                    matched_id = True
                counts[label] = counts.get(label, 0) + 1
    n = len(sample)
    scored = {label: count / n for label, count in counts.items()}
    return {label: share for label, share in scored.items() if share >= 0.8}


def _mask_url(text: str) -> str:
    """Keep the scheme and host, blank out the path and query.

    Tokens, session ids and tracking parameters live in the path — the host is
    the part a reader still needs in order to understand the column.
    """
    match = re.match(r"^(https?://|www\.)?([^/?#]+)(.*)$", text, re.IGNORECASE)
    if not match:
        return "***"
    return f"{match.group(1) or ''}{match.group(2)}/***"


def mask_cell(text: str, *, allow_bank_card: bool = False) -> tuple[str, str] | None:
    """Return ``(masked, label)`` when *text* looks sensitive, else ``None``.

    Masking keeps the value recognisable for debugging — the last four digits of
    a phone, the domain of an email — while destroying the part that identifies
    a person. Every rule is a shape test, never a guess.

    ``allow_bank_card`` exists because the bank-card shape (13-19 digits) also
    matches order numbers and other long identifiers. A bare run of digits is
    only treated as a card when the column itself was already flagged, or when
    the value is written with separators (``4111 1111 1111 1111``).
    """
    cell = text.strip()
    if not cell:
        return None

    # ID card first: an 18-digit ID card also satisfies the bank-card shape.
    if PII_ID_CARD_RE.match(cell):
        if len(cell) >= 10:
            return cell[:6] + "*" * (len(cell) - 10) + cell[-4:], "身份证号"
        return "*" * len(cell), "身份证号"

    compact = cell.replace(" ", "").replace("-", "")
    if (
        compact.isdigit()
        and 13 <= len(compact) <= 19
        and _luhn_ok(compact)
        and (allow_bank_card or " " in cell or "-" in cell)
    ):
        return "*" * (len(compact) - 4) + compact[-4:], "银行卡号"

    if PII_EMAIL_RE.match(cell):
        local, _, domain = cell.partition("@")
        return f"{local[:1]}***@{domain}", "邮箱"

    if PII_PHONE_RE.match(cell):
        return cell[:3] + "****" + cell[-4:], "手机号"

    if PII_IPV4_RE.match(cell):
        parts = cell.split(".")
        return f"{parts[0]}.{parts[1]}.*.*", "IPv4 地址"

    if PII_URL_RE.match(cell):
        return _mask_url(cell), "URL"

    return None


@dataclass
class MaskReport:
    """What the masking pass redacted."""
    output: Path
    cells_masked: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    rows_written: int = 0
    cols_written: int = 0


def mask_table(
    header: list[str],
    rows: list[list[str]],
    flagged: dict[str, set[str]] | None = None,
    *,
    allow_all_cards: bool = False,
) -> tuple[list[str], list[list[str]], MaskReport]:
    """Return a copy of the table with sensitive cells redacted.

    ``flagged`` maps a column name to the PII labels detection found in it.
    It is only consulted for bank cards, where a bare 16-digit run is too
    ambiguous to redact without column-level evidence.
    """
    report = MaskReport(output=Path())
    flagged = flagged or {}
    masked_rows: list[list[str]] = []

    for row in rows:
        new_row: list[str] = []
        for idx, cell in enumerate(row):
            name = header[idx] if idx < len(header) else ""
            allow_card = allow_all_cards or "银行卡号" in flagged.get(name, set())
            hit = mask_cell(cell, allow_bank_card=allow_card)
            if hit is None:
                new_row.append(cell)
                continue
            masked, label = hit
            new_row.append(masked)
            report.cells_masked += 1
            report.by_label[label] = report.by_label.get(label, 0) + 1
        masked_rows.append(new_row)

    report.rows_written = len(masked_rows)
    report.cols_written = len(header)
    return header, masked_rows, report


def render_mask_report(report: MaskReport) -> str:
    """Human summary of a masking run."""
    lines = [""]
    lines.append("  " + bold("dataloupe") + dim(f"  v{__version__}") + "  " + bold("脱敏完成"))
    lines.append("")
    lines.append(f"  输出  {report.output}")
    lines.append(f"  规模  {report.rows_written:,} 行 × {report.cols_written} 列")
    lines.append(f"  脱敏  {report.cells_masked:,} 个单元格")
    if report.by_label:
        width = max(display_width(label) for label in report.by_label)
        for label, count in sorted(report.by_label.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {pad(label, width)}  {count:,}")
    lines.append("")
    if report.cells_masked == 0:
        lines.append("  " + dim("没有匹配到需要脱敏的内容。"))
    else:
        lines.append("  " + tint("✓ 已脱敏。原文件未改动。", "32"))
    lines.append("")
    return "\n".join(lines)


def has_fullwidth_ascii(text: str) -> bool:
    """True if *text* holds full-width ASCII variants or an ideographic space.

    Han characters are wide too, but they are not a problem — only the
    full-width forms of ASCII (１２３, ＡＢＣ, （）, and U+3000) break parsing.
    """
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF60 or 0xFFE0 <= cp <= 0xFFE6 or cp == 0x3000:
            return True
    return False


# ---------------------------------------------------------------------------
# Encoding & delimiter detection
# ---------------------------------------------------------------------------

def _decodes(raw: bytes, encoding: str) -> bool:
    """True if *raw* decodes cleanly, tolerating a multi-byte char cut at EOF."""
    for cut in (0, 1, 2, 3):
        chunk = raw[: len(raw) - cut] if cut else raw
        try:
            chunk.decode(encoding)
            return True
        except (UnicodeDecodeError, LookupError):
            continue
    return False


def detect_encoding(path: Path) -> str:
    """Guess the encoding of *path* from its first 256 KiB."""
    with path.open("rb") as fh:
        return detect_encoding_bytes(fh.read(256 * 1024))


def detect_encoding_bytes(raw: bytes) -> str:
    """Guess an encoding by BOM first, then by trial decoding.

    Order matters: UTF-8 is strict, GB18030 accepts almost any byte sequence,
    so it must come after UTF-8 but before the legacy single-byte fallbacks.
    """
    if not raw:
        return "utf-8"
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for encoding in ("utf-8", "gb18030", "big5", "cp1252"):
        if _decodes(raw, encoding):
            return encoding
    return "latin-1"


def peek_text(path: Path, size: int = 256 * 1024) -> tuple[str, str]:
    """Return ``(encoding, decoded_prefix)`` without reading the whole file."""
    with path.open("rb") as fh:
        raw = fh.read(size)
    encoding = detect_encoding_bytes(raw)
    return encoding, raw.decode(encoding, errors="replace")


def sniff_delimiter(sample: str) -> str:
    """Pick the delimiter that splits every sampled line into the same shape."""
    lines = [ln for ln in sample.splitlines() if ln.strip()][:30]
    if not lines:
        return ","
    best, best_score = ",", -1.0
    for candidate in (",", "\t", ";", "|"):
        counts = [ln.count(candidate) for ln in lines]
        if min(counts) == 0:
            continue
        consistency = counts.count(counts[0]) / len(counts)
        # Consistency dominates; field count breaks ties.
        score = consistency * 1000 + counts[0]
        if score > best_score:
            best, best_score = candidate, score
    return best


# ---------------------------------------------------------------------------
# XLSX (zip + xml, no third-party reader)
# ---------------------------------------------------------------------------

def _col_index(cell_ref: str) -> int:
    """'B7' -> 1 (zero-based column index)."""
    index = 0
    for ch in cell_ref:
        if ch.isalpha():
            index = index * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, index - 1)


def _xlsx_date_styles(archive: zipfile.ZipFile) -> set[int]:
    """Style indices whose number format is a date/time."""
    if "xl/styles.xml" not in archive.namelist():
        return set()
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except ET.ParseError:
        return set()

    custom: dict[int, str] = {}
    for fmt in root.iter(f"{XLSX_NS}numFmt"):
        try:
            custom[int(fmt.get("numFmtId", "-1"))] = fmt.get("formatCode", "")
        except ValueError:
            continue

    date_styles: set[int] = set()
    cell_xfs = root.find(f"{XLSX_NS}cellXfs")
    if cell_xfs is None:
        return date_styles
    for idx, xf in enumerate(cell_xfs.findall(f"{XLSX_NS}xf")):
        try:
            fmt_id = int(xf.get("numFmtId", "0"))
        except ValueError:
            continue
        code = custom.get(fmt_id, "")
        if fmt_id in XLSX_DATE_FMT_IDS:
            date_styles.add(idx)
        elif code and re.search(r"[ymdhs]", re.sub(r'"[^"]*"|\[[^\]]*\]', "", code)):
            date_styles.add(idx)
    return date_styles


def _serial_to_date(serial: float) -> str:
    try:
        stamp = XLSX_EPOCH + timedelta(days=float(serial))
    except (ValueError, OverflowError):
        return str(serial)
    if stamp.hour or stamp.minute or stamp.second:
        return stamp.strftime("%Y-%m-%d %H:%M:%S")
    return stamp.strftime("%Y-%m-%d")


def read_xlsx(path: Path, limit: int | None = None) -> tuple[list[str], list[list[str]]]:
    """Read the first worksheet of an xlsx file."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall(f"{XLSX_NS}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{XLSX_NS}t")))
            except ET.ParseError:
                shared = []

        date_styles = _xlsx_date_styles(archive)

        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("xlsx 里没有找到工作表 / no worksheet found")
        try:
            sheet_root = ET.fromstring(archive.read(sheets[0]))
        except ET.ParseError as exc:
            raise ValueError(f"工作表解析失败 / cannot parse worksheet: {exc}") from exc

        rows: list[list[str]] = []
        for row in sheet_root.iter(f"{XLSX_NS}row"):
            # +1 because the header occupies the first row.
            if limit is not None and len(rows) >= limit + 1:
                break
            cells: dict[int, str] = {}
            for cell in row.findall(f"{XLSX_NS}c"):
                col = _col_index(cell.get("r", "A1"))
                kind = cell.get("t")
                value_node = cell.find(f"{XLSX_NS}v")
                raw = value_node.text if value_node is not None and value_node.text else ""

                if kind == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = raw
                elif kind == "inlineStr":
                    node = cell.find(f"{XLSX_NS}is")
                    value = "".join(t.text or "" for t in node.iter(f"{XLSX_NS}t")) if node is not None else ""
                elif kind == "b":
                    value = "TRUE" if raw == "1" else "FALSE"
                elif kind in ("str", "e"):
                    value = raw
                else:
                    try:
                        style = int(cell.get("s", "-1"))
                    except ValueError:
                        style = -1
                    if raw and style in date_styles:
                        value = _serial_to_date(raw)
                    else:
                        value = raw
                cells[col] = value

            if not cells:
                rows.append([])
                continue
            width = max(cells) + 1
            rows.append([cells.get(i, "") for i in range(width)])

    if not rows:
        return [], []

    header = rows[0]
    body = rows[1:]
    # Normalise width so ragged sheets don't break the analyser.
    width = max([len(header)] + [len(r) for r in body]) if body else len(header)
    header = header + [""] * (width - len(header))
    body = [r + [""] * (width - len(r)) for r in body]
    return header, body


# ---------------------------------------------------------------------------
# Generic readers
# ---------------------------------------------------------------------------

def _read_jsonl(text: str, limit: int | None) -> tuple[list[str], list[list[str]]]:
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
        if limit is not None and len(records) >= limit:
            break

    if not records:
        return [], []

    header: list[str] = []
    for record in records:
        for key in record:
            if key not in header:
                header.append(key)

    def render(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    rows = [[render(record.get(key, "")) for key in header] for record in records]
    return header, rows


def _read_json_array(text: str, limit: int | None) -> tuple[list[str], list[list[str]]]:
    data = json.loads(text)
    if isinstance(data, dict):
        # Common shapes: {"data": [...]} or {"rows": [...]}
        for key in ("data", "rows", "items", "records", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON 顶层不是数组 / top-level JSON is not an array")
    if limit is not None:
        data = data[:limit]
    header: list[str] = []
    for record in data:
        if isinstance(record, dict):
            for key in record:
                if key not in header:
                    header.append(key)
    if not header:
        if not data:
            return [], []
        # A flat array of scalars — [1, 2, 3] — is a one-column table, not an
        # error. Saying "no header row" to that would be unhelpful.
        rows = []
        for record in data:
            if isinstance(record, (dict, list)):
                rows.append([json.dumps(record, ensure_ascii=False)])
            elif record is None:
                rows.append([""])
            else:
                rows.append([str(record)])
        return ["value"], rows
    rows = []
    for record in data:
        if not isinstance(record, dict):
            record = {"value": record}
        rows.append([
            "" if record.get(k) is None else
            json.dumps(record[k], ensure_ascii=False) if isinstance(record[k], (dict, list)) else str(record[k])
            for k in header
        ])
    return header, rows


@dataclass
class Source:
    """A parsed input file: header plus body rows, all strings."""
    path: Path
    header: list[str]
    rows: list[list[str]]
    fmt: str
    encoding: str
    delimiter: str
    size: int
    ragged_rows: int = 0
    empty_rows: int = 0


@dataclass
class StreamStats:
    """Counters the row iterator fills in as it runs.

    The iterator is lazy, so these cannot be return values — they are read back
    after the caller has drained it.
    """
    read: int = 0
    ragged: int = 0
    empty: int = 0


@dataclass
class SourceStream:
    """Everything known about a file before its rows have been read."""
    header: list[str]
    fmt: str
    encoding: str
    delimiter: str
    size: int
    stats: StreamStats


def _raise_csv_field_limit() -> None:
    """Lift the 128 KiB per-field cap.

    ``csv`` refuses fields longer than 131072 characters by default, which is
    easy to hit with a CSV carrying JSON blobs or long descriptions. The cap
    exists to stop runaway memory use on malformed input, not because the
    parser cannot handle it.
    """
    limit = sys.maxsize
    while limit > 1 << 16:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            # 32-bit builds store the limit in a C long.
            limit //= 1024


def _csv_rows(
    path: Path,
    encoding: str,
    delimiter: str,
    limit: int | None,
    stats: StreamStats,
) -> tuple[list[str], Iterator[list[str]]]:
    """Open a delimited file and hand back its header plus a lazy row iterator.

    The file stays open for the lifetime of the iterator and is closed when it
    is exhausted or abandoned, so callers never hold the whole file in memory.
    """
    _raise_csv_field_limit()
    handle = path.open("r", encoding=encoding, errors="replace", newline="")
    try:
        reader = csv.reader(handle, delimiter=delimiter)
        header: list[str] | None = None
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                stats.empty += 1
                continue
            header = row
            break
        if header is None:
            raise ValueError("文件是空的，或者没有可解析的数据 / file is empty")
    except Exception:
        handle.close()
        raise

    width = len(header)

    def generate() -> Iterator[list[str]]:
        try:
            for row in reader:
                if limit is not None and stats.read >= limit:
                    return
                if not row or all(not cell.strip() for cell in row):
                    stats.empty += 1
                    continue
                if len(row) != width:
                    stats.ragged += 1
                    if len(row) < width:
                        row = row + [""] * (width - len(row))
                    else:
                        row = row[:width]
                stats.read += 1
                yield row
        finally:
            handle.close()

    return header, generate()


def iter_source(
    path: Path, limit: int | None = None
) -> tuple[SourceStream, Iterator[list[str]]]:
    """Detect what *path* is, then stream its rows.

    CSV / TSV are read incrementally — only one row is in memory at a time.
    XLSX, JSON and JSONL are parsed whole, because those formats have no
    meaningful line boundary to stream on and are rarely large.
    """
    if not path.exists():
        raise FileNotFoundError(f"文件不存在 / file not found: {path}")
    if path.is_dir():
        raise ValueError(f"这是一个文件夹，不是文件 / not a file: {path}")

    size = path.stat().st_size
    suffix = path.suffix.lower()
    stats = StreamStats()

    if suffix in (".xlsx", ".xlsm"):
        header, rows = read_xlsx(path, limit)
        stats.read = len(rows)
        return SourceStream(header, "xlsx", "—", "—", size, stats), iter(rows)

    encoding, sample = peek_text(path)
    stripped = sample.lstrip()

    if suffix == ".json" or (stripped[:1] in "[{" and suffix not in (".csv", ".tsv")):
        try:
            text = path.read_text(encoding=encoding, errors="replace")
            header, rows = _read_json_array(text, limit)
            stats.read = len(rows)
            return SourceStream(header, "json", encoding, "—", size, stats), iter(rows)
        except (json.JSONDecodeError, ValueError):
            pass

    # JSONL: several lines each parsing as a JSON object.
    probe = [ln for ln in stripped.splitlines() if ln.strip()][:3]
    if suffix in (".jsonl", ".ndjson") or (
        len(probe) >= 2 and all(ln.lstrip().startswith("{") for ln in probe)
        and all(_is_json_object(ln) for ln in probe)
    ):
        text = path.read_text(encoding=encoding, errors="replace")
        header, rows = _read_jsonl(text, limit)
        if header:
            stats.read = len(rows)
            return SourceStream(header, "jsonl", encoding, "—", size, stats), iter(rows)

    delimiter = "\t" if suffix == ".tsv" else sniff_delimiter(sample)
    fmt = "tsv" if delimiter == "\t" else "csv"
    header, rows = _csv_rows(path, encoding, delimiter, limit, stats)
    return SourceStream(header, fmt, encoding, delimiter, size, stats), rows


def load_source(path: Path, limit: int | None = None) -> Source:
    """Read any supported file fully into memory. Used by ``--fix``, which has
    to rewrite the whole table anyway."""
    stream, rows = iter_source(path, limit)
    return Source(
        path=path,
        header=stream.header,
        rows=list(rows),
        fmt=stream.fmt,
        encoding=stream.encoding,
        delimiter=stream.delimiter,
        size=stream.size,
        ragged_rows=stream.stats.ragged,
        empty_rows=stream.stats.empty,
    )


def _is_json_object(line: str) -> bool:
    try:
        return isinstance(json.loads(line), dict)
    except json.JSONDecodeError:
        return False


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

def parse_date(value: str) -> datetime | None:
    """Parse *value* as a date, or return None.

    ``datetime.strptime`` compiles a regex and touches the locale on every call,
    which costs ~14s on a 500k-row file. ISO-shaped strings therefore take a
    regex + direct construction fast path; only the ambiguous formats
    (01/02/2024, 2024年1月5日) fall through to strptime.
    """
    text = value.strip()
    if len(text) < 6 or len(text) > 32:
        return None

    match = ISO_LIKE_RE.match(text)
    if match:
        # One groups() call rather than six group(n) calls: this runs once per
        # cell of every date column, so the difference is measurable.
        year, month, day, hour, minute, second = match.groups()
        try:
            return datetime(
                int(year), int(month), int(day),
                int(hour or 0), int(minute or 0), int(second or 0),
            )
        except ValueError:
            return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


DATE_FORMAT_LABELS = {
    "%Y-%m-%d": "YYYY-MM-DD",
    "%Y/%m/%d": "YYYY/MM/DD",
    "%Y.%m.%d": "YYYY.MM.DD",
    "%d/%m/%Y": "DD/MM/YYYY",
    "%m/%d/%Y": "MM/DD/YYYY",
    "%d-%m-%Y": "DD-MM-YYYY",
    "%Y-%m-%d %H:%M:%S": "YYYY-MM-DD HH:MM:SS",
    "%Y/%m/%d %H:%M:%S": "YYYY/MM/DD HH:MM:SS",
    "%Y-%m-%d %H:%M": "YYYY-MM-DD HH:MM",
    "%Y-%m-%dT%H:%M:%S": "ISO 8601",
    "%Y-%m-%dT%H:%M:%SZ": "ISO 8601 UTC",
    "%Y-%m-%dT%H:%M:%S.%fZ": "ISO 8601 ms",
    "%Y-%m-%dT%H:%M:%S%z": "ISO 8601 +TZ",
    "%Y年%m月%d日": "YYYY年MM月DD日",
    "%Y-%m": "YYYY-MM",
}


def detect_date_format(value: str) -> str | None:
    """Name the shape a date string uses, or None if it is not a date.

    Only used for reporting. A column mixing ``2024-01-05`` with ``05/01/2024``
    is a genuine hazard: both parse, and they disagree about which number is
    the month.
    """
    text = value.strip()
    if ISO_LIKE_RE.match(text):
        base = {"-": "YYYY-MM-DD", "/": "YYYY/MM/DD", ".": "YYYY.MM.DD"}.get(text[4], "YYYY-MM-DD")
        if ":" in text:
            base += " HH:MM:SS"
            if "Z" in text:
                base += " UTC"
        return base
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(text, fmt)
            return DATE_FORMAT_LABELS.get(fmt, fmt)
        except ValueError:
            continue
    return None


AMBIGUOUS_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$")


def is_ambiguous_date(value: str) -> bool:
    """True when a slash/dash date cannot be resolved without knowing the locale.

    ``03/05/2024`` is 3 May in Europe and 5 March in the US. Both numbers are
    below 13, so the shape alone does not decide it. ``25/03/2024`` is
    unambiguous — 25 cannot be a month.
    """
    match = AMBIGUOUS_DATE_RE.match(value.strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first <= 12 and second <= 12


def classify_value(value: str) -> str:
    """Classify one raw cell as bool / int / float / date / text."""
    text = value.strip()
    low = text.lower()
    if low in BOOL_TOKENS:
        return "bool"
    if INT_RE.match(text):
        return "int"
    if FLOAT_RE.match(text):
        return "float"
    if parse_date(text) is not None:
        return "date"
    return "text"


def infer_kind(values: list[str]) -> tuple[str, int]:
    """Return ``(kind, mismatch_count)`` for a column.

    A column is considered numeric when at least TYPE_TOLERANCE of the sampled
    non-empty values parse as numbers. Everything else needs a clear majority.
    """
    sample = [v for v in values if not is_missing(v)][:TYPE_SAMPLE]
    if not sample:
        return "empty", 0

    counts = {"bool": 0, "int": 0, "float": 0, "date": 0, "text": 0}
    for value in sample:
        counts[classify_value(value)] += 1

    return kind_from_counts(counts)


def kind_from_counts(counts: dict[str, int]) -> tuple[str, int]:
    """Decide a column's type from already-tallied per-value classifications.

    Split out from :func:`infer_kind` so the streaming accumulator can tally
    while it reads, and only commit to a type once it has seen enough values.
    """
    total = sum(counts.values())
    if not total:
        return "empty", 0

    numeric = counts["int"] + counts["float"]
    if numeric / total >= TYPE_TOLERANCE:
        kind = "int" if counts["float"] == 0 else "float"
        return kind, total - numeric

    for kind in ("bool", "date"):
        if counts[kind] / total >= TYPE_TOLERANCE:
            return kind, total - counts[kind]

    if counts["text"] / total >= TYPE_TOLERANCE:
        return "text", total - counts["text"]

    # No type dominates: report the plurality so the user still learns something.
    plurality = max(counts, key=lambda k: counts[k])
    return (plurality if counts[plurality] > 0 else "text"), total - counts[plurality]


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@dataclass
class Issue:
    """One finding about the data, with a suggested fix."""
    severity: str          # high | medium | low
    title: str
    detail: str
    column: str | None = None
    suggestion: str = ""


@dataclass
class ColumnProfile:
    name: str
    index: int
    kind: str = "text"
    total: int = 0
    missing: int = 0
    distinct: int = 0
    distinct_capped: bool = False
    mismatch: int = 0
    #: True when quantiles / histogram were estimated from a sample, not the
    #: whole column (only happens above MAX_VALUES values).
    approximate: bool = False
    # numeric
    num_min: float | None = None
    num_max: float | None = None
    num_mean: float | None = None
    num_std: float | None = None
    num_median: float | None = None
    num_q1: float | None = None
    num_q3: float | None = None
    num_outliers: int = 0
    histogram: list[int] = field(default_factory=list)
    hist_edges: list[float] = field(default_factory=list)
    values_sample: list[float] = field(default_factory=list)
    # text
    top_values: list[tuple[str, int]] = field(default_factory=list)
    min_len: int = 0
    max_len: int = 0
    # dates
    date_min: str | None = None
    date_max: str | None = None
    date_formats: list[str] = field(default_factory=list)
    ambiguous_dates: int = 0
    # hygiene counters
    leading_space: int = 0
    trailing_space: int = 0
    fullwidth: int = 0
    control_chars: int = 0
    formula_cells: int = 0
    # raw values kept for sensitive-pattern detection (flat, capped)
    value_sample_raw: list[str] = field(default_factory=list)
    # PII labels -> share of sampled cells that match (set by find_issues)
    pii: dict[str, float] = field(default_factory=dict)
    empty_name: bool = False
    duplicate_name: bool = False
    raw_name: str = ""

    @property
    def missing_rate(self) -> float:
        return self.missing / self.total if self.total else 0.0

    @property
    def distinct_rate(self) -> float:
        return self.distinct / self.total if self.total else 0.0


@dataclass
class Profile:
    path: str
    fmt: str
    encoding: str
    delimiter: str
    size: int
    rows: int
    cols: int
    columns: list[ColumnProfile]
    issues: list[Issue]
    score: int
    duplicate_rows: int
    empty_rows: int
    ragged_rows: int
    preview: list[list[str]]
    header: list[str]
    elapsed: float
    correlations: list[tuple[str, str, float]] = field(default_factory=list)
    truncated: bool = False
    #: True when the file had more distinct rows than DEDUP_CAP, so the
    #: duplicate count only covers the rows remembered.
    duplicate_scan_capped: bool = False

    def issues_by(self, severity: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == severity]


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated quantile of an already sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return sorted_values[int(pos)]
    return sorted_values[low] * (high - pos) + sorted_values[high] * (pos - low)


def histogram(values: list[float], buckets: int = HIST_BUCKETS) -> tuple[list[int], list[float]]:
    """Equal-width histogram. Returns counts and bucket edges."""
    if not values:
        return [], []
    low, high = min(values), max(values)
    if low == high:
        return [len(values)], [low, high]
    step = (high - low) / buckets
    counts = [0] * buckets
    for value in values:
        idx = int((value - low) / step)
        counts[min(idx, buckets - 1)] += 1
    edges = [low + step * i for i in range(buckets + 1)]
    return counts, edges


def _to_number(value: str) -> float | None:
    """Parse a cell as a number, tolerating thousands separators."""
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        # Thousands separators are the only common fix-up worth trying.
        if "," not in text:
            return None
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return None


class ColumnAccumulator:
    """Streaming statistics for one column.

    Feed values in with :meth:`add`, read the result out with :meth:`finish`.
    Memory stays bounded no matter how many rows pass through, because every
    structure it keeps has a hard cap: the distinct-value table stops growing at
    DISTINCT_CAP, the numeric sample at MAX_VALUES, the type tally at
    TYPE_SAMPLE, the hygiene tally at HYGIENE_SAMPLE.

    Two-phase by necessity. A column's type cannot be known until enough values
    have been classified, yet the type decides which statistics are worth
    collecting — hoarding every numeric value of a text column is pure waste.
    So the first TYPE_SAMPLE non-empty values are buffered, a type is chosen,
    and the buffer is replayed into the right collector. Everything after that
    flows straight through.

    One consequence worth knowing: past MAX_VALUES numeric values, the median,
    quartiles and histogram come from a uniform random sample (reservoir
    sampling, Algorithm L) instead of the whole column, and the profile is
    flagged ``approximate``. Min, max, mean, standard deviation and the missing
    count stay exact at any size.
    """

    __slots__ = (
        "name", "index", "kind", "total", "missing", "mismatch",
        "_counts", "_head", "_typed", "_values", "_distinct_capped",
        "_distinct_count", "_sink",
        "_min_len", "_max_len",
        "_num_min", "_num_max", "_n", "_mean", "_m2",
        "_sample", "_seen", "_rng", "_w", "_next",
        "_date_min", "_date_max", "_formats", "_ambiguous", "_dated",
        "_hygiene_seen", "_leading", "_trailing", "_fullwidth", "_control",
        "_formula",
        "_pii_sample", "_pii_seen", "_pii_rng", "_pii_w", "_pii_next",
    )

    def __init__(self, name: str, index: int) -> None:
        self.name = name
        self.index = index
        self.kind: str | None = None
        self.total = 0
        self.missing = 0
        self.mismatch = 0

        self._counts = {"bool": 0, "int": 0, "float": 0, "date": 0, "text": 0}
        self._head: list[str] = []
        self._typed = 0
        self._values: dict[str, int] = {}
        self._distinct_capped = False
        self._distinct_count = 0
        #: Bound method that absorbs one stripped cell, once the type is known.
        self._sink = None

        self._min_len = -1
        self._max_len = -1

        self._num_min = math.inf
        self._num_max = -math.inf
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._sample = array("d")
        self._seen = 0
        self._rng = random.Random(RESERVOIR_SEED + index)
        self._w = 1.0
        self._next = 1

        self._date_min: datetime | None = None
        self._date_max: datetime | None = None
        self._formats: Counter[str] = Counter()
        self._ambiguous = 0
        self._dated = 0

        self._hygiene_seen = 0
        self._leading = 0
        self._trailing = 0
        self._fullwidth = 0
        self._control = 0
        self._formula = 0

        # Bounded reservoir of raw text, purely for sensitive-pattern scans.
        self._pii_sample: list[str] = []
        self._pii_seen = 0
        self._pii_rng = random.Random(RESERVOIR_SEED + 2_000_000 + index)
        self._pii_w = 1.0
        self._pii_next = 1

    # -- ingest ------------------------------------------------------------

    def add(self, value: str) -> None:
        """Fold one cell into the running statistics."""
        self.total += 1
        # Strip once here and pass the clean text down: every downstream step
        # wants it stripped anyway, and this is the hottest path in the program.
        text = value.strip()
        if not text:
            self.missing += 1
            return
        if len(text) <= 8 and text.lower() in MISSING_TOKENS:
            self.missing += 1
            return

        # Once the distinct table is full there is nothing left to learn from
        # it, and high-cardinality columns are exactly the ones with millions
        # of rows to get through — so stop paying for the lookup and hand the
        # memory back. Only the count is kept.
        values = self._values
        if self._distinct_capped:
            pass
        elif text in values:
            values[text] += 1
        elif len(values) < DISTINCT_CAP:
            values[text] = 1
            self._distinct_count = len(values)
        else:
            self._distinct_capped = True
            self._distinct_count = len(values)
            values.clear()

        # Formula injection is a security check, so it runs on every value
        # rather than a sample. The one-character prefix test keeps it cheap;
        # only cells that could possibly be formulas reach the full check.
        first = text[0]
        if first in "=@+-" and looks_like_formula(text):
            self._formula += 1

        # Sensitive-pattern scans need to see real cell strings, so keep a small
        # uniform sample of them. Cheap and flat: at most PII_SAMPLE_CAP values
        # live here no matter how many rows flow through.
        self._reservoir_pii(text)

        if self._hygiene_seen < HYGIENE_SAMPLE:
            self._hygiene_seen += 1
            self._check_hygiene(value)

        if self.kind is None:
            self._head.append(text)
            self._typed += 1
            self._counts[classify_value(text)] += 1
            if self._typed >= TYPE_SAMPLE:
                self._decide()
        else:
            self._sink(text)

    def _decide(self) -> None:
        """Commit to a column type, then replay the buffered prefix."""
        self.kind, self.mismatch = kind_from_counts(self._counts)
        self._sink = getattr(self, _SINKS[self.kind])
        head, self._head = self._head, []
        for text in head:
            self._sink(text)

    def _absorb_number(self, text: str) -> None:
        """Parse a numeric cell, then fold it into Welford and the reservoir."""
        try:
            x = float(text)
        except ValueError:
            # Thousands separators are the only common fix-up worth trying.
            if "," not in text:
                return
            try:
                x = float(text.replace(",", ""))
            except ValueError:
                return

        if x < self._num_min:
            self._num_min = x
        if x > self._num_max:
            self._num_max = x

        n = self._n + 1
        self._n = n
        delta = x - self._mean
        self._mean += delta / n
        self._m2 += delta * (x - self._mean)

        seen = self._seen + 1
        self._seen = seen
        if seen <= MAX_VALUES:
            self._sample.append(x)
            if seen == MAX_VALUES:
                self._seed_reservoir()
        elif seen >= self._next:
            self._replace_reservoir(x)

    def _absorb_date(self, text: str) -> None:
        stamp = parse_date(text)
        if stamp is not None:
            if self._date_min is None or stamp < self._date_min:
                self._date_min = stamp
            if self._date_max is None or stamp > self._date_max:
                self._date_max = stamp
        if self._dated < TYPE_SAMPLE:
            self._dated += 1
            shape = detect_date_format(text)
            if shape is not None:
                self._formats[shape] += 1
            if is_ambiguous_date(text):
                self._ambiguous += 1

    def _absorb_text(self, text: str) -> None:
        length = len(text)
        if self._min_len < 0 or length < self._min_len:
            self._min_len = length
        if length > self._max_len:
            self._max_len = length

    def _check_hygiene(self, value: str) -> None:
        if value[:1].isspace():
            self._leading += 1
        if value[-1:].isspace():
            self._trailing += 1
        has_fw = has_ctrl = False
        for ch in value:
            cp = ord(ch)
            if not has_fw and (0xFF01 <= cp <= 0xFF60 or cp == 0x3000 or cp == 0xFFE0 <= cp <= 0xFFE6):
                has_fw = True
            elif not has_ctrl and cp < 32 and ch != "\t":
                has_ctrl = True
            if has_fw and has_ctrl:
                break
        if has_fw:
            self._fullwidth += 1
        if has_ctrl:
            self._control += 1

    # -- reservoir (Algorithm L) -------------------------------------------

    def _seed_reservoir(self) -> None:
        self._w = max(math.exp(math.log(self._rng.random() or 1e-12) / MAX_VALUES), 1e-12)
        self._next = self._seen + self._skip()

    def _replace_reservoir(self, x: float) -> None:
        """Swap one sampled slot, then skip ahead geometrically.

        Plain reservoir sampling calls the RNG once per row, which on ten
        million rows costs seconds. Algorithm L skips ahead instead, so the RNG
        is touched only a few hundred thousand times.
        """
        self._sample[self._rng.randrange(MAX_VALUES)] = x
        self._w = max(self._w * math.exp(math.log(self._rng.random() or 1e-12) / MAX_VALUES), 1e-12)
        self._next = self._seen + self._skip()
    def _skip(self) -> int:
        return _skip_ahead(self._rng, self._w)

    def _reservoir_pii(self, text: str) -> None:
        """Algorithm-L reservoir of raw text, capped at PII_SAMPLE_CAP."""
        seen = self._pii_seen + 1
        self._pii_seen = seen
        sample = self._pii_sample
        cap = PII_SAMPLE_CAP
        if seen <= cap:
            sample.append(text)
            if seen == cap:
                self._pii_w = max(math.exp(math.log(self._pii_rng.random() or 1e-12) / cap), 1e-12)
                self._pii_next = seen + _skip_ahead(self._pii_rng, self._pii_w)
            return
        if seen >= self._pii_next:
            sample[self._pii_rng.randrange(cap)] = text
            self._pii_w = max(self._pii_w * math.exp(math.log(self._pii_rng.random() or 1e-12) / cap), 1e-12)
            self._pii_next = seen + _skip_ahead(self._pii_rng, self._pii_w)

    # -- output ------------------------------------------------------------

    def finish(self) -> ColumnProfile:
        """Freeze the running statistics into a ColumnProfile."""
        if self.kind is None:
            self._decide()

        col = ColumnProfile(name=self.name, index=self.index, total=self.total)
        col.kind = self.kind or "empty"
        col.missing = self.missing
        col.mismatch = self.mismatch
        col.distinct = self._distinct_count
        col.distinct_capped = self._distinct_capped
        # Raw strings are kept for every column, so a phone number stored as a
        # bare 11-digit string (in an int column) still gets scanned.
        col.value_sample_raw = list(self._pii_sample)
        col.leading_space = self._leading
        col.trailing_space = self._trailing
        col.fullwidth = self._fullwidth
        col.control_chars = self._control
        col.formula_cells = self._formula

        if col.kind in ("int", "float"):
            if self._n:
                col.num_min = self._num_min
                col.num_max = self._num_max
                col.num_mean = self._mean
                col.num_std = math.sqrt(self._m2 / self._n) if self._n > 1 else 0.0
                col.approximate = self._n > MAX_VALUES
                ordered = sorted(self._sample)
                col.num_median = percentile(ordered, 0.5)
                col.num_q1 = percentile(ordered, 0.25)
                col.num_q3 = percentile(ordered, 0.75)
                if col.num_q1 is not None and col.num_q3 is not None:
                    iqr = col.num_q3 - col.num_q1
                    low_fence = col.num_q1 - 1.5 * iqr
                    high_fence = col.num_q3 + 1.5 * iqr
                    col.num_outliers = sum(1 for n in self._sample if n < low_fence or n > high_fence)
                col.histogram, col.hist_edges = histogram(self._sample)
                # A uniform sample's prefix is still uniform, so correlation can
                # read straight off it.
                col.values_sample = list(self._sample[:CORRELATION_SAMPLE])

        elif col.kind == "date":
            if self._date_min is not None:
                col.date_min = self._date_min.strftime("%Y-%m-%d")
            if self._date_max is not None:
                col.date_max = self._date_max.strftime("%Y-%m-%d")
            col.date_formats = [name for name, _ in self._formats.most_common()]
            col.ambiguous_dates = self._ambiguous

        else:
            col.min_len = max(self._min_len, 0)
            col.max_len = max(self._max_len, 0)
            col.top_values = sorted(self._values.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

        return col


def profile_column(name: str, index: int, values: list[str]) -> ColumnProfile:
    """Compute every statistic for one already-loaded column."""
    acc = ColumnAccumulator(name, index)
    for value in values:
        acc.add(value)
    return acc.finish()


#: Which per-type absorber a column feeds into once its type is settled.
#: Resolved by name so the binding happens per column, not per cell.
_SINKS = {
    "int": "_absorb_number",
    "float": "_absorb_number",
    "date": "_absorb_date",
    "bool": "_absorb_text",
    "text": "_absorb_text",
    # An empty column has no non-empty values, so this is never called; the
    # entry exists so _SINKS covers every kind kind_from_counts can return.
    "empty": "_absorb_text",
}


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, or None when it is undefined.

    NaN marks a cell that was missing in one of the two columns. Those rows are
    dropped as pairs, never one side at a time — otherwise the two series slide
    out of alignment and the coefficient describes rows that never existed.

    Undefined means: fewer than 3 paired values, or either series is constant
    (zero variance), where the formula divides by zero.
    """
    pairs = [(x, y) for x, y in zip(xs, ys) if x == x and y == y]
    n = len(pairs)
    if n < 3:
        return None
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    numerator = var_x = var_y = 0.0
    for x, y in pairs:
        dx = x - mean_x
        dy = y - mean_y
        numerator += dx * dy
        var_x += dx * dx
        var_y += dy * dy
    if var_x <= 0 or var_y <= 0:
        return None
    return numerator / math.sqrt(var_x * var_y)


def compute_correlations(columns: list[ColumnProfile]) -> list[tuple[str, str, float]]:
    """All column pairs whose |r| clears the reporting threshold, strongest first."""
    numeric = [
        col for col in columns
        if col.kind in ("int", "float") and len(col.values_sample) >= 3
    ][:MAX_CORRELATION_COLUMNS]

    pairs: list[tuple[str, str, float]] = []
    for i in range(len(numeric)):
        for j in range(i + 1, len(numeric)):
            r = pearson(numeric[i].values_sample, numeric[j].values_sample)
            if r is not None and abs(r) >= CORRELATION_REPORT_THRESHOLD:
                pairs.append((numeric[i].name, numeric[j].name, r))
    pairs.sort(key=lambda item: -abs(item[2]))
    return pairs


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def find_issues(profile: Profile) -> list[Issue]:
    """Turn raw statistics into a prioritised, actionable list."""
    issues: list[Issue] = []
    rows = max(profile.rows, 1)

    if profile.rows == 0:
        issues.append(Issue(
            "high", "没有数据行",
            "文件只有表头，一行数据都没有",
            suggestion="确认导出是否中断；如果确实没有数据，下游会静默得到一张空表",
        ))

    if profile.ragged_rows:
        issues.append(Issue(
            "high", "行列数不一致",
            f"{profile.ragged_rows} 行的字段数与表头不符（已按表头宽度截断/补齐）",
            suggestion="检查这些行是否含未转义的分隔符，或重新导出数据",
        ))

    if profile.duplicate_rows:
        rate = profile.duplicate_rows / rows
        if rate >= 0.01:
            issues.append(Issue(
                "high" if rate > 0.2 else "medium", "存在重复行",
                f"{profile.duplicate_rows} 行（{rate:.1%}）与其他行完全相同",
                suggestion="去重前先确认重复是采集错误还是业务上本就允许",
            ))

    if profile.empty_rows:
        issues.append(Issue(
            "low", "存在空行", f"{profile.empty_rows} 行完全为空（已跳过）",
        ))

    if profile.duplicate_scan_capped:
        issues.append(Issue(
            "low", "重复行检测未覆盖全表",
            f"文件超过 {DEDUP_CAP:,} 个不同行，去重只核对了前 {DEDUP_CAP:,} 个",
            suggestion="数据量到这个规模，建议在数据库里用 GROUP BY 判断重复",
        ))

    for col in profile.columns:
        if col.empty_name:
            issues.append(Issue("high", "列名为空", f"第 {col.index + 1} 列没有名字",
                                suggestion="补一个列名，否则按列名取值的代码会出错"))

    reported_names: set[str] = set()
    for col in profile.columns:
        if col.duplicate_name and col.raw_name not in reported_names:
            reported_names.add(col.raw_name)
            count = sum(1 for c in profile.columns if c.raw_name == col.raw_name)
            issues.append(Issue("high", "列名重复",
                                f"「{col.raw_name}」出现了 {count} 次，读取时后一列会覆盖前一列",
                                column=col.raw_name,
                                suggestion="重命名后重新导出，否则按列名取值只会拿到其中一列"))

    # Names that differ only by case collide in every case-insensitive system:
    # SQL Server, Excel headers, and most ORMs.
    spellings: dict[str, list[str]] = {}
    for col in profile.columns:
        if col.raw_name and not col.duplicate_name:
            spellings.setdefault(col.raw_name.lower(), []).append(col.raw_name)
    for group in spellings.values():
        if len(set(group)) > 1:
            joined = "、".join(sorted(set(group)))
            issues.append(Issue("medium", "列名仅大小写不同",
                                f"{joined} 同时存在",
                                column=group[0],
                                suggestion="SQL Server、Excel、多数 ORM 不区分大小写，"
                                           "这两列会被当成同一列"))

    for col in profile.columns:
        label = col.name or f"第{col.index + 1}列"

        # Sensitive-pattern scan. Runs on every column (not just text ones): a
        # phone number stored as a bare 11-digit string lands in an int column
        # and would otherwise slip past a text-only check. The sample is capped,
        # so cost is flat; a column only flags when 80%+ of its sample matches,
        # so a lone email in a name field does not.
        col.pii = detect_pii(col.value_sample_raw)
        if col.pii:
            findings = "、".join(f"{name} {share:.0%}" for name, share in col.pii.items())
            issues.append(Issue(
                "high", "疑似包含敏感信息",
                f"「{label}」样本中有 {findings} 命中",
                column=col.name,
                suggestion="确认该列是否含有 PII/密钥；在分享或入库前先做脱敏",
            ))

        if col.kind == "empty" and col.total:
            issues.append(Issue("high", "整列为空", f"「{label}」没有任何数据", column=col.name,
                                suggestion="确认这列是否应该存在"))
            continue

        if col.formula_cells:
            issues.append(Issue(
                "medium", "存在可被 Excel 执行的单元格",
                f"「{label}」有 {col.formula_cells} 个值以 = + - @ 开头（如 =cmd|'/C calc'!A0）",
                column=col.name,
                suggestion="这类 CSV 注入在 Excel / WPS 里打开时会当成公式执行；"
                           "导入时选「作为文本」，或让导出方在值前加单引号",
            ))

        if col.missing_rate >= 0.5:
            issues.append(Issue("high", "缺失严重",
                                f"「{label}」缺失 {col.missing_rate:.0%}（{col.missing}/{col.total}）",
                                column=col.name, suggestion="考虑补数据或直接删除该列"))
        elif col.missing_rate >= 0.2:
            issues.append(Issue("medium", "缺失偏多",
                                f"「{label}」缺失 {col.missing_rate:.0%}（{col.missing}/{col.total}）",
                                column=col.name))

        if col.kind != "text" and col.mismatch:
            rate = col.mismatch / max(col.total - col.missing, 1)
            if rate >= 0.05:
                issues.append(Issue("medium", "类型混杂",
                                    f"「{label}」判定为 {col.kind}，但有 {col.mismatch} 个值不是（{rate:.1%}）",
                                    column=col.name,
                                    suggestion="这些值多为空格、占位符或单位混写，清洗后可正常计算"))

        if col.leading_space or col.trailing_space:
            issues.append(Issue("low", "多余空格",
                                f"「{label}」有 {col.leading_space + col.trailing_space} 个值带首尾空格",
                                column=col.name, suggestion="strip 之后再去重/匹配，否则同一值会被当成两个"))

        if col.fullwidth:
            issues.append(Issue("low", "含全角字符",
                                f"「{label}」有 {col.fullwidth} 个值含全角字符（中文标点、全角数字）",
                                column=col.name, suggestion="全角数字无法直接参与计算，需要先转半角"))

        if col.control_chars:
            issues.append(Issue("medium", "含不可见字符",
                                f"「{label}」有 {col.control_chars} 个值含制表符/控制字符",
                                column=col.name, suggestion="这些字符肉眼看不见，但会让字符串比较失败"))

        if col.kind == "date" and len(col.date_formats) > 1:
            issues.append(Issue(
                "high", "日期格式混用",
                f"「{label}」同时存在 {len(col.date_formats)} 种格式："
                f"{'、'.join(col.date_formats[:4])}",
                column=col.name,
                suggestion="DD/MM/YYYY 和 MM/DD/YYYY 只差顺序，程序无法自动判断，"
                           "必须先人工统一，否则整列日期会错位",
            ))

        if col.ambiguous_dates:
            issues.append(Issue(
                "medium", "日期存在歧义",
                f"「{label}」有 {col.ambiguous_dates} 个日期无法确定日月顺序（如 03/05/2024）",
                column=col.name,
                suggestion="欧美习惯相反，本工具按 日/月 解析；如果数据来自美国，"
                           "整列日期会错位，建议统一改成 YYYY-MM-DD",
            ))

        if col.distinct == 1 and col.total > 1:
            issues.append(Issue("low", "常量列", f"「{label}」所有值都一样", column=col.name,
                                suggestion="对分析没有信息量，可考虑移除"))

        # A capped distinct count means "at least DISTINCT_CAP", which is
        # itself proof of high cardinality — the rate is meaningless once the
        # table stops growing, so treat the cap as a match.
        if col.kind == "text" and col.total >= 20 and (col.distinct_capped or col.distinct_rate >= 0.98):
            shown = f"≥{col.distinct:,}" if col.distinct_capped else f"{col.distinct:,}"
            issues.append(Issue("low", "疑似 ID 列",
                                f"「{label}」几乎每行都不同（{shown}/{col.total:,}）",
                                column=col.name, suggestion="大概率是主键或唯一标识，不适合做统计"))

        elif col.kind == "int" and col.total >= 20 and col.missing == 0 and (
            col.distinct_capped or col.distinct_rate >= 0.99
        ):
            span = (col.num_max or 0) - (col.num_min or 0)
            if span >= col.total - 1:
                issues.append(Issue("low", "疑似序号列",
                                    f"「{label}」取值连续且不重复（{col.num_min:g} ~ {col.num_max:g}）",
                                    column=col.name,
                                    suggestion="这是主键/自增序号，做统计或画图时建议排除"))

        if col.num_outliers and col.total:
            rate = col.num_outliers / col.total
            if rate >= 0.05:
                issues.append(Issue("low", "存在离群值",
                                    f"「{label}」有 {col.num_outliers} 个值落在 1.5×IQR 之外（{rate:.1%}）",
                                    column=col.name, suggestion="先确认是录入错误还是真实极值，再决定是否剔除"))

    # Near-perfectly correlated columns form one group. Reporting every pair
    # would emit three warnings for the same finding (A-B, A-C, B-C).
    parent: dict[str, str] = {}

    def find(name: str) -> str:
        parent.setdefault(name, name)
        root = name
        while parent[root] != root:
            root = parent[root]
        while parent[name] != root:      # path compression
            parent[name], name = root, parent[name]
        return root

    for a, b, r in profile.correlations:
        if abs(r) >= CORRELATION_REDUNDANT_THRESHOLD:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

    groups: dict[str, list[str]] = {}
    for name in list(parent):
        groups.setdefault(find(name), []).append(name)

    for members in groups.values():
        if len(members) > 1:
            quoted = "、".join(f"「{m}」" for m in members)
            issues.append(Issue(
                "medium", "疑似冗余列组",
                f"{quoted} 互相高度相关（|r| ≥ {CORRELATION_REDUNDANT_THRESHOLD}）",
                column=members[0],
                suggestion="这一组列携带同样的信息，保留一个即可；"
                           "否则建模时同一个因素会被重复计算",
            ))

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (order[i.severity], i.column or ""))
    return issues


def score_quality(issues: list[Issue]) -> int:
    """0-100 health score. High issues cost the most."""
    penalty = {"high": 10, "medium": 4, "low": 1}
    total = sum(penalty[i.severity] for i in issues)
    return max(0, 100 - total)


# ---------------------------------------------------------------------------
# Top-level analysis
# ---------------------------------------------------------------------------

def _skip_ahead(rng: random.Random, w: float) -> int:
    """How many items Algorithm L may pass over before taking the next one."""
    if not 0.0 < w < 1.0:
        return 0
    return int(math.log(rng.random() or 1e-12) / math.log1p(-w))


def _row_fingerprint(row: list[str]) -> int:
    """A 64-bit fingerprint of a whole row.

    Rows are compared by fingerprint rather than by value so duplicate
    detection never holds a copy of the file in memory. ``hash(tuple(row))``
    is used rather than a hand-rolled loop because the built-in runs entirely
    in C — it is roughly nine times faster, which matters at ten million rows.

    Two different rows colliding in 64 bits is a 1-in-1.8e19 event. Even across
    a billion rows the odds are about one in eighteen billion.
    """
    return hash(tuple(row))


def _attach_row_aligned_samples(
    columns: list[ColumnProfile], rows: list[tuple[str, ...]]
) -> None:
    """Replace each numeric column's sample with a row-aligned float series.

    A gap in one column used to shift that column's sample against its
    neighbour's, so the two series described different rows. Missing cells
    become NaN instead, and :func:`pearson` drops whole pairs.
    """
    numeric = [i for i, col in enumerate(columns) if col.kind in ("int", "float")]
    if not numeric or not rows:
        return

    series: dict[int, list[float]] = {i: [] for i in numeric}
    for row in rows:
        for i in numeric:
            cell = row[i] if i < len(row) else ""
            number = _to_number(cell) if cell else None
            series[i].append(math.nan if number is None else number)
    for i in numeric:
        columns[i].values_sample = series[i]


def analyse(path: Path, limit: int | None = None) -> Profile:
    """Stream *path* and build a complete Profile.

    Memory is bounded by the column count, not the row count: rows are folded
    into per-column accumulators as they arrive and then discarded. A 10-million
    row file costs the same as a 10-row one, apart from time.
    """
    started = time.perf_counter()
    stream, rows = iter_source(path, limit)

    if not stream.header:
        raise ValueError("没有识别到表头 / no header row found")

    # Disambiguate duplicate / empty column names so dict keys stay unique.
    names: list[str] = []
    counts: dict[str, int] = {}
    for idx, raw_name in enumerate(stream.header):
        name = raw_name.strip() or f"列{idx + 1}"
        counts[name] = counts.get(name, 0) + 1
        names.append(name if counts[name] == 1 else f"{name}_{counts[name]}")

    accumulators = [ColumnAccumulator(name, idx) for idx, name in enumerate(names)]
    width = len(accumulators)

    seen: set[int] = set()
    duplicates = 0
    dedup_capped = False
    preview: list[list[str]] = []
    row_count = 0

    # Correlations need the same rows on both sides of every pair. Sampling each
    # column separately would pair row 8 of one column with row 12 of another,
    # so the sample is drawn once per row and split into columns afterwards.
    # The cell budget keeps a 200-column sheet from costing 200x the memory.
    corr_cap = max(50, min(CORRELATION_SAMPLE, 400_000 // max(width, 1)))
    corr_rows: list[tuple[str, ...]] = []
    corr_seen = 0
    corr_rng = random.Random(RESERVOIR_SEED)
    corr_w = 1.0
    corr_next = 1

    try:
        for row in rows:
            row_count += 1
            if len(preview) < PREVIEW_ROWS:
                preview.append(row)

            fingerprint = _row_fingerprint(row)
            if fingerprint in seen:
                duplicates += 1
            elif not dedup_capped:
                if len(seen) < DEDUP_CAP:
                    seen.add(fingerprint)
                else:
                    dedup_capped = True

            corr_seen += 1
            if corr_seen <= corr_cap:
                corr_rows.append(tuple(row))
                if corr_seen == corr_cap:
                    corr_w = max(math.exp(math.log(corr_rng.random() or 1e-12) / corr_cap), 1e-12)
                    corr_next = corr_seen + _skip_ahead(corr_rng, corr_w)
            elif corr_seen >= corr_next:
                corr_rows[corr_rng.randrange(corr_cap)] = tuple(row)
                corr_w = max(corr_w * math.exp(math.log(corr_rng.random() or 1e-12) / corr_cap), 1e-12)
                corr_next = corr_seen + _skip_ahead(corr_rng, corr_w)

            if len(row) == width:
                for acc, cell in zip(accumulators, row):
                    acc.add(cell)
            else:
                for idx, acc in enumerate(accumulators):
                    acc.add(row[idx] if idx < len(row) else "")
    finally:
        # Release the file handle even if a column accumulator raised. Plain
        # list iterators (xlsx, JSON) have nothing to close.
        close = getattr(rows, "close", None)
        if close is not None:
            close()

    header_counts = Counter(raw.strip() for raw in stream.header)
    columns: list[ColumnProfile] = []
    for idx, acc in enumerate(accumulators):
        col = acc.finish()
        col.raw_name = stream.header[idx].strip()
        col.empty_name = not col.raw_name
        col.duplicate_name = header_counts[col.raw_name] > 1
        columns.append(col)

    _attach_row_aligned_samples(columns, corr_rows)

    profile = Profile(
        path=str(path),
        fmt=stream.fmt,
        encoding=stream.encoding,
        delimiter=stream.delimiter,
        size=stream.size,
        rows=row_count,
        cols=len(names),
        columns=columns,
        issues=[],
        score=100,
        duplicate_rows=duplicates,
        empty_rows=stream.stats.empty,
        ragged_rows=stream.stats.ragged,
        preview=preview,
        header=names,
        elapsed=0.0,
        truncated=limit is not None and row_count >= limit,
        duplicate_scan_capped=dedup_capped,
    )
    profile.correlations = compute_correlations(columns)
    profile.issues = find_issues(profile)
    profile.score = score_quality(profile.issues)
    profile.elapsed = time.perf_counter() - started
    return profile


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

USE_COLOR = bool(sys.stdout.isatty()) and not os.environ.get("NO_COLOR")


def tint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bold(text: str) -> str:
    return tint(text, "1")


def dim(text: str) -> str:
    return tint(text, "2")


def score_color(score: int) -> str:
    if score >= 90:
        return "32"
    if score >= 70:
        return "33"
    return "31"


def score_label(score: int) -> str:
    if score >= 90:
        return "干净"
    if score >= 75:
        return "良好"
    if score >= 60:
        return "需要清理"
    return "问题较多"


SEVERITY_STYLE = {
    "high": ("31", "严重"),
    "medium": ("33", "警告"),
    "low": ("36", "提示"),
}


def score_bar(score: int, width: int = 22) -> str:
    filled = round(score / 100 * width)
    return "█" * filled + "░" * (width - filled)


def column_summary(col: ColumnProfile) -> str:
    """One-line human summary of what a column contains."""
    if col.kind == "empty":
        return "（无数据）"
    if col.kind in ("int", "float") and col.num_min is not None:
        mean = f"{col.num_mean:,.2f}" if col.num_mean is not None else "—"
        return f"{col.num_min:g} ~ {col.num_max:g}，均值 {mean}"
    if col.kind == "date" and col.date_min:
        return f"{col.date_min} ~ {col.date_max}"
    if col.kind == "bool":
        top = "、".join(v for v, _ in col.top_values[:2])
        return f"取值：{top}"
    if col.top_values:
        value, count = col.top_values[0]
        share = count / max(col.total - col.missing, 1)
        return f"最常见「{truncate(value, 18)}」{share:.0%}"
    if col.distinct_capped:
        return "取值几乎都不同（ID / 自由文本）"
    return "—"


def render_table(headers: list[str], rows: list[list[str]], aligns: list[str]) -> list[str]:
    """Render a fixed-width table that stays aligned with CJK text."""
    widths = []
    for i, head in enumerate(headers):
        width = display_width(head)
        for row in rows:
            width = max(width, display_width(row[i]))
        widths.append(width)

    out = ["  ".join(bold(pad(h, widths[i])) for i, h in enumerate(headers))]
    out.append(dim("  ".join("─" * w for w in widths)))
    for row in rows:
        out.append("  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)))
    return out


def render_terminal(profile: Profile, show_preview: bool = False) -> str:
    """Build the whole terminal report as one string."""
    lines: list[str] = []
    name = Path(profile.path).name
    fmt_label = {"csv": "CSV", "tsv": "TSV", "jsonl": "JSONL", "json": "JSON", "xlsx": "XLSX"}.get(profile.fmt, profile.fmt)
    sep_label = {"\t": "制表符", ",": "逗号", ";": "分号", "|": "竖线", "—": "—"}.get(profile.delimiter, profile.delimiter)

    lines.append("")
    lines.append("  " + bold("dataloupe") + dim(f"  v{__version__}"))
    lines.append("")
    lines.append(f"  {bold(name)}")
    meta_parts = [fmt_label]
    if profile.encoding not in ("—", ""):
        meta_parts.append(encoding_label(profile.encoding))
    if profile.delimiter not in ("—", ""):
        meta_parts.append(f"{sep_label}分隔")
    meta_parts.append(human_size(profile.size))
    lines.append("  " + dim(" · ".join(meta_parts)))
    counts = f"{profile.rows:,} 行 × {profile.cols} 列"
    lines.append("  " + dim(f"{counts} · 用时 {profile.elapsed:.2f}s"))
    if profile.truncated:
        lines.append("  " + tint("（已按 --limit 截断，统计仅覆盖读取到的部分）", "33"))
    lines.append("")

    color = score_color(profile.score)
    lines.append(f"  体检评分  {tint(f'{profile.score:>3}', color + ';1')} {dim('/ 100')}  "
                 f"{tint(score_bar(profile.score), color)}  {tint(score_label(profile.score), color)}")
    lines.append("")

    high = len(profile.issues_by("high"))
    medium = len(profile.issues_by("medium"))
    low = len(profile.issues_by("low"))
    lines.append("  " + dim("问题：") +
                 tint(f"严重 {high}", "31" if high else "2") + "  " +
                 tint(f"警告 {medium}", "33" if medium else "2") + "  " +
                 tint(f"提示 {low}", "36" if low else "2"))
    lines.append("")

    headers = ["列名", "类型", "缺失", "唯一值", "内容摘要"]
    rows = []
    for col in profile.columns:
        miss = f"{col.missing_rate:.0%}" if col.total else "—"
        miss_cell = tint(miss, "31" if col.missing_rate >= 0.5 else "33" if col.missing_rate >= 0.2 else "2")
        rows.append([
            truncate(col.name, 22),
            col.kind,
            miss_cell,
            f"≥{col.distinct:,}" if col.distinct_capped else f"{col.distinct:,}",
            truncate(column_summary(col), 34),
        ])
    lines.extend("  " + line for line in render_table(headers, rows, ["left", "left", "right", "right", "left"]))
    lines.append("")

    if profile.correlations:
        lines.append("  " + bold("强相关列") + dim("  相关系数越接近 ±1 越像同一件事"))
        corr_rows = []
        for a, b, r in profile.correlations[:6]:
            if abs(r) >= CORRELATION_REDUNDANT_THRESHOLD:
                color = "31"
            elif abs(r) >= 0.8:
                color = "33"
            else:
                color = "2"
            corr_rows.append([truncate(a, 16), truncate(b, 16), tint(f"{r:+.2f}", color)])
        lines.extend("  " + line for line in render_table(
            ["列 A", "列 B", "相关系数"], corr_rows, ["left", "left", "right"]))
        lines.append("")

    pii_cols = [(col.name, col.pii) for col in profile.columns if col.pii]
    if pii_cols:
        lines.append("  " + bold("⚠ 疑似敏感信息") + dim("  分享或入库前请先脱敏"))
        for name, pii in pii_cols:
            findings = "、".join(f"{label} {share:.0%}" for label, share in pii.items())
            lines.append(f"  {tint('●', '31')} {name}: {findings}")
        lines.append("")

    if profile.issues:
        lines.append("  " + bold("发现的问题"))
        for issue in profile.issues:
            code, label = SEVERITY_STYLE[issue.severity]
            head = f"  {tint('●', code)} {tint(label, code)}  {issue.title}"
            lines.append(head)
            lines.append(f"      {issue.detail}")
            if issue.suggestion:
                lines.append("      " + dim(f"→ {issue.suggestion}"))
        lines.append("")
    else:
        lines.append("  " + tint("没有发现质量问题。", "32"))
        lines.append("")

    sampled = [col.name for col in profile.columns if col.approximate]
    if sampled:
        lines.append("  " + dim(
            f"中位数 / 分位数 / 直方图取自 {MAX_VALUES:,} 个随机抽样值"
            f"（{truncate('、'.join(sampled[:3]), 40)}"
            f"{' 等' if len(sampled) > 3 else ''}）；"
            f"最小值、最大值、均值、标准差、缺失数仍是精确值"
        ))
        lines.append("")

    if show_preview:
        lines.append("  " + bold("数据预览") + dim(f"（前 {min(len(profile.preview), PREVIEW_ROWS)} 行）"))
        head_row = [truncate(h, 14) for h in profile.header]
        body = [[truncate(cell, 14) for cell in row] for row in profile.preview[:20]]
        lines.extend("  " + line for line in render_table(head_row, body, ["left"] * len(head_row)))
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

HTML_CSS = """
:root{
  --bg:#f7f7f5; --card:#ffffff; --line:#e6e6e2; --ink:#1b1b19; --muted:#6b6b64;
  --accent:#4f46e5; --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --chip:#f1f1ee;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#141413; --card:#1d1d1b; --line:#2f2f2b; --ink:#f2f2ef; --muted:#9a9a92;
        --chip:#262623; --accent:#8b85f5;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 72px}
header{display:flex;flex-wrap:wrap;gap:24px;align-items:center;justify-content:space-between;
  padding-bottom:24px;border-bottom:1px solid var(--line);margin-bottom:32px}
h1{font-size:22px;font-weight:600;margin:0 0 6px;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:13px;font-family:ui-monospace,"Cascadia Code",Consolas,monospace}
.brand{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.score{display:flex;align-items:center;gap:14px}
.score-num{font-size:34px;font-weight:600;line-height:1}
.score-cap{font-size:12px;color:var(--muted);margin-top:4px}
h2{font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);margin:40px 0 14px}
.issues{display:grid;gap:8px}
.issue{background:var(--card);border:1px solid var(--line);border-left-width:3px;
  border-radius:10px;padding:13px 16px}
.issue.high{border-left-color:var(--bad)} .issue.medium{border-left-color:var(--warn)}
.issue.low{border-left-color:var(--accent)}
.issue-top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.tag{font-size:11px;padding:1px 7px;border-radius:20px;background:var(--chip);color:var(--muted);
  font-weight:500;white-space:nowrap}
.issue.high .tag{color:var(--bad)} .issue.medium .tag{color:var(--warn)} .issue.low .tag{color:var(--accent)}
.issue-title{font-weight:500}
.issue-body{color:var(--muted);font-size:13.5px;margin-top:4px}
.fix{color:var(--muted);font-size:13px;margin-top:6px;padding-left:14px;position:relative}
.fix::before{content:"→";position:absolute;left:0;opacity:.6}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(310px,1fr))}
.col{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
.col-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
.col-name{font-weight:600;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.kind{font-size:11px;font-family:ui-monospace,Consolas,monospace;padding:2px 8px;border-radius:20px;
  background:var(--chip);color:var(--muted);white-space:nowrap}
.kind.int,.kind.float{color:var(--accent)} .kind.date{color:var(--ok)} .kind.empty{color:var(--bad)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:13px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;text-align:right;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.spark{margin-top:12px}
.bar{fill:var(--accent);opacity:.55}
.tbl-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--card);
  max-height:520px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 14px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--card);font-weight:500;color:var(--muted);font-size:12px;
  letter-spacing:.04em;z-index:1}
tr:last-child td{border-bottom:none}
td{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;max-width:280px;overflow:hidden;
  text-overflow:ellipsis}
td.num{text-align:right}
footer{margin-top:48px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);
  font-size:12px;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
.empty-note{color:var(--muted);font-size:13.5px}
.pii{margin-top:9px;display:flex;flex-wrap:wrap;gap:6px}
.pii-tag{background:#fde2e1;color:#b42318;border:1px solid #f6b8b3;border-radius:6px;
  padding:2px 8px;font-size:11.5px;font-weight:600}
.note{color:var(--muted);font-size:12.5px;line-height:1.6;margin:14px 0 0}
.corr-grid{display:grid;gap:8px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.corr{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;
  display:flex;align-items:center;gap:8px;font-size:12.5px;font-family:ui-monospace,Consolas,monospace}
.corr .pair{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.corr .arrow{color:var(--muted);font-size:11px;flex-shrink:0}
.corr .r{margin-left:auto;font-weight:600;flex-shrink:0}
.corr .r.bad{color:var(--bad)} .corr .r.warn{color:var(--warn)} .corr .r.ok{color:var(--muted)}
.verdict{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:20px;
  font-size:13px;font-weight:600;border:1px solid}
.verdict.breaking{background:#fde2e1;color:#b42318;border-color:#f6b8b3}
.verdict.drift{background:#fdf3e0;color:#96590b;border-color:#f3d9a8}
.verdict.clean{background:#e5f6ea;color:#15803d;border-color:#b7e4c3}
.facts{display:flex;flex-wrap:wrap;gap:26px;margin:18px 0 4px;
  font-family:ui-monospace,Consolas,monospace;font-size:13px}
.facts div{color:var(--muted)}
.facts b{color:var(--ink);font-weight:600}
.sec{margin-top:26px}
.sec h3{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin:0 0 10px}
.rows{display:grid;gap:6px}
.row{background:var(--card);border:1px solid var(--line);border-radius:9px;
  padding:9px 14px;display:flex;align-items:center;gap:12px;font-size:13px;
  font-family:ui-monospace,Consolas,monospace}
.row .mark{font-weight:700;width:14px;text-align:center;flex-shrink:0}
.row .mark.add{color:var(--ok)} .row .mark.del{color:var(--bad)}
.row .mark.ren{color:var(--muted)} .row .mark.bad{color:var(--bad)}
.row .name{font-weight:600}
.row .why{margin-left:auto;color:var(--muted);font-size:12.5px;text-align:right}
.shift{background:var(--card);border:1px solid var(--line);border-radius:9px;padding:11px 14px}
.shift-top{display:flex;align-items:baseline;gap:10px;font-size:13px;
  font-family:ui-monospace,Consolas,monospace}
.shift-top .name{font-weight:600}
.shift-top .move{margin-left:auto;font-weight:600;color:var(--warn)}
.bars{margin-top:9px;display:grid;gap:5px}
.barline{display:flex;align-items:center;gap:9px;font-size:11.5px;
  font-family:ui-monospace,Consolas,monospace;color:var(--muted)}
.barline .track{flex:1;height:8px;background:var(--chip);border-radius:5px;overflow:hidden}
.barline .fill{height:100%;border-radius:5px}
.barline .fill.old{background:var(--muted);opacity:.45}
.barline .fill.new{background:var(--warn)}
.barline .val{width:96px;text-align:right;flex-shrink:0;color:var(--ink)}
"""

HTML_JS = """
document.querySelectorAll('[data-filter]').forEach(function(btn){
  btn.addEventListener('click',function(){
    var want=btn.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(function(b){b.classList.remove('on')});
    btn.classList.add('on');
    document.querySelectorAll('.issue').forEach(function(el){
      el.style.display=(want==='all'||el.classList.contains(want))?'':'none';
    });
  });
});
"""


def _esc(text: object) -> str:
    import html as _html
    return _html.escape("" if text is None else str(text), quote=True)


def _sparkline(col: ColumnProfile) -> str:
    """Inline SVG histogram for a numeric column."""
    if not col.histogram:
        return ""
    peak = max(col.histogram) or 1
    width, height = 260, 44
    bar_w = width / len(col.histogram)
    bars = []
    for i, count in enumerate(col.histogram):
        h = (count / peak) * (height - 4)
        x = i * bar_w
        bars.append(
            f'<rect class="bar" x="{x:.1f}" y="{height - h:.1f}" '
            f'width="{max(bar_w - 1.2, 0.8):.1f}" height="{max(h, 1):.1f}" rx="1.5"/>'
        )
    low = f"{col.hist_edges[0]:g}" if col.hist_edges else ""
    high = f"{col.hist_edges[-1]:g}" if col.hist_edges else ""
    return (
        f'<div class="spark"><svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="分布直方图">{ "".join(bars) }</svg>'
        f'<div class="kv" style="margin-top:4px"><dt>{_esc(low)}</dt>'
        f'<dd>{_esc(high)}</dd></div></div>'
    )


def _score_ring(score: int) -> str:
    color = "#16a34a" if score >= 90 else "#d97706" if score >= 70 else "#dc2626"
    radius, circumference = 26, 2 * 3.14159 * 26
    offset = circumference * (1 - score / 100)
    return (
        '<svg width="64" height="64" viewBox="0 0 64 64" role="img" aria-label="评分">'
        f'<circle cx="32" cy="32" r="{radius}" fill="none" stroke="currentColor" '
        'stroke-opacity="0.12" stroke-width="6"/>'
        f'<circle cx="32" cy="32" r="{radius}" fill="none" stroke="{color}" stroke-width="6" '
        f'stroke-linecap="round" stroke-dasharray="{circumference:.1f}" '
        f'stroke-dashoffset="{offset:.1f}" transform="rotate(-90 32 32)"/>'
        f'<text x="32" y="37" text-anchor="middle" font-size="17" font-weight="600" '
        f'fill="currentColor">{score}</text></svg>'
    )


def _column_card(col: ColumnProfile) -> str:
    items: list[str] = []
    items.append(f"<dt>缺失</dt><dd>{col.missing_rate:.1%}</dd>")
    items.append(f"<dt>唯一值</dt><dd>{'≥' if col.distinct_capped else ''}{col.distinct:,}</dd>")

    if col.kind in ("int", "float") and col.num_min is not None:
        items.append(f"<dt>范围</dt><dd>{col.num_min:g} ~ {col.num_max:g}</dd>")
        items.append(f"<dt>均值</dt><dd>{col.num_mean:,.2f}</dd>")
        items.append(f"<dt>中位数</dt><dd>{col.num_median:,.2f}{'*' if col.approximate else ''}</dd>")
        items.append(f"<dt>标准差</dt><dd>{col.num_std:,.2f}</dd>")
        if col.num_outliers:
            items.append(f"<dt>离群值</dt><dd>{col.num_outliers:,}</dd>")
    elif col.kind == "date" and col.date_min:
        items.append(f"<dt>最早</dt><dd>{col.date_min}</dd>")
        items.append(f"<dt>最晚</dt><dd>{col.date_max}</dd>")
    elif col.kind == "text":
        if col.min_len or col.max_len:
            items.append(f"<dt>长度</dt><dd>{col.min_len} ~ {col.max_len}</dd>")
        for value, count in col.top_values[:3]:
            items.append(f"<dt>{_esc(truncate(value, 16))}</dt><dd>{count:,}</dd>")

    pii_badge = ""
    if col.pii:
        tags = " ".join(
            f'<span class="pii-tag">{_esc(name)}</span>' for name in col.pii
        )
        pii_badge = f'<div class="pii">{tags}</div>'

    return (
        f'<div class="col"><div class="col-head">'
        f'<span class="col-name" title="{_esc(col.name)}">{_esc(col.name)}</span>'
        f'<span class="kind {_esc(col.kind)}">{_esc(col.kind)}</span></div>'
        f'<dl class="kv">{"".join(items)}</dl>{_sparkline(col)}{pii_badge}</div>'
    )


def render_html(profile: Profile) -> str:
    """Build a fully self-contained HTML report."""
    name = Path(profile.path).name
    fmt_label = {"csv": "CSV", "tsv": "TSV", "jsonl": "JSONL", "json": "JSON", "xlsx": "XLSX"}.get(profile.fmt, profile.fmt)
    sep_label = {"\t": "制表符", ",": "逗号", ";": "分号", "|": "竖线", "—": "—"}.get(profile.delimiter, profile.delimiter)

    counts = {"high": len(profile.issues_by("high")),
              "medium": len(profile.issues_by("medium")),
              "low": len(profile.issues_by("low"))}

    if profile.issues:
        issue_html = []
        for issue in profile.issues:
            label = SEVERITY_STYLE[issue.severity][1]
            fix = f'<div class="fix">{_esc(issue.suggestion)}</div>' if issue.suggestion else ""
            issue_html.append(
                f'<div class="issue {issue.severity}"><div class="issue-top">'
                f'<span class="tag">{label}</span>'
                f'<span class="issue-title">{_esc(issue.title)}</span></div>'
                f'<div class="issue-body">{_esc(issue.detail)}</div>{fix}</div>'
            )
        issues_block = "".join(issue_html)
    else:
        issues_block = '<div class="empty-note">没有发现质量问题，这份数据很干净。</div>'

    cards = "".join(_column_card(col) for col in profile.columns)

    sampled = [col.name for col in profile.columns if col.approximate]
    sampled_note = (
        f'<p class="note">带 * 的中位数与直方图取自 {MAX_VALUES:,} 个随机抽样值'
        f'（{"、".join(_esc(n) for n in sampled[:5])}{" 等" if len(sampled) > 5 else ""}）。'
        f'最小值、最大值、均值、标准差、缺失数都是精确值。</p>'
        if sampled else ""
    )

    if profile.correlations:
        corr_items = []
        for a, b, r in profile.correlations[:12]:
            if abs(r) >= CORRELATION_REDUNDANT_THRESHOLD:
                cls = "bad"
            elif abs(r) >= 0.8:
                cls = "warn"
            else:
                cls = "ok"
            corr_items.append(
                f'<div class="corr"><span class="pair" title="{_esc(a)}">{_esc(a)}</span>'
                f'<span class="arrow">↔</span>'
                f'<span class="pair" title="{_esc(b)}">{_esc(b)}</span>'
                f'<span class="r {cls}">{r:+.2f}</span></div>'
            )
        corr_block = (f'<h2>强相关列 · {len(profile.correlations)} 对</h2>'
                      f'<div class="corr-grid">{"".join(corr_items)}</div>')
    else:
        corr_block = ""

    head_cells = "".join(f"<th>{_esc(h)}</th>" for h in profile.header)
    body_rows = []
    for row in profile.preview:
        cells = "".join(f"<td>{_esc(cell)}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")

    numeric_types = {"int", "float"}
    header_html = "".join(
        f'<th class="{"num" if col.kind in numeric_types else ""}">{_esc(h)}</th>'
        for h, col in zip(profile.header, profile.columns)
    )

    html_meta_parts = [fmt_label]
    if profile.encoding not in ("—", ""):
        html_meta_parts.append(encoding_label(profile.encoding))
    if profile.delimiter not in ("—", ""):
        html_meta_parts.append(f"{sep_label}分隔")
    html_meta_parts.append(human_size(profile.size))
    html_meta = _esc(" · ".join(html_meta_parts))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dataloupe · {_esc(name)}</title>
<style>{HTML_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <div class="brand">dataloupe report</div>
    <h1>{_esc(name)}</h1>
    <div class="meta">{html_meta}</div>
    <div class="meta">{profile.rows:,} 行 × {profile.cols} 列 · 分析耗时 {profile.elapsed:.2f}s</div>
  </div>
  <div class="score">
    {_score_ring(profile.score)}
    <div><div class="score-num">{profile.score}<span style="font-size:14px;color:var(--muted)">/100</span></div>
    <div class="score-cap">{_esc(score_label(profile.score))}</div></div>
  </div>
</header>

<h2>问题 · {counts['high']} 严重 / {counts['medium']} 警告 / {counts['low']} 提示</h2>
<div class="issues">{issues_block}</div>

<h2>列概览 · {profile.cols} 列</h2>
<div class="grid">{cards}</div>
{sampled_note}

{corr_block}

<h2>数据预览 · 前 {len(profile.preview)} 行</h2>
<div class="tbl-wrap"><table>
<thead><tr>{header_html}</tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table></div>

<footer>
  <span>dataloupe v{__version__} · 零依赖数据体检</span>
  <span>{_esc(profile.path)}</span>
</footer>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def render_json(profile: Profile) -> str:
    payload = {
        "file": profile.path,
        "format": profile.fmt,
        "encoding": profile.encoding,
        "delimiter": profile.delimiter,
        "bytes": profile.size,
        "rows": profile.rows,
        "cols": profile.cols,
        "score": profile.score,
        "elapsed_seconds": round(profile.elapsed, 4),
        "duplicate_rows": profile.duplicate_rows,
        "ragged_rows": profile.ragged_rows,
        "empty_rows": profile.empty_rows,
        # Anything that limits how much of the file was examined, so a consumer
        # can tell an exact answer from a bounded one.
        "truncated": profile.truncated,
        "duplicate_scan_capped": profile.duplicate_scan_capped,
        "issues": [
            {"severity": i.severity, "title": i.title, "detail": i.detail,
             "column": i.column, "suggestion": i.suggestion}
            for i in profile.issues
        ],
        "columns": [
            {
                "name": c.name,
                "kind": c.kind,
                "missing": c.missing,
                "missing_rate": round(c.missing_rate, 4),
                "distinct": c.distinct,
                "distinct_capped": c.distinct_capped,
                "approximate": c.approximate,
                "min": c.num_min, "max": c.num_max, "mean": c.num_mean,
                "median": c.num_median, "std": c.num_std, "outliers": c.num_outliers,
                "date_min": c.date_min, "date_max": c.date_max,
                "top_values": c.top_values,
                "formula_cells": c.formula_cells,
                "pii": c.pii,
            }
            for c in profile.columns
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# SQL schema output
# ---------------------------------------------------------------------------

def quote_identifier(name: str, dialect: str) -> str:
    """Quote a name unless it is a plain lowercase identifier.

    Quoting everything works but reads badly; leaving everything unquoted
    breaks on ``Order`` or ``我的列``. So: quote only when needed.
    """
    if re.fullmatch(r"[a-z_][a-z0-9_]*", name) and name not in SQL_RESERVED:
        return name
    quote = "`" if dialect == "mysql" else '"'
    return quote + name.replace(quote, quote * 2) + quote


def sql_type_for(col: ColumnProfile, dialect: str) -> str:
    """Map an inferred column type onto a SQL type for *dialect*."""
    kind = col.kind
    # A date column that also holds times needs a timestamp, not a date.
    if kind == "date" and any("HH" in shape for shape in col.date_formats):
        kind = "datetime"
    return SQL_TYPES[dialect][kind]


def render_sql(profile: Profile, dialect: str, table: str | None = None) -> str:
    """A CREATE TABLE statement built from the inferred column types."""
    if dialect not in SQL_TYPES:
        raise ValueError(f"不支持的方言 / unknown dialect: {dialect}")

    name = table or Path(profile.path).stem or "data"
    types = [sql_type_for(col, dialect) for col in profile.columns]
    identifiers = [quote_identifier(col.name, dialect) for col in profile.columns]
    name_width = max(display_width(i) for i in identifiers)

    body = ",\n".join(
        f"    {pad(ident, name_width)}  {kind}"
        for ident, kind in zip(identifiers, types)
    )
    return (
        f"-- dataloupe {__version__} · {Path(profile.path).name} · "
        f"{profile.rows:,} rows × {profile.cols} columns\n"
        f"CREATE TABLE {quote_identifier(name, dialect)} (\n"
        f"{body}\n"
        f");"
    )


# ---------------------------------------------------------------------------
# Cleaning (--fix)
# ---------------------------------------------------------------------------

#: Characters in the full-width ASCII block sit exactly 0xFEE0 above their
#: half-width counterparts, so conversion is arithmetic, not a lookup table.
FULLWIDTH_OFFSET = 0xFEE0


def to_halfwidth(text: str) -> str:
    """Convert full-width ASCII (１２３, ＡＢＣ, （）) to half-width."""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - FULLWIDTH_OFFSET))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def strip_control(text: str) -> str:
    """Drop control characters except tab, which is meaningful in CSV fields."""
    return "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32)


@dataclass
class FixReport:
    """What the cleaning pass actually changed."""
    output: Path
    cells_changed: int = 0
    whitespace: int = 0
    fullwidth: int = 0
    control: int = 0
    rows_dropped: int = 0
    cols_dropped: int = 0
    headers_fixed: int = 0
    rows_written: int = 0
    cols_written: int = 0


def clean_table(
    header: list[str],
    rows: list[list[str]],
    *,
    dedupe: bool = False,
    drop_empty_cols: bool = False,
    fix_headers: bool = True,
    normalise_missing: bool = True,
) -> tuple[list[str], list[list[str]], FixReport]:
    """Repair the common defects dataloupe reports.

    Everything here is conservative: values are trimmed and normalised, never
    reinterpreted. A cell that held "NA" becomes empty only when it is a
    recognised missing marker; real text is left alone.
    """
    report = FixReport(output=Path())

    cleaned_header = list(header)
    if fix_headers:
        renamed: list[str] = []
        seen: dict[str, int] = {}
        for idx, raw in enumerate(header):
            name = raw.strip() or f"column_{idx + 1}"
            if name in seen:
                seen[name] += 1
                name = f"{name}_{seen[name]}"
            else:
                seen[name] = 1
            if name != raw:
                report.headers_fixed += 1
            renamed.append(name)
        cleaned_header = renamed

    cleaned: list[list[str]] = []
    for row in rows:
        new_row = []
        for cell in row:
            original = cell
            value = cell.strip()
            if value != original:
                report.whitespace += 1

            half = to_halfwidth(value)
            if half != value:
                report.fullwidth += 1
                value = half

            stripped = strip_control(value)
            if stripped != value:
                report.control += 1
                value = stripped

            if normalise_missing and value and value.lower() in MISSING_TOKENS:
                value = ""

            if value != original:
                report.cells_changed += 1
            new_row.append(value)
        cleaned.append(new_row)

    if dedupe:
        seen: set[tuple[str, ...]] = set()
        unique_rows = []
        for row in cleaned:
            key = tuple(row)
            if key in seen:
                report.rows_dropped += 1
                continue
            seen.add(key)
            unique_rows.append(row)
        cleaned = unique_rows

    kept_header = list(cleaned_header)
    if drop_empty_cols and cleaned:
        keep = [i for i in range(len(kept_header)) if any(row[i] for row in cleaned)]
        if len(keep) < len(kept_header):
            report.cols_dropped = len(kept_header) - len(keep)
            kept_header = [kept_header[i] for i in keep]
            cleaned = [[row[i] for i in keep] for row in cleaned]

    report.rows_written = len(cleaned)
    report.cols_written = len(kept_header)
    return kept_header, cleaned, report


def write_clean_csv(
    path: Path,
    header: list[str],
    rows: list[list[str]],
    *,
    bom: bool = True,
) -> None:
    """Write UTF-8 CSV. A BOM by default so Excel opens it without mojibake."""
    encoding = "utf-8-sig" if bom else "utf-8"
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def render_fix_report(report: FixReport) -> str:
    lines = ["", f"  {bold('已清洗')} → {report.output}", ""]
    if report.cells_changed:
        parts = []
        if report.whitespace:
            parts.append(f"空格 {report.whitespace}")
        if report.fullwidth:
            parts.append(f"全角 {report.fullwidth}")
        if report.control:
            parts.append(f"控制字符 {report.control}")
        detail = f"（{'、'.join(parts)}）" if parts else ""
        lines.append(f"  修正 {report.cells_changed} 个单元格{detail}")
    if report.rows_dropped:
        lines.append(f"  删除 {report.rows_dropped} 行重复")
    if report.cols_dropped:
        lines.append(f"  删除 {report.cols_dropped} 个空列")
    if report.headers_fixed:
        lines.append(f"  修正 {report.headers_fixed} 个列名（空名或重名）")
    if not (report.cells_changed or report.rows_dropped or report.cols_dropped or report.headers_fixed):
        lines.append("  数据本来就是干净的，内容原样写出")
    lines.append(f"  写出 {report.rows_written:,} 行 × {report.cols_written} 列")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _configure_output() -> None:
    """Force UTF-8 on stdout and stderr.

    Windows defaults to the system code page, which on an English install is
    cp1252 — a charset that cannot represent any Chinese character. Printing
    the report then dies with UnicodeEncodeError. Reconfiguring the streams
    fixes it for every locale, including CI runners.

    ``errors="replace"`` is the last line of defence: if a terminal genuinely
    cannot render a character, print a placeholder instead of crashing.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a real stream (captured by a test runner), or already closed.
            pass


# ---------------------------------------------------------------------------
# Schema & data drift between two exports
# ---------------------------------------------------------------------------

#: Column kinds a downstream numeric reader can treat interchangeably. A column
#: moving int -> float (or back) is a widening, not a break: both flow through
#: ``float()`` without complaint. Anything else is a schema break.
_NUMERIC_KINDS = frozenset({"int", "float"})


@dataclass
class DistributionShift:
    """A numeric column whose centre moved between two exports."""
    column: str
    old_mean: float
    new_mean: float
    old_median: float | None
    new_median: float | None
    #: How many of the *old* standard deviations the mean moved by.
    effect: float
    #: Share of the old mean that the move represents, when the mean was non-zero.
    relative: float | None


@dataclass
class RowDiff:
    """How the row sets of two exports differ, over shared columns only.

    Counts are of *distinct* rows, not raw row counts: a duplicate row adds
    nothing to a set comparison, and saying so plainly is more honest than
    reporting a number that mixes duplicates into the total.
    """
    old_only: int = 0
    new_only: int = 0
    common: int = 0
    old_distinct: int = 0
    new_distinct: int = 0
    shared_columns: list[str] = field(default_factory=list)
    #: False when a side hit ROW_DIFF_CAP, making the numbers a lower bound.
    exact: bool = True


@dataclass
class SchemaDiff:
    old_path: str
    new_path: str
    old_rows: int
    new_rows: int
    old_encoding: str
    new_encoding: str
    old_cols: int
    new_cols: int
    added: list[str]
    removed: list[str]
    renamed: list[tuple[str, str]]
    type_changes: list[tuple[str, str, str, bool]]
    type_inconsistencies: list[tuple[str, float, float]]
    quality_regressions: list[tuple[str, float, float]]
    drift: bool
    breaking: bool
    #: Filled in by the caller: they need the files, not just the profiles.
    distribution_shifts: list[DistributionShift] = field(default_factory=list)
    row_diff: RowDiff | None = None


def diff_profiles(old: "Profile", new: "Profile") -> SchemaDiff:
    """Compare two profiles of the *same* logical dataset at different times.

    The headline cases are the ones that silently break a pipeline: a column
    renamed, a column dropped, a numeric column that turned into text because
    someone typed "N/A" into it, a date column that stopped parsing. Row counts
    and encodings are reported too, because a switch from UTF-8 to GBK is also
    a change nobody asked for.
    """
    old_cols = {c.name: c for c in old.columns}
    new_cols = {c.name: c for c in new.columns}

    added = [n for n in new_cols if n not in old_cols]
    removed = [n for n in old_cols if n not in new_cols]
    shared = [n for n in new_cols if n in old_cols]

    type_changes = []
    type_inconsistencies = []
    quality_regressions = []
    distribution_shifts: list[DistributionShift] = []
    for name in shared:
        oc, nc = old_cols[name], new_cols[name]
        if oc.kind != nc.kind:
            breaking = not (oc.kind in _NUMERIC_KINDS and nc.kind in _NUMERIC_KINDS)
            type_changes.append((name, oc.kind, nc.kind, breaking))

        shift = _distribution_shift(oc, nc)
        if shift is not None:
            distribution_shifts.append(shift)

        # A column can keep its inferred type yet start swallowing dirty values
        # (an "N/A" that was not a missing token, a "TBD", a stray letter). The
        # kind stays put but the share of values that do not parse as that type
        # jumps — exactly the kind of quiet corruption a drift check exists to
        # catch, so track the mismatch rate separately from the type itself.
        old_mr = oc.mismatch / oc.total if oc.total else 0.0
        new_mr = nc.mismatch / nc.total if nc.total else 0.0
        if new_mr - old_mr >= 0.1 or (old_mr == 0.0 and new_mr > 0.0):
            type_inconsistencies.append((name, old_mr, new_mr))

        old_rate = oc.missing / oc.total if oc.total else 0.0
        new_rate = nc.missing / nc.total if nc.total else 0.0
        if new_rate - old_rate >= 0.1 or (old_rate == 0.0 and new_rate > 0.0):
            quality_regressions.append((name, old_rate, new_rate))

    renamed = _guess_renames(removed, added, old_cols, new_cols)

    # A guessed rename is one drop + one add with the same underlying column, so
    # take them back out of the plain added/removed lists to avoid double noise.
    renamed_old = {o for o, _ in renamed}
    renamed_new = {n for _, n in renamed}
    added = [n for n in added if n not in renamed_new]
    removed = [n for n in removed if n not in renamed_old]

    encoding_changed = old.encoding != new.encoding
    drift = bool(added or removed or renamed or type_changes
                 or type_inconsistencies or quality_regressions
                 or distribution_shifts
                 or encoding_changed or old.rows != new.rows)
    breaking = bool(
        removed
        or encoding_changed
        or quality_regressions
        or type_inconsistencies
        or any(b for (_, _, _, b) in type_changes)
    )

    return SchemaDiff(
        old_path=old.path, new_path=new.path,
        old_rows=old.rows, new_rows=new.rows,
        old_encoding=old.encoding, new_encoding=new.encoding,
        old_cols=old.cols, new_cols=new.cols,
        added=added, removed=removed, renamed=renamed,
        type_changes=type_changes, type_inconsistencies=type_inconsistencies,
        quality_regressions=quality_regressions,
        drift=drift, breaking=breaking,
        distribution_shifts=distribution_shifts,
    )


def _looks_like_sequence(col: "ColumnProfile") -> bool:
    """True for a column that is an auto-increment key or a dense row number.

    Such a column's mean is a function of how many rows exist, not of the data,
    so reporting it as a "distribution shift" would be pure noise.
    """
    if col.kind != "int" or col.num_min is None or col.num_max is None:
        return False
    if not col.distinct_capped and col.distinct_rate < 0.99:
        return False
    return (col.num_max - col.num_min) >= col.total - 1


def _distribution_shift(old: "ColumnProfile", new: "ColumnProfile") -> DistributionShift | None:
    """Decide whether a numeric column's centre moved enough to matter.

    Two yardsticks, either of which is enough:

    * **effect size** — the mean moved by at least one old standard deviation.
      Scale-free, so it works the same for revenue and for percentages.
    * **relative change** — the mean moved by at least half its old value.
      Catches the case where the spread dwarfs the mean, which the effect-size
      test would wave through.

    A column that is not numeric on both sides, or has no values, is skipped.
    """
    if old.kind not in _NUMERIC_KINDS or new.kind not in _NUMERIC_KINDS:
        return None
    if old.num_mean is None or new.num_mean is None:
        return None
    # An auto-increment key moves its mean every time rows are appended, and
    # that says nothing about the data. Same test the quality checks use.
    if _looks_like_sequence(old):
        return None

    delta = new.num_mean - old.num_mean
    std = old.num_std or 0.0
    effect = abs(delta) / std if std > 1e-12 else (0.0 if delta == 0 else math.inf)

    base = abs(old.num_mean)
    relative = delta / base if base > 1e-12 else None

    moved = effect >= DISTRIBUTION_SHIFT_EFFECT
    if relative is not None and abs(relative) >= DISTRIBUTION_SHIFT_RELATIVE:
        moved = True
    if not moved:
        return None

    return DistributionShift(
        column=old.name,
        old_mean=old.num_mean, new_mean=new.num_mean,
        old_median=old.num_median, new_median=new.num_median,
        effect=effect, relative=relative,
    )


def _row_hashes(
    path: Path,
    columns: list[str],
    limit: int | None,
    cap: int,
) -> tuple[set[int], int, bool]:
    """Fingerprint every row of *path* over *columns*, bounded by *cap*.

    Only the named columns take part, so a schema change (an added or dropped
    column) cannot make every row look different — which is exactly the noise
    that would swamp a naive whole-row comparison.
    """
    stream, rows = iter_source(path, limit)
    index = {name: i for i, name in enumerate(stream.header)}
    picks = [index[name] for name in columns if name in index]

    hashes: set[int] = set()
    total = 0
    capped = False
    try:
        for row in rows:
            total += 1
            fingerprint = hash(tuple(row[i] if i < len(row) else "" for i in picks))
            if fingerprint in hashes:
                continue
            if len(hashes) < cap:
                hashes.add(fingerprint)
            else:
                capped = True
    finally:
        close = getattr(rows, "close", None)
        if close is not None:
            close()
    return hashes, total, capped


def compare_rows(
    old_path: Path,
    new_path: Path,
    shared_columns: list[str],
    limit: int | None = None,
    cap: int = ROW_DIFF_CAP,
) -> RowDiff:
    """Compare the row *sets* of two exports over the columns they share.

    This answers "did rows disappear, or did new ones show up" without holding
    either file in memory: one 64-bit fingerprint per distinct row, capped.
    """
    if not shared_columns:
        return RowDiff(shared_columns=[])

    old_set, _, old_capped = _row_hashes(old_path, shared_columns, limit, cap)
    new_set, _, new_capped = _row_hashes(new_path, shared_columns, limit, cap)

    common = len(old_set & new_set)
    return RowDiff(
        old_only=len(old_set) - common,
        new_only=len(new_set) - common,
        common=common,
        old_distinct=len(old_set),
        new_distinct=len(new_set),
        shared_columns=shared_columns,
        exact=not (old_capped or new_capped),
    )


def _guess_renames(removed: list[str], added: list[str],
                   old_cols: dict[str, "ColumnProfile"],
                   new_cols: dict[str, "ColumnProfile"]) -> list[tuple[str, str]]:
    """Pair dropped and new columns that look like the same column renamed.

    A rename is *guessed*, never asserted: it fires only when the old and new
    names are close (edit distance <= 2, or one contains the other) AND the
    inferred types agree. Real renames without those hints stay reported as a
    plain drop plus add, which is the honest thing to do.
    """
    if not removed or not added:
        return []

    candidates = []
    for o in removed:
        for n in added:
            if old_cols[o].kind != new_cols[n].kind:
                continue
            lo, ln = o.lower(), n.lower()
            if lo == ln:
                continue
            dist = _edit_distance(lo, ln)
            # Containment is a strong rename signal even across length ("id" in
            # "user_id"); require the contained token to be at least two chars so
            # a stray single letter never matches. Plain near-misses below edit
            # distance 2 only count once both names are long enough that a two-
            # character difference is unlikely to be coincidence.
            contained = (len(lo) >= 2 and lo in ln) or (len(ln) >= 2 and ln in lo)
            if contained or (len(lo) >= 3 and len(ln) >= 3 and dist <= 2):
                candidates.append((dist, o, n))
    candidates.sort()

    seen_old: set[str] = set()
    seen_new: set[str] = set()
    pairs = []
    for _, o, n in candidates:
        if o in seen_old or n in seen_new:
            continue
        seen_old.add(o)
        seen_new.add(n)
        pairs.append((o, n))
    return pairs


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance with a flat row-vector (no full matrix)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        cur = [prev[0] + 1]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def render_diff_terminal(diff: SchemaDiff, strict: bool = False) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("  " + bold("dataloupe") + dim(f"  v{__version__}") + "  " + bold("数据漂移对比"))
    lines.append("")
    lines.append("  " + bold(Path(diff.new_path).name))
    lines.append("  " + dim(f"{Path(diff.old_path).name} -> {Path(diff.new_path).name}"))
    lines.append("")

    delta = diff.new_rows - diff.old_rows
    sign = "+" if delta >= 0 else ""
    lines.append(f"  行数  {diff.old_rows:,} -> {diff.new_rows:,}  ({sign}{delta:,})")
    lines.append(f"  列数  {diff.old_cols} -> {diff.new_cols}")
    if diff.old_encoding != diff.new_encoding:
        lines.append("  编码  " + tint(f"{diff.old_encoding} -> {diff.new_encoding}", "33") + "  ⚠")
    lines.append("")

    if not diff.drift:
        lines.append("  " + tint("✓ 两份文件结构一致，未发现漂移。", "32"))
        lines.append("")
        return "\n".join(lines)

    if diff.added:
        w = max(display_width(n) for n in diff.added)
        lines.append("  " + bold("新增列（建议确认来源）"))
        for name in diff.added:
            lines.append("    + " + tint(pad(name, w), "32"))
        lines.append("")

    if diff.removed:
        w = max(display_width(n) for n in diff.removed)
        lines.append("  " + bold("删除列（下游若有引用会报错）"))
        for name in diff.removed:
            lines.append("    - " + tint(pad(name, w), "31"))
        lines.append("")

    if diff.renamed:
        w = max(display_width(f"{o} -> {n}") for o, n in diff.renamed)
        lines.append("  " + bold("疑似重命名（类型一致，名称相近）"))
        for old, new in diff.renamed:
            lines.append("    ~ " + pad(f"{old} -> {new}", w))
        lines.append("")

    breaking = [(n, o, nw) for (n, o, nw, b) in diff.type_changes if b]
    benign = [(n, o, nw) for (n, o, nw, b) in diff.type_changes if not b]
    if breaking:
        w = max(display_width(n) for n, _, _ in breaking)
        lines.append("  " + bold("类型变更 · 破坏性（下游会报错或算错）"))
        for name, o, nw in breaking:
            lines.append("    ! " + pad(name, w) + "  " + tint(f"{o} -> {nw}", "31"))
        lines.append("")
    if benign:
        w = max(display_width(n) for n, _, _ in benign)
        lines.append("  " + bold("类型变更 · 兼容（仍可读）"))
        for name, o, nw in benign:
            lines.append("    ~ " + pad(name, w) + "  " + dim(f"{o} -> {nw}"))
        lines.append("")

    if diff.type_inconsistencies:
        w = max(display_width(n) for n, _, _ in diff.type_inconsistencies)
        lines.append("  " + bold("类型污染（混入无法解析的值）"))
        for name, o, nw in diff.type_inconsistencies:
            lines.append("    ! " + pad(name, w) + "  " + tint(f"{o:.1%} -> {nw:.1%}", "31"))
        lines.append("")

    if diff.quality_regressions:
        w = max(display_width(n) for n, _, _ in diff.quality_regressions)
        lines.append("  " + bold("质量退化（缺失率上升）"))
        for name, o, nw in diff.quality_regressions:
            lines.append("    ↓ " + pad(name, w) + "  " + f"{o:.1%} -> {nw:.1%}")
        lines.append("")

    if diff.distribution_shifts:
        w = max(display_width(s.column) for s in diff.distribution_shifts)
        lines.append("  " + bold("分布漂移（数值中心明显移动）"))
        for shift in diff.distribution_shifts:
            # A relative move under 1% rounds to "+0%" and tells the reader
            # nothing; the effect size is the useful number in that case.
            if shift.relative is not None and abs(shift.relative) >= 0.01:
                move = f"{shift.relative:+.0%}"
            elif math.isfinite(shift.effect):
                move = f"{shift.effect:.1f}σ"
            else:
                move = "∞"
            lines.append(
                "    ≠ " + pad(shift.column, w)
                + "  均值 " + f"{shift.old_mean:,.2f} -> {shift.new_mean:,.2f}"
                + "  " + tint(f"({move})", "33")
            )
        lines.append("")

    if diff.row_diff is not None and (diff.row_diff.old_only or diff.row_diff.new_only):
        rd = diff.row_diff
        lines.append("  " + bold("行级差异") + dim(
            f"  按共有的 {len(rd.shared_columns)} 列比对（不同行，非行数）"))
        if rd.new_only:
            lines.append("    + " + tint(f"{rd.new_only:,}", "32") + " 行只出现在新文件")
        if rd.old_only:
            lines.append("    - " + tint(f"{rd.old_only:,}", "31") + " 行只出现在旧文件")
        lines.append("      " + dim(f"共有 {rd.common:,} 行"))
        if not rd.exact:
            # Truncating both sides can only shrink the measured intersection,
            # so the two differences are overestimates, not underestimates.
            lines.append("      " + dim(
                f"只比对了前 {ROW_DIFF_CAP:,} 个不同的行，差异数是上限"))
        lines.append("")

    verdict = "破坏性变更" if diff.breaking else "非破坏性变更"
    code = "31" if diff.breaking else "33"
    lines.append("  " + tint(f"结论：{verdict}。" + ("--strict 下也会失败。" if strict and not diff.breaking else ""), code))
    lines.append("")
    return "\n".join(lines)


def render_diff_json(diff: SchemaDiff) -> str:
    payload = {
        "old_path": diff.old_path,
        "new_path": diff.new_path,
        "old_rows": diff.old_rows,
        "new_rows": diff.new_rows,
        "old_encoding": diff.old_encoding,
        "new_encoding": diff.new_encoding,
        "old_cols": diff.old_cols,
        "new_cols": diff.new_cols,
        "drift": diff.drift,
        "breaking": diff.breaking,
        "added": diff.added,
        "removed": diff.removed,
        "renamed": [{"from": o, "to": n} for o, n in diff.renamed],
        "type_changes": [
            {"column": n, "old": o, "new": nw, "breaking": b}
            for (n, o, nw, b) in diff.type_changes
        ],
        "type_inconsistencies": [
            {"column": n, "old_mismatch_rate": round(o, 4), "new_mismatch_rate": round(nw, 4)}
            for (n, o, nw) in diff.type_inconsistencies
        ],
        "quality_regressions": [
            {"column": n, "old_missing_rate": round(o, 4), "new_missing_rate": round(nw, 4)}
            for (n, o, nw) in diff.quality_regressions
        ],
        "distribution_shifts": [
            {
                "column": s.column,
                "old_mean": s.old_mean, "new_mean": s.new_mean,
                "old_median": s.old_median, "new_median": s.new_median,
                "effect_sigma": round(s.effect, 3) if math.isfinite(s.effect) else None,
                "relative_change": round(s.relative, 4) if s.relative is not None else None,
            }
            for s in diff.distribution_shifts
        ],
        "row_diff": None if diff.row_diff is None else {
            "old_only": diff.row_diff.old_only,
            "new_only": diff.row_diff.new_only,
            "common": diff.row_diff.common,
            "old_distinct": diff.row_diff.old_distinct,
            "new_distinct": diff.row_diff.new_distinct,
            "shared_columns": diff.row_diff.shared_columns,
            "exact": diff.row_diff.exact,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_diff_html(diff: SchemaDiff) -> str:
    """A shareable HTML version of the drift report.

    Same shell as the profile report, but the content is a list of changes
    rather than a list of columns — the reader wants to know what moved, not
    what the data looks like.
    """
    old_name = Path(diff.old_path).name
    new_name = Path(diff.new_path).name

    if diff.breaking:
        verdict, verdict_cls = "破坏性变更", "breaking"
    elif diff.drift:
        verdict, verdict_cls = "检测到漂移", "drift"
    else:
        verdict, verdict_cls = "未发现漂移", "clean"

    delta = diff.new_rows - diff.old_rows
    sign = "+" if delta >= 0 else ""
    facts = [
        f'<div>行数 <b>{diff.old_rows:,} → {diff.new_rows:,}</b> ({sign}{delta:,})</div>',
        f'<div>列数 <b>{diff.old_cols} → {diff.new_cols}</b></div>',
    ]
    if diff.old_encoding != diff.new_encoding:
        facts.append(
            f'<div>编码 <b>{_esc(diff.old_encoding)} → {_esc(diff.new_encoding)}</b></div>')

    sections: list[str] = []

    def add_section(title: str, body: str) -> None:
        sections.append(
            f'<div class="sec"><h3>{title}</h3><div class="rows">{body}</div></div>')

    def simple_rows(mark: str, mark_cls: str, names: list[str], why: str) -> str:
        return "".join(
            f'<div class="row"><span class="mark {mark_cls}">{mark}</span>'
            f'<span class="name">{_esc(n)}</span>'
            f'<span class="why">{_esc(why)}</span></div>'
            for n in names
        )

    if diff.added:
        add_section("新增列", simple_rows("+", "add", diff.added, "确认来源"))

    if diff.removed:
        add_section("删除列", simple_rows("-", "del", diff.removed, "下游按名取值会报错"))

    if diff.renamed:
        add_section("疑似重命名", "".join(
            f'<div class="row"><span class="mark ren">~</span>'
            f'<span class="name">{_esc(o)} → {_esc(n)}</span>'
            f'<span class="why">类型一致，名称相近</span></div>'
            for o, n in diff.renamed))

    breaking_changes = [(n, o, nw) for (n, o, nw, b) in diff.type_changes if b]
    benign_changes = [(n, o, nw) for (n, o, nw, b) in diff.type_changes if not b]
    if breaking_changes:
        add_section("类型变更 · 破坏性", "".join(
            f'<div class="row"><span class="mark bad">!</span>'
            f'<span class="name">{_esc(n)}</span>'
            f'<span class="why">{_esc(o)} → {_esc(nw)}</span></div>'
            for n, o, nw in breaking_changes))
    if benign_changes:
        add_section("类型变更 · 兼容", "".join(
            f'<div class="row"><span class="mark ren">~</span>'
            f'<span class="name">{_esc(n)}</span>'
            f'<span class="why">{_esc(o)} → {_esc(nw)}</span></div>'
            for n, o, nw in benign_changes))

    if diff.type_inconsistencies:
        add_section("类型污染", "".join(
            f'<div class="row"><span class="mark bad">!</span>'
            f'<span class="name">{_esc(n)}</span>'
            f'<span class="why">{o:.1%} → {nw:.1%} 无法解析</span></div>'
            for n, o, nw in diff.type_inconsistencies))

    if diff.quality_regressions:
        add_section("质量退化", "".join(
            f'<div class="row"><span class="mark bad">↓</span>'
            f'<span class="name">{_esc(n)}</span>'
            f'<span class="why">缺失 {o:.1%} → {nw:.1%}</span></div>'
            for n, o, nw in diff.quality_regressions))

    if diff.distribution_shifts:
        blocks = []
        for shift in diff.distribution_shifts:
            if shift.relative is not None and abs(shift.relative) >= 0.01:
                move = f"{shift.relative:+.0%}"
            elif math.isfinite(shift.effect):
                move = f"{shift.effect:.1f}σ"
            else:
                move = "∞"
            peak = max(abs(shift.old_mean), abs(shift.new_mean)) or 1.0
            old_w = abs(shift.old_mean) / peak * 100
            new_w = abs(shift.new_mean) / peak * 100
            blocks.append(
                f'<div class="shift"><div class="shift-top">'
                f'<span class="name">{_esc(shift.column)}</span>'
                f'<span class="move">{move}</span></div>'
                f'<div class="bars">'
                f'<div class="barline"><span class="val">{shift.old_mean:,.2f}</span>'
                f'<span class="track"><span class="fill old" style="width:{old_w:.1f}%"></span></span></div>'
                f'<div class="barline"><span class="val">{shift.new_mean:,.2f}</span>'
                f'<span class="track"><span class="fill new" style="width:{new_w:.1f}%"></span></span></div>'
                f'</div></div>'
            )
        add_section("分布漂移（均值）", "".join(blocks))

    if diff.row_diff is not None and (diff.row_diff.old_only or diff.row_diff.new_only):
        rd = diff.row_diff
        body = (
            f'<div class="row"><span class="mark add">+</span>'
            f'<span class="name">{rd.new_only:,} 行</span>'
            f'<span class="why">只出现在新文件</span></div>'
            f'<div class="row"><span class="mark del">-</span>'
            f'<span class="name">{rd.old_only:,} 行</span>'
            f'<span class="why">只出现在旧文件</span></div>'
            f'<div class="row"><span class="mark ren">=</span>'
            f'<span class="name">{rd.common:,} 行</span>'
            f'<span class="why">两边都有</span></div>'
        )
        add_section(f"行级差异 · 按共有的 {len(rd.shared_columns)} 列比对", body)

    body_html = "".join(sections) if sections else (
        '<p class="empty-note">两份文件没有差异。</p>')

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dataloupe · 漂移对比 · {_esc(new_name)}</title>
<style>{HTML_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <div class="brand">dataloupe · 漂移对比</div>
    <h1>{_esc(new_name)}</h1>
    <div class="meta">{_esc(old_name)} → {_esc(new_name)}</div>
  </div>
  <div><span class="verdict {verdict_cls}">{verdict}</span></div>
</header>
<div class="facts">{"".join(facts)}</div>
{body_html}
<footer>
  <span>dataloupe v{__version__} · 零依赖数据体检</span>
  <span>{_esc(diff.new_path)}</span>
</footer>
</div>
</body>
</html>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataloupe",
        description="零依赖的数据文件体检工具 / zero-dependency data file profiler",
        epilog="示例 / examples:\n"
               "  dataloupe data.csv\n"
               "  dataloupe data.csv -o report.html\n"
               "  dataloupe data.xlsx --json\n"
               "  dataloupe data.csv --sql postgres\n"
               "  dataloupe users.csv --mask\n"
               "  dataloupe big.csv --limit 100000\n"
               "  dataloupe old.csv new.csv --diff",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("files", nargs="+", help="要分析的文件；配合 --diff 时给两个（旧文件 新文件）")
    parser.add_argument("-o", "--output", metavar="PATH",
                        help="HTML 报告路径；配合 --diff 时是漂移报告，"
                             "配合 --fix / --mask 时是输出数据路径")
    parser.add_argument("--fix", action="store_true",
                        help="清洗数据并写出新文件（默认 <原名>.cleaned.csv）")
    parser.add_argument("--dedupe", action="store_true",
                        help="配合 --fix：删除完全重复的行")
    parser.add_argument("--drop-empty-cols", action="store_true",
                        help="配合 --fix：删除整列为空的列")
    parser.add_argument("--no-bom", action="store_true",
                        help="配合 --fix：输出不带 BOM（默认带，方便 Excel 直接打开）")
    parser.add_argument("--keep-headers", action="store_true",
                        help="配合 --fix：不重命名空列名和重复列名")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出 JSON（便于脚本处理）")
    parser.add_argument("--sql", metavar="DIALECT", choices=sorted(SQL_TYPES),
                        help="按推断出的类型输出 CREATE TABLE 语句（postgres/mysql/sqlite）")
    parser.add_argument("--table", metavar="NAME", default=None,
                        help="配合 --sql：表名（默认取文件名）")
    parser.add_argument("--preview", action="store_true",
                        help="终端里额外打印前 20 行数据")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="最多读取 N 行（大文件快速抽样）")
    parser.add_argument("--fail-under", type=int, default=None, metavar="SCORE",
                        help="评分低于 SCORE 时以退出码 1 结束（配合 CI 用）")
    parser.add_argument("--diff", action="store_true",
                        help="对比两个文件的结构、分布与行级漂移（需两个文件）")
    parser.add_argument("--strict", action="store_true",
                        help="配合 --diff：连新增列、疑似重命名、兼容类型变更也判为失败")
    parser.add_argument("--no-rows", action="store_true",
                        help="配合 --diff：跳过行级差异（大文件可省一次扫描）")
    parser.add_argument("--mask", action="store_true",
                        help="输出脱敏副本，把邮箱/手机号/身份证号等替换成掩码"
                             "（默认 <原名>.masked.csv）")
    parser.add_argument("--mask-all", action="store_true",
                        help="配合 --mask：连裸数字形式的银行卡号也脱敏（更激进）")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    parser.add_argument("--version", action="version", version=f"dataloupe {__version__}")
    return parser


def _write_or_report(target: Path, write) -> bool:
    """Run a writer, turning a bad path into a message instead of a traceback.

    Creates missing parent directories first: if someone types
    ``-o reports/march/data.html`` they mean it, and failing on the folder
    alone is not useful.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target)
    except OSError as exc:
        print(f"错误：写不进 {target}（{exc.strerror or exc}）", file=sys.stderr)
        return False
    return True


def _run_diff(args: argparse.Namespace) -> int:
    """Compare two exports and report what drifted. See :func:`diff_profiles`."""
    if len(args.files) != 2:
        print("错误：--diff 需要两个文件（旧文件 新文件）", file=sys.stderr)
        return 2
    if args.fix or args.sql or args.mask:
        print("错误：--diff 不支持 --fix / --sql / --mask", file=sys.stderr)
        return 2

    paths = [Path(p).expanduser() for p in args.files]
    profiles = []
    for path in paths:
        try:
            profiles.append(analyse(path, limit=args.limit))
        except FileNotFoundError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130

    old_profile, new_profile = profiles
    diff = diff_profiles(old_profile, new_profile)

    if not args.no_rows:
        shared = [c.name for c in new_profile.columns
                  if c.name in {o.name for o in old_profile.columns}]
        try:
            diff.row_diff = compare_rows(paths[0], paths[1], shared, limit=args.limit)
            # Rows appearing is normal for an incremental export; rows that
            # vanished from a snapshot are not. Only the second counts as drift.
            if diff.row_diff.old_only:
                diff.drift = True
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            # The row diff is an extra; failing to compute it must not take the
            # structural answer down with it.
            print(f"提示：行级差异计算失败（{exc}）", file=sys.stderr)

    if args.output:
        target = Path(args.output).expanduser()
        if not _write_or_report(
            target, lambda t: t.write_text(render_diff_html(diff), encoding="utf-8")
        ):
            return 2
        print(f"  HTML 漂移报告已写入 {target}\n")

    if args.as_json:
        print(render_diff_json(diff))
    elif not args.output:
        print(render_diff_terminal(diff, strict=args.strict))

    # A breaking change fails the build by default; --strict also fails on any
    # drift at all (added columns, renames, compatible type widening).
    failed = diff.breaking or (args.strict and diff.drift)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)

    global USE_COLOR
    if args.no_color:
        USE_COLOR = False
    elif os.name == "nt":
        # Turn on ANSI escape processing for classic conhost.
        os.system("")

    if args.diff:
        return _run_diff(args)

    if len(args.files) != 1:
        print("错误：单文件模式只接受一个文件（对比两个文件请用 --diff）", file=sys.stderr)
        return 2

    path = Path(args.files[0]).expanduser()

    try:
        profile = analyse(path, limit=args.limit)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    # Exit code reflects data health: 0 clean, 1 has high-severity issues or
    # scored below --fail-under. --json honours it too, because that is the
    # form a CI gate consumes.
    failed = bool(profile.issues_by("high"))
    if args.fail_under is not None and profile.score < args.fail_under:
        failed = True

    if args.as_json:
        print(render_json(profile))
        return 1 if failed else 0

    if args.sql:
        # A code generator: dirty data does not stop the schema from existing.
        print(render_sql(profile, args.sql, args.table))
        return 0

    print(render_terminal(profile, show_preview=args.preview))

    if args.fix or args.mask:
        source = load_source(path, limit=args.limit)
        header, rows = source.header, source.rows

        fix_report = None
        if args.fix:
            header, rows, fix_report = clean_table(
                header,
                rows,
                dedupe=args.dedupe,
                drop_empty_cols=args.drop_empty_cols,
                fix_headers=not args.keep_headers,
            )

        mask_report = None
        if args.mask:
            flagged = {c.name: set(c.pii) for c in profile.columns if c.pii}
            header, rows, mask_report = mask_table(
                header, rows, flagged, allow_all_cards=args.mask_all,
            )

        suffix = ".masked.csv" if args.mask else ".cleaned.csv"
        target = (Path(args.output).expanduser() if args.output
                  else path.with_name(f"{path.stem}{suffix}"))
        # The one promise these commands make is that the source survives.
        # Typing the input path into -o would quietly break it.
        try:
            same_file = target.resolve() == path.resolve()
        except OSError:
            same_file = False
        if same_file:
            print(f"错误：输出路径和输入是同一个文件（{target}）\n"
                  f"      不覆盖原文件，请换一个 -o 路径", file=sys.stderr)
            return 2
        if not _write_or_report(target, lambda t: write_clean_csv(t, header, rows, bom=not args.no_bom)):
            return 2

        if fix_report is not None:
            fix_report.output = target
            print(render_fix_report(fix_report))
        if mask_report is not None:
            mask_report.output = target
            print(render_mask_report(mask_report))
        return 0

    if args.output:
        target = Path(args.output).expanduser()
        if not _write_or_report(target, lambda t: t.write_text(render_html(profile), encoding="utf-8")):
            return 2
        print(f"  HTML 报告已写入 {target}\n")
    else:
        print(dim("  -o report.html 生成网页报告  ·  --fix 清洗数据\n"))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
