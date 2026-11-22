"""
A PDF's pages as text a model can read without guessing - tables kept as tables, Kruti Dev Hindi turned into Hindi.

Plain text extraction flattens a fee table into "101 Arts (B.A.) 3 6 4000/- 10+2 H", and the model has to guess which
number is the duration and which the fee. Here each table row is written with its own column names:
"Prog. Code: 101 | Name of the programme: Arts (B.A.) | Duration (years) Min.: 3 | ... | Admission Fee: 4000/-", and a
table that runs onto the next page keeps the header it started with. Text in a Kruti Dev font is converted
(shared/ai/krutidev.py). The rows are also returned on their own, so what the model wrote can be checked against them.

Uses pdfplumber for the layout (where each table cell is). Any failure returns None and the caller falls back to plain
text, so a PDF this cannot lay out is still read.
"""

import io
import re
from dataclasses import dataclass, field

from shared.ai import krutidev
from shared.utils.logging import get_logger

logger = get_logger(__name__)

LINE_GAP = 3  # points: words this close vertically are one line
HEADER_CHARS = 80
# Font symbols (Wingdings bullets) come out as private-use characters.
_PRIVATE = re.compile("[-]")


@dataclass(slots=True)
class Page:
    text: str
    rows: list[str] = field(default_factory=list)  # every table row on the page, written "Column: value | ..."


def _kruti_fonts(pdf) -> set[str]:
    """The fonts whose text is Kruti Dev, judged over the whole document (font names in a PDF are often anonymous)."""
    by_font: dict[str, list[str]] = {}
    for page in pdf.pages:
        for ch in page.chars:
            parts = by_font.setdefault(ch["fontname"], [])
            if len(parts) < 40_000:
                parts.append(ch["text"])
    fonts = set()
    for name, chars in by_font.items():
        if krutidev.looks_like_krutidev("".join(chars)):
            fonts.add(name)
    return fonts


def _lines(region, kruti: set[str]) -> list[tuple[float, str]]:
    """(top, text) per line, Kruti Dev words converted."""
    words = region.extract_words(extra_attrs=["fontname"], keep_blank_chars=False)
    # Group by height first, then read each line left to right: words in two fonts sit a point apart vertically.
    lines: list[tuple[float, list[dict]]] = []
    for w in sorted(words, key=lambda w: w["top"]):
        if lines and abs(lines[-1][0] - w["top"]) <= LINE_GAP:
            lines[-1][1].append(w)
        else:
            lines.append((w["top"], [w]))
    out: list[tuple[float, str]] = []
    for top, line in lines:
        items = [(text, w["fontname"] in kruti) for w in sorted(line, key=lambda w: w["x0"])
                 if (text := _PRIVATE.sub("", w["text"]))]
        if not items:
            continue
        kruti_words = krutidev.convert_line([t for t, k in items if k])
        it = iter(kruti_words)
        out.append((top, " ".join(next(it) if k else t for t, k in items)))
    return out


def _cell_text(page, bbox, kruti: set[str]) -> str:
    try:
        region = page.within_bbox(bbox)
    except ValueError:
        return ""
    return " ".join(text for _, text in _lines(region, kruti)).strip()


_NUMBER = re.compile(r"[\d.,/\- ]*\d[\d.,/\- ]*")


def _has_digit(cells: list[str | None]) -> bool:
    """A data row: some cell is a plain number (a code, a duration, a fee) - a header like "Paper No. 0" is not."""
    return any(c and _NUMBER.fullmatch(c.strip()) for c in cells)


def _filled(cells: list[str | None]) -> list[str]:
    return [c for c in cells if c]


def _headers(grid: list[list[str | None]]) -> tuple[list[str], int]:
    """Column names from the header row (and a second header row such as "Min. | Max."), and how many rows they take."""
    width = len(grid[0])
    names: list[str] = []
    carried = ""
    for j in range(width):
        value = grid[0][j]
        carried = value if value else carried
        names.append(carried)
    used = 1
    while used < len(grid):
        row = grid[used]
        if _has_digit(row) or len(_filled(row)) < 2:
            break
        names = [f"{n} {row[j]}".strip() if row[j] else n for j, n in enumerate(names)]
        used += 1
    return [n[:HEADER_CHARS] for n in names], used


def _render_table(grid: list[list[str | None]], carried: list[str] | None) -> tuple[list[str], list[str]]:
    """
    The table's lines and its column names. `carried` is the header of a table that ran off the previous page; it is
    used when this table, the first on its page, has as many columns and does not start with a header of its own.
    """
    width = len(grid[0])
    first = grid[0]
    # The rest of a table: its first row is data, or a part-row (a cell running over from the last page) - a new
    # table's header fills most of its cells.
    if carried is not None and len(carried) == width and (_has_digit(first) or len(_filled(first)) * 2 < width):
        names, start = carried, 0
    else:
        names, start = _headers(grid)
    lines: list[str] = []
    for row in grid[start:]:
        filled = _filled(row)
        if not filled:
            continue
        if len(filled) == 1 and not _has_digit(row):
            lines.append(f"-- {filled[0]} --")  # a section heading inside the table ("Graduation")
            continue
        cells = [f"{names[j] or 'Column ' + str(j + 1)}: {value}" for j, value in enumerate(row) if value]
        lines.append(" | ".join(cells))
    return lines, names


def read(raw: bytes, max_pages: int) -> list[Page] | None:
    """Each page as text with its tables laid out; None when the PDF cannot be laid out here."""
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            pdf_pages = pdf.pages[:max_pages]
            kruti = _kruti_fonts(pdf)
            pages: list[Page] = []
            carried: list[str] | None = None
            for page in pdf_pages:
                tables = page.find_tables()
                boxes = [t.bbox for t in tables]

                def outside(obj, boxes=boxes) -> bool:
                    if obj.get("object_type") != "char":
                        return True
                    return not any(b[0] <= obj["x0"] and obj["x1"] <= b[2] and b[1] <= obj["top"] and obj["bottom"] <= b[3] for b in boxes)

                blocks: list[tuple[float, str]] = _lines(page.filter(outside), kruti)
                rows: list[str] = []
                for index, table in enumerate(tables):
                    # Only the page's first table, starting near the top, can be the rest of the last page's table.
                    if index > 0 or table.bbox[1] > page.height * 0.25:
                        carried = None
                    grid = [[_cell_text(page, cell, kruti) or None if cell else None for cell in row.cells] for row in table.rows]
                    grid = [r for r in grid if any(r)]
                    if not grid or len(grid[0]) < 2:
                        blocks.extend((table.bbox[1], c) for r in grid for c in _filled(r))
                        continue
                    lines, carried = _render_table(grid, carried)
                    if not lines:
                        continue
                    rows.extend(line for line in lines if not line.startswith("-- "))
                    blocks.append((table.bbox[1], "[Table]\n" + "\n".join(lines) + "\n[End of table]"))
                if not tables:
                    carried = None
                blocks.sort(key=lambda b: b[0])
                pages.append(Page(text="\n".join(text for _, text in blocks).strip(), rows=rows))
            if kruti:
                logger.info("pdf laid out with Kruti Dev converted, fonts=%s", len(kruti))
            return pages
    except Exception as exc:  # noqa: BLE001 - plain text extraction is the fallback
        logger.warning("pdf layout failed (%s), reading plain text", type(exc).__name__)
        return None
