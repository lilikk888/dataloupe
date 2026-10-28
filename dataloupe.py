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
import io
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

__version__ = "1.0.0"

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
    """Guess a text encoding by BOM first, then by trial decoding.

    Order matters: UTF-8 is strict, GB18030 accepts almost any byte sequence,
    so it must come after UTF-8 but before the legacy single-byte fallbacks.
    """
    with path.open("rb") as fh:
        raw = fh.read(256 * 1024)
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
        return [], []
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


def load_source(path: Path, limit: int | None = None) -> Source:
    """Read any supported file into a uniform header + rows structure."""
    if not path.exists():
        raise FileNotFoundError(f"文件不存在 / file not found: {path}")
    if path.is_dir():
        raise ValueError(f"这是一个文件夹，不是文件 / not a file: {path}")

    size = path.stat().st_size
    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xlsm"):
        header, rows = read_xlsx(path, limit)
        return Source(path, header, rows, "xlsx", "—", "—", size)

    encoding = detect_encoding(path)
    text = path.read_text(encoding=encoding, errors="replace")

    stripped = text.lstrip()
    if suffix in (".json",) or (stripped[:1] in "[{" and suffix not in (".csv", ".tsv")):
        try:
            header, rows = _read_json_array(text, limit)
            return Source(path, header, rows, "json", encoding, "—", size)
        except (json.JSONDecodeError, ValueError):
            pass

    # JSONL: several lines each parsing as a JSON object.
    probe = [ln for ln in stripped.splitlines() if ln.strip()][:3]
    if suffix in (".jsonl", ".ndjson") or (
        len(probe) >= 2 and all(ln.lstrip().startswith("{") for ln in probe)
        and all(_is_json_object(ln) for ln in probe)
    ):
        header, rows = _read_jsonl(text, limit)
        if header:
            return Source(path, header, rows, "jsonl", encoding, "—", size)

    delimiter = "\t" if suffix == ".tsv" else sniff_delimiter(text[:65536])
    fmt = "tsv" if delimiter == "\t" else "csv"

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    raw_rows: list[list[str]] = []
    empty = 0
    try:
        for row in reader:
            # +1 because the header occupies the first row.
            if limit is not None and len(raw_rows) >= limit + 1:
                break
            if not row or all(not cell.strip() for cell in row):
                empty += 1
                continue
            raw_rows.append(row)
    except csv.Error as exc:
        raise ValueError(f"CSV 解析失败 / cannot parse CSV: {exc}") from exc

    if not raw_rows:
        raise ValueError("文件是空的，或者没有可解析的数据 / file is empty")

    header = raw_rows[0]
    body = raw_rows[1:]

    width = len(header)
    ragged = 0
    normalised: list[list[str]] = []
    for row in body:
        if len(row) != width:
            ragged += 1
            if len(row) < width:
                row = row + [""] * (width - len(row))
            else:
                row = row[:width]
        normalised.append(row)

    return Source(path, header, normalised, fmt, encoding, delimiter, size, ragged, empty)


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
        try:
            return datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0),
            )
        except ValueError:
            return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


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

    total = len(sample)
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
    mismatch: int = 0
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
    # text
    top_values: list[tuple[str, int]] = field(default_factory=list)
    min_len: int = 0
    max_len: int = 0
    # dates
    date_min: str | None = None
    date_max: str | None = None
    # hygiene counters
    leading_space: int = 0
    trailing_space: int = 0
    fullwidth: int = 0
    control_chars: int = 0
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
    truncated: bool = False

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


def profile_column(name: str, index: int, values: list[str]) -> ColumnProfile:
    """Compute every statistic for one column."""
    col = ColumnProfile(name=name, index=index, total=len(values))

    non_empty = [v for v in values if not is_missing(v)]
    col.missing = len(values) - len(non_empty)
    col.kind, col.mismatch = infer_kind(non_empty)

    col.distinct = len(set(map(str.strip, non_empty)))

    # One pass over a sample, not four passes over everything.
    for value in non_empty[:HYGIENE_SAMPLE]:
        if value[:1].isspace():
            col.leading_space += 1
        if value[-1:].isspace():
            col.trailing_space += 1
        has_fw = has_ctrl = False
        for ch in value:
            cp = ord(ch)
            if not has_fw and (0xFF01 <= cp <= 0xFF60 or cp == 0x3000 or 0xFFE0 <= cp <= 0xFFE6):
                has_fw = True
            elif not has_ctrl and cp < 32 and ch != "\t":
                has_ctrl = True
            if has_fw and has_ctrl:
                break
        if has_fw:
            col.fullwidth += 1
        if has_ctrl:
            col.control_chars += 1

    if col.kind in ("int", "float"):
        numbers: list[float] = []
        for value in non_empty:
            text = value.strip()
            try:
                numbers.append(float(text))
            except ValueError:
                # Thousands separators are the only common fix-up worth trying.
                if "," in text:
                    try:
                        numbers.append(float(text.replace(",", "")))
                    except ValueError:
                        continue
        numbers = numbers[:MAX_VALUES]
        if numbers:
            col.num_min = min(numbers)
            col.num_max = max(numbers)
            col.num_mean = statistics.fmean(numbers)
            col.num_std = statistics.pstdev(numbers) if len(numbers) > 1 else 0.0
            ordered = sorted(numbers)
            col.num_median = percentile(ordered, 0.5)
            col.num_q1 = percentile(ordered, 0.25)
            col.num_q3 = percentile(ordered, 0.75)
            if col.num_q1 is not None and col.num_q3 is not None:
                iqr = col.num_q3 - col.num_q1
                low_fence = col.num_q1 - 1.5 * iqr
                high_fence = col.num_q3 + 1.5 * iqr
                col.num_outliers = sum(1 for n in numbers if n < low_fence or n > high_fence)
            col.histogram, col.hist_edges = histogram(numbers)

    elif col.kind == "date":
        stamps = [d for d in (parse_date(v) for v in non_empty) if d is not None]
        if stamps:
            col.date_min = min(stamps).strftime("%Y-%m-%d")
            col.date_max = max(stamps).strftime("%Y-%m-%d")

    else:
        lengths = [len(v.strip()) for v in non_empty]
        if lengths:
            col.min_len = min(lengths)
            col.max_len = max(lengths)
        counter: dict[str, int] = {}
        for value in non_empty:
            key = value.strip()
            counter[key] = counter.get(key, 0) + 1
        col.top_values = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]

    return col


# ---------------------------------------------------------------------------
# Quality checks
# ---------------------------------------------------------------------------

def find_issues(profile: Profile) -> list[Issue]:
    """Turn raw statistics into a prioritised, actionable list."""
    issues: list[Issue] = []
    rows = max(profile.rows, 1)

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

    for col in profile.columns:
        label = col.name or f"第{col.index + 1}列"

        if col.kind == "empty" and col.total:
            issues.append(Issue("high", "整列为空", f"「{label}」没有任何数据", column=col.name,
                                suggestion="确认这列是否应该存在"))
            continue

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

        if col.distinct == 1 and col.total > 1:
            issues.append(Issue("low", "常量列", f"「{label}」所有值都一样", column=col.name,
                                suggestion="对分析没有信息量，可考虑移除"))

        if col.kind == "text" and col.total >= 20 and col.distinct_rate >= 0.98:
            issues.append(Issue("low", "疑似 ID 列",
                                f"「{label}」几乎每行都不同（{col.distinct}/{col.total}）",
                                column=col.name, suggestion="大概率是主键或唯一标识，不适合做统计"))

        elif col.kind == "int" and col.total >= 20 and col.missing == 0 and col.distinct_rate >= 0.99:
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

def analyse(path: Path, limit: int | None = None) -> Profile:
    """Read *path* and build a complete Profile."""
    started = time.perf_counter()
    source = load_source(path, limit)

    if not source.header:
        raise ValueError("没有识别到表头 / no header row found")

    # Disambiguate duplicate / empty column names so dict keys stay unique.
    names: list[str] = []
    counts: dict[str, int] = {}
    for idx, raw_name in enumerate(source.header):
        name = raw_name.strip() or f"列{idx + 1}"
        counts[name] = counts.get(name, 0) + 1
        names.append(name if counts[name] == 1 else f"{name}_{counts[name]}")

    columns: list[ColumnProfile] = []
    for idx, name in enumerate(names):
        values = [row[idx] if idx < len(row) else "" for row in source.rows]
        col = profile_column(name, idx, values)
        col.empty_name = not source.header[idx].strip()
        col.duplicate_name = source.header.count(source.header[idx]) > 1
        col.raw_name = source.header[idx].strip()
        columns.append(col)

    # Whole-row duplicates.
    seen_rows: set[tuple[str, ...]] = set()
    duplicates = 0
    for row in source.rows:
        key = tuple(row)
        if key in seen_rows:
            duplicates += 1
        else:
            seen_rows.add(key)

    profile = Profile(
        path=str(path),
        fmt=source.fmt,
        encoding=source.encoding,
        delimiter=source.delimiter,
        size=source.size,
        rows=len(source.rows),
        cols=len(names),
        columns=columns,
        issues=[],
        score=100,
        duplicate_rows=duplicates,
        empty_rows=source.empty_rows,
        ragged_rows=source.ragged_rows,
        preview=source.rows[:PREVIEW_ROWS],
        header=names,
        elapsed=0.0,
        truncated=limit is not None and len(source.rows) >= limit,
    )
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
            f"{col.distinct:,}",
            truncate(column_summary(col), 34),
        ])
    lines.extend("  " + line for line in render_table(headers, rows, ["left", "left", "right", "right", "left"]))
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
    items.append(f"<dt>唯一值</dt><dd>{col.distinct:,}</dd>")

    if col.kind in ("int", "float") and col.num_min is not None:
        items.append(f"<dt>范围</dt><dd>{col.num_min:g} ~ {col.num_max:g}</dd>")
        items.append(f"<dt>均值</dt><dd>{col.num_mean:,.2f}</dd>")
        items.append(f"<dt>中位数</dt><dd>{col.num_median:,.2f}</dd>")
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

    return (
        f'<div class="col"><div class="col-head">'
        f'<span class="col-name" title="{_esc(col.name)}">{_esc(col.name)}</span>'
        f'<span class="kind {_esc(col.kind)}">{_esc(col.kind)}</span></div>'
        f'<dl class="kv">{"".join(items)}</dl>{_sparkline(col)}</div>'
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
                "min": c.num_min, "max": c.num_max, "mean": c.num_mean,
                "median": c.num_median, "std": c.num_std, "outliers": c.num_outliers,
                "date_min": c.date_min, "date_max": c.date_max,
                "top_values": c.top_values,
            }
            for c in profile.columns
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataloupe",
        description="零依赖的数据文件体检工具 / zero-dependency data file profiler",
        epilog="示例 / examples:\n"
               "  dataloupe data.csv\n"
               "  dataloupe data.csv -o report.html\n"
               "  dataloupe data.xlsx --json\n"
               "  dataloupe big.csv --limit 100000",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", help="要分析的文件（csv/tsv/jsonl/json/xlsx）")
    parser.add_argument("-o", "--output", metavar="HTML",
                        help="把 HTML 报告写到这个路径")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出 JSON（便于脚本处理）")
    parser.add_argument("--preview", action="store_true",
                        help="终端里额外打印前 20 行数据")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="最多读取 N 行（大文件快速抽样）")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")
    parser.add_argument("--version", action="version", version=f"dataloupe {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)

    global USE_COLOR
    if args.no_color:
        USE_COLOR = False
    elif os.name == "nt":
        # Turn on ANSI escape processing for classic conhost.
        os.system("")

    path = Path(args.file).expanduser()

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

    if args.as_json:
        print(render_json(profile))
        return 0

    print(render_terminal(profile, show_preview=args.preview))

    if args.output:
        target = Path(args.output).expanduser()
        target.write_text(render_html(profile), encoding="utf-8")
        print(f"  HTML 报告已写入 {target}\n")
    else:
        print(dim("  加 -o report.html 生成可交互的网页报告\n"))

    # Exit code reflects data health: 0 clean, 1 has high-severity issues.
    return 1 if profile.issues_by("high") else 0


if __name__ == "__main__":
    sys.exit(main())
