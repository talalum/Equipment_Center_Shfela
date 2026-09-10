"""
Reading an Excel (.xlsx) sheet, with the standard library only.

An xlsx file is a zip holding XML parts. Only what the import actually needs is
read here — the first worksheet, its header row and the cells under it — and the
result is the same list of dictionaries that `csv.DictReader` produces. That way
the importer never has to know which of the two formats it was handed.

Not supported, on purpose: the old binary .xls (a completely different format,
unreadable without an external library), formulas (the value last calculated by
Excel is read, which is what the file shows on screen), and any sheet other than
the first one.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

#: Ceiling on the *uncompressed* size of the parts that are read. A zip
#: decompresses to far more than it weighs, so the upload limit alone does not
#: bound the memory this costs.
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
#: Ceiling on the number of data rows read from the sheet.
MAX_ROWS = 20_000

_COLUMN_LETTERS = re.compile(r"[A-Z]+")


class SpreadsheetError(Exception):
    """The file is not a readable xlsx. The message is in English — the interface wording is the importer's job."""


def looks_like_xlsx(content: bytes) -> bool:
    """Whether the content is a zip, which every xlsx is. Cheap check before parsing."""
    return content[:2] == b"PK"


def _column_index(reference: str) -> int:
    """'A1' -> 0, 'B7' -> 1, 'AA3' -> 26. The row number carries no information here."""
    letters = _COLUMN_LETTERS.match(reference or "")
    if not letters:
        return -1
    index = 0
    for char in letters.group(0):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _number_text(raw: str) -> str:
    """
    Excel stores every number as a float, so a SKU comes out as "1101.0". The
    trailing .0 has to go before `normalize_sku` sees it — it strips dots, and
    would turn 1101.0 into 11010.
    """
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return raw
    return str(int(number)) if number.is_integer() else repr(number)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    # A single string may be split across several runs when part of it is
    # formatted differently; joining every <t> puts it back together.
    return ["".join(t.text or "" for t in si.iter(f"{MAIN_NS}t")) for si in root.findall(f"{MAIN_NS}si")]


def _first_sheet_path(archive: zipfile.ZipFile) -> str:
    """
    The path of the first sheet, resolved through the relationship id, because
    the file name is not necessarily sheet1.xml and the order in the zip means
    nothing.
    """
    names = archive.namelist()
    if "xl/workbook.xml" not in names:
        raise SpreadsheetError("not an xlsx file: xl/workbook.xml is missing")

    sheets = ET.fromstring(archive.read("xl/workbook.xml")).iter(f"{MAIN_NS}sheet")
    first = next(iter(sheets), None)
    if first is None:
        raise SpreadsheetError("the workbook holds no sheet")

    relationship_id = first.get(f"{REL_NS}id")
    if relationship_id and "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")).iter(f"{PKG_REL_NS}Relationship"):
            if rel.get("Id") != relationship_id:
                continue
            target = rel.get("Target", "")
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if path in names:
                return path

    # A file whose relationships cannot be followed still usually holds the
    # ordinary sheet1.xml, and reading it beats refusing the file.
    fallback = [n for n in names if n.startswith("xl/worksheets/sheet")]
    if not fallback:
        raise SpreadsheetError("no worksheet found in the file")
    return sorted(fallback)[0]


def _check_size(archive: zipfile.ZipFile, *paths: str) -> None:
    total = sum(info.file_size for info in archive.infolist() if info.filename in paths)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise SpreadsheetError(f"the sheet expands to {total} bytes, beyond the {MAX_UNCOMPRESSED_BYTES} allowed")


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{MAIN_NS}t")).strip()
    value = cell.find(f"{MAIN_NS}v")
    if value is None or value.text is None:
        return ""
    text = value.text.strip()
    if kind == "s":
        try:
            return shared[int(text)].strip()
        except (ValueError, IndexError):
            return ""
    if kind == "e":  # #N/A and friends — an error is not a value
        return ""
    if kind == "b":
        return "1" if text == "1" else "0"
    if kind == "str":  # the text result of a formula
        return text
    return _number_text(text)


def read_rows(content: bytes) -> list[dict[str, str]]:
    """
    The first sheet as a list of dictionaries keyed by the header row.

    The header names are stripped of surrounding whitespace — the report carries
    at least one column whose name ends in a space, and nobody should have to
    know that. A row with nothing in it is dropped rather than returned empty,
    and a row shorter than the header gets "" for the columns it lacks.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise SpreadsheetError(f"the file is not a valid xlsx: {exc}") from exc

    with archive:
        sheet_path = _first_sheet_path(archive)
        _check_size(archive, sheet_path, "xl/sharedStrings.xml")
        shared = _shared_strings(archive)
        sheet = ET.fromstring(archive.read(sheet_path))

        header: list[str] = []
        rows: list[dict[str, str]] = []
        for row in sheet.iter(f"{MAIN_NS}row"):
            values: dict[int, str] = {}
            for cell in row.findall(f"{MAIN_NS}c"):
                index = _column_index(cell.get("r", ""))
                text = _cell_text(cell, shared)
                if index >= 0 and text:
                    values[index] = text
            if not values:
                continue

            if not header:
                header = [values.get(i, "") for i in range(max(values) + 1)]
                continue

            rows.append({name: values.get(i, "") for i, name in enumerate(header) if name})
            if len(rows) >= MAX_ROWS:
                break

    if not header:
        raise SpreadsheetError("the sheet is empty — not even a header row")
    return rows
