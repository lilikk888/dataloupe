"""Render real dataloupe output as a terminal-style SVG for the README.

Run once after changing the report layout, then delete.
"""
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dataloupe as dl

OUT = ROOT / "docs" / "terminal.svg"
OUT.parent.mkdir(exist_ok=True)

FONT_SIZE = 13
CHAR_W = 7.82          # width of one monospace cell at FONT_SIZE
LINE_H = 21
PAD_X = 26
PAD_Y = 52
TITLE_H = 34

COLORS = {
    "1": None,          # bold handled separately
    "2": "#7d7d74",
    "31": "#f87171",
    "32": "#4ade80",
    "33": "#fbbf24",
    "36": "#67e8f9",
}
DEFAULT_FG = "#e6e6e1"

ANSI_RE = re.compile(r"\x1b\[([\d;]*)m")


def char_cells(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def parse_ansi(text: str):
    """Split ANSI-coloured text into (line, [(segment, color, bold)])."""
    lines = []
    for raw_line in text.split("\n"):
        segments = []
        color = None
        bold = False
        pos = 0
        for match in ANSI_RE.finditer(raw_line):
            if match.start() > pos:
                segments.append((raw_line[pos:match.start()], color, bold))
            codes = [c for c in match.group(1).split(";") if c]
            for code in codes:
                if code == "0":
                    color, bold = None, False
                elif code == "1":
                    bold = True
                elif code in COLORS:
                    color = COLORS[code]
            pos = match.end()
        if pos < len(raw_line):
            segments.append((raw_line[pos:], color, bold))
        lines.append(segments)
    return lines


def build_svg(lines) -> str:
    max_cells = 0
    for segments in lines:
        cells = sum(char_cells(ch) for seg, _, _ in segments for ch in seg)
        max_cells = max(max_cells, cells)

    width = int(PAD_X * 2 + max_cells * CHAR_W)
    height = int(PAD_Y + len(lines) * LINE_H + 18)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="dataloupe terminal output">',
        f'<rect width="{width}" height="{height}" rx="12" fill="#1b1b19"/>',
        f'<path d="M0 12a12 12 0 0 1 12-12h{width - 24}a12 12 0 0 1 12 12v{TITLE_H - 12}'
        f'H0z" fill="#242422"/>',
        f'<circle cx="24" cy="{TITLE_H / 2 + 1}" r="5.5" fill="#ff5f57"/>',
        f'<circle cx="44" cy="{TITLE_H / 2 + 1}" r="5.5" fill="#febc2e"/>',
        f'<circle cx="64" cy="{TITLE_H / 2 + 1}" r="5.5" fill="#28c840"/>',
        f'<text x="{width / 2}" y="{TITLE_H / 2 + 5}" text-anchor="middle" '
        f'font-family="ui-monospace,Consolas,monospace" font-size="12" '
        f'fill="#8a8a80">dataloupe</text>',
    ]

    for row, segments in enumerate(lines):
        y = PAD_Y + row * LINE_H
        x = PAD_X
        for seg, color, bold in segments:
            if not seg:
                continue
            weight = ' font-weight="600"' if bold else ""
            fill = color or DEFAULT_FG
            # One tspan per character keeps CJK and ASCII on the same grid.
            for ch in seg:
                cells = char_cells(ch)
                if cells == 0:
                    continue
                if ch != " ":
                    parts.append(
                        f'<text x="{x:.1f}" y="{y}" '
                        f"font-family=\"ui-monospace,Consolas,'Microsoft YaHei',monospace\" "
                        f'font-size="{FONT_SIZE}" fill="{fill}"{weight} '
                        f'xml:space="preserve">{escape(ch)}</text>'
                    )
                x += cells * CHAR_W

    parts.append("</svg>")
    return "\n".join(parts)


def escape(ch: str) -> str:
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(ch, ch)


def write(name: str, text: str) -> None:
    target = ROOT / "docs" / name
    target.write_text(build_svg(parse_ansi(text)), encoding="utf-8")
    print(f"wrote {target}  ({target.stat().st_size:,} bytes)")


def main():
    dl.USE_COLOR = True

    profile = dl.analyse(ROOT / "examples" / "users.csv")
    write("terminal.svg", dl.render_terminal(profile, show_preview=False))

    old = dl.analyse(ROOT / "examples" / "orders_march.csv")
    new = dl.analyse(ROOT / "examples" / "orders_april.csv")
    diff = dl.diff_profiles(old, new)
    diff.row_diff = dl.compare_rows(
        ROOT / "examples" / "orders_march.csv",
        ROOT / "examples" / "orders_april.csv",
        [c.name for c in new.columns if c.name in {o.name for o in old.columns}],
    )
    write("diff.svg", dl.render_diff_terminal(diff))

    users_path = ROOT / "examples" / "users.csv"
    users = dl.analyse(users_path)
    results = dl.check_rules(users, users_path, [
        "id:unique",
        "email:not-null",
        "email:matches(^\\S+@\\S+$)",
        "note:in(普通用户|VIP 客户)",
        "rows>=100",
    ])
    write("rules.svg", dl.render_rules(results))


if __name__ == "__main__":
    main()
