"""
Reads a lead export file (CSV or Excel) into plain rows, with lowercased
headers, so the shared lead parser can map them.

The file readers are the same ones the receivables import uses.
"""

from shared.integrations.receivables import _read_csv, _read_xlsx


def parse_lead_file(filename: str, content: bytes) -> list[dict]:
    headers, records = _read_xlsx(content) if filename.lower().endswith(".xlsx") else _read_csv(content)
    rows: list[dict] = []
    for record in records:
        row = {
            (key or "").strip().lower(): (str(value).strip() if value is not None else "")
            for key, value in record.items()
            if key
        }
        if any(v for v in row.values()):
            rows.append(row)
    return rows
