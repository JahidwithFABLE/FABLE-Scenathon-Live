from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import copy
import math
import os
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple, Any

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", DOC_REL_NS)


class FableWorkbookError(Exception):
    pass


@dataclass
class CalculatorMeta:
    filename: str
    country: str
    country_id: str
    pathway: str
    iteration: Optional[int]
    trade_adjustment_setting: Optional[str]
    product_count: int
    year_count: int
    record_count: int


@dataclass
class TradeRow:
    country: str
    country_id: str
    pathway: str
    product: str
    year: int
    import_quantity: float
    export_quantity: float
    source_filename: str
    iteration: Optional[int] = None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + ord(ch) - 64
    return n


def _num_to_col(n: int) -> str:
    out = []
    while n:
        n, r = divmod(n - 1, 26)
        out.append(chr(65 + r))
    return "".join(reversed(out))


def _parse_cell_ref(ref: str) -> Tuple[int, int]:
    m = re.fullmatch(r"([A-Z]+)(\d+)", ref.upper())
    if not m:
        raise ValueError(f"Invalid cell reference: {ref}")
    return _col_to_num(m.group(1)), int(m.group(2))


def _parse_range_ref(ref: str) -> Tuple[int, int, int, int]:
    a, b = ref.split(":") if ":" in ref else (ref, ref)
    c1, r1 = _parse_cell_ref(a)
    c2, r2 = _parse_cell_ref(b)
    return c1, r1, c2, r2


def _cell_ref(col: int, row: int) -> str:
    return f"{_num_to_col(col)}{row}"


def _as_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    try:
        return float(s)
    except ValueError as e:
        raise FableWorkbookError(f"Expected a numeric value, got {value!r}") from e


def _as_int(value: Any) -> int:
    n = _as_number(value)
    return int(round(n))


def _guess_iso3(filename: str, country: str) -> str:
    stem = Path(filename).stem
    # Strong filename patterns first (e.g. _ARG_, -ARG-, ...)
    candidates = re.findall(r"(?:^|[_\- ])([A-Z]{3})(?=$|[_\- ])", stem.upper())
    if candidates:
        return candidates[-1]
    # Minimal alias map covering common FABLE spellings; filename remains fallback.
    aliases = {
        "argentina": "ARG", "uzbekistan": "UZB", "france": "FRA", "brazil": "BRA",
        "india": "IND", "indonesia": "IDN", "china": "CHN", "denmark": "DNK",
        "ethiopia": "ETH", "guatemala": "GTM", "vietnam": "VNM", "viet nam": "VNM",
        "united kingdom": "GBR", "united states": "USA", "united states of america": "USA",
        "côte d'ivoire": "CIV", "cote d'ivoire": "CIV", "russian federation": "RUS",
        "south korea": "KOR", "republic of korea": "KOR", "iran": "IRN",
        "bolivia": "BOL", "tanzania": "TZA", "venezuela": "VEN",
    }
    key = country.strip().lower()
    if key in aliases:
        return aliases[key]
    # Stable, human-readable fallback; the engine can still group correctly.
    cleaned = re.sub(r"[^A-Z]", "", country.upper())
    return (cleaned[:3] or "UNK").ljust(3, "X")


def _guess_iteration(filename: str) -> Optional[int]:
    m = re.search(r"iteration[_\- ]?(\d+)", filename, flags=re.I)
    return int(m.group(1)) if m else None


class XlsxPackage:
    """Small OOXML helper focused on FABLE-C tables.

    It intentionally edits the XLSX package directly so formulas, formatting,
    hidden sheets, and workbook structure are preserved instead of rebuilding
    the workbook from scratch.
    """

    def __init__(self, source: bytes | str | Path):
        if isinstance(source, (str, Path)):
            self.filename = Path(source).name
            self._bytes = Path(source).read_bytes()
        else:
            self.filename = "uploaded.xlsx"
            self._bytes = source
        self._zip = zipfile.ZipFile(BytesIO(self._bytes), "r")
        self._names = set(self._zip.namelist())
        self._shared_strings = self._load_shared_strings()
        self._sheet_name_to_path = self._load_sheet_map()
        self._table_map = self._load_table_map()
        self._sheet_cache: Dict[str, ET.Element] = {}
        self._cell_index: Dict[int, Dict[str, ET.Element]] = {}
        self._modified_xml: Dict[str, bytes] = {}

    def close(self) -> None:
        self._zip.close()

    def _load_shared_strings(self) -> List[str]:
        if "xl/sharedStrings.xml" not in self._names:
            return []
        root = ET.fromstring(self._zip.read("xl/sharedStrings.xml"))
        strings: List[str] = []
        for si in root.findall(f"{{{MAIN_NS}}}si"):
            strings.append("".join((t.text or "") for t in si.iter(f"{{{MAIN_NS}}}t")))
        return strings

    def _load_sheet_map(self) -> Dict[str, str]:
        ns = {"m": MAIN_NS, "r": DOC_REL_NS}
        wb = ET.fromstring(self._zip.read("xl/workbook.xml"))
        rels = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        out: Dict[str, str] = {}
        for s in wb.find("m:sheets", ns):
            rid = s.attrib[f"{{{DOC_REL_NS}}}id"]
            target = rel_map[rid]
            out[s.attrib["name"]] = posixpath.normpath(posixpath.join("xl", target))
        return out

    def _load_table_map(self) -> Dict[str, Dict[str, str]]:
        # table XML itself provides the name/ref; worksheet relationships tell us its sheet.
        table_to_sheet: Dict[str, str] = {}
        for sheet_path in self._sheet_name_to_path.values():
            rel_path = posixpath.join(
                posixpath.dirname(sheet_path), "_rels", posixpath.basename(sheet_path) + ".rels"
            )
            if rel_path not in self._names:
                continue
            rels = ET.fromstring(self._zip.read(rel_path))
            for rel in rels:
                if rel.attrib.get("Type", "").endswith("/table"):
                    target = posixpath.normpath(
                        posixpath.join(posixpath.dirname(sheet_path), rel.attrib["Target"])
                    )
                    table_to_sheet[target] = sheet_path

        out: Dict[str, Dict[str, str]] = {}
        for name in self._names:
            if not (name.startswith("xl/tables/table") and name.endswith(".xml")):
                continue
            root = ET.fromstring(self._zip.read(name))
            table_name = root.attrib.get("displayName") or root.attrib.get("name")
            if not table_name:
                continue
            out[table_name.lower()] = {
                "name": table_name,
                "ref": root.attrib["ref"],
                "table_path": name,
                "sheet_path": table_to_sheet.get(name, ""),
            }
        return out

    def table_info(self, name: str) -> Dict[str, str]:
        info = self._table_map.get(name.lower())
        if not info or not info.get("sheet_path"):
            raise FableWorkbookError(f"Required Excel table {name!r} was not found")
        return info

    def _sheet_root(self, sheet_path: str) -> ET.Element:
        if sheet_path not in self._sheet_cache:
            data = self._modified_xml.get(sheet_path) or self._zip.read(sheet_path)
            self._sheet_cache[sheet_path] = ET.fromstring(data)
        return self._sheet_cache[sheet_path]

    def _find_cell(self, sheet_root: ET.Element, ref: str) -> Optional[ET.Element]:
        key = id(sheet_root)
        idx = self._cell_index.get(key)
        if idx is None:
            idx = {c.attrib.get("r"): c for c in sheet_root.iter(f"{{{MAIN_NS}}}c") if c.attrib.get("r")}
            self._cell_index[key] = idx
        return idx.get(ref)

    def _cell_value(self, cell: Optional[ET.Element]) -> Any:
        if cell is None:
            return None
        t = cell.attrib.get("t")
        v = cell.find(f"{{{MAIN_NS}}}v")
        if t == "inlineStr":
            return "".join((x.text or "") for x in cell.iter(f"{{{MAIN_NS}}}t"))
        if v is None:
            return None
        text = v.text or ""
        if t == "s":
            try:
                return self._shared_strings[int(text)]
            except Exception:
                return text
        if t in ("str", "e"):
            return text
        if t == "b":
            return text == "1"
        try:
            n = float(text)
            return int(n) if n.is_integer() else n
        except ValueError:
            return text

    def read_table(self, name: str) -> List[Dict[str, Any]]:
        info = self.table_info(name)
        c1, r1, c2, r2 = _parse_range_ref(info["ref"])
        root = self._sheet_root(info["sheet_path"])
        headers: List[str] = []
        for col in range(c1, c2 + 1):
            val = self._cell_value(self._find_cell(root, _cell_ref(col, r1)))
            headers.append(str(val).strip() if val is not None else f"COL_{col}")
        rows: List[Dict[str, Any]] = []
        for row in range(r1 + 1, r2 + 1):
            record = {}
            has_value = False
            for idx, col in enumerate(range(c1, c2 + 1)):
                value = self._cell_value(self._find_cell(root, _cell_ref(col, row)))
                record[headers[idx]] = value
                if value not in (None, ""):
                    has_value = True
            if has_value:
                rows.append(record)
        return rows

    def _write_inline_string(self, cell: ET.Element, value: str) -> None:
        # Preserve style and reference, replace only the content payload.
        for child in list(cell):
            cell.remove(child)
        cell.attrib["t"] = "inlineStr"
        is_node = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
        t_node = ET.SubElement(is_node, f"{{{MAIN_NS}}}t")
        t_node.text = value

    def _write_number(self, cell: ET.Element, value: float | int) -> None:
        for child in list(cell):
            cell.remove(child)
        cell.attrib.pop("t", None)
        v = ET.SubElement(cell, f"{{{MAIN_NS}}}v")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        v.text = format(value, ".15g") if isinstance(value, float) else str(value)

    def _ensure_row(self, root: ET.Element, row_num: int) -> ET.Element:
        sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
        if sheet_data is None:
            raise FableWorkbookError("Worksheet has no sheetData")
        for row in sheet_data.findall(f"{{{MAIN_NS}}}row"):
            if int(row.attrib.get("r", "0")) == row_num:
                return row
        new_row = ET.Element(f"{{{MAIN_NS}}}row", {"r": str(row_num)})
        rows = sheet_data.findall(f"{{{MAIN_NS}}}row")
        inserted = False
        for i, row in enumerate(rows):
            if int(row.attrib.get("r", "0")) > row_num:
                sheet_data.insert(i, new_row)
                inserted = True
                break
        if not inserted:
            sheet_data.append(new_row)
        return new_row

    def _ensure_cell(self, root: ET.Element, col: int, row_num: int, style: Optional[str] = None) -> ET.Element:
        ref = _cell_ref(col, row_num)
        existing = self._find_cell(root, ref)
        if existing is not None:
            return existing
        row = self._ensure_row(root, row_num)
        cell = ET.Element(f"{{{MAIN_NS}}}c", {"r": ref})
        if style is not None:
            cell.attrib["s"] = style
        target_col = col
        inserted = False
        cells = row.findall(f"{{{MAIN_NS}}}c")
        for i, c in enumerate(cells):
            c_col, _ = _parse_cell_ref(c.attrib["r"])
            if c_col > target_col:
                row.insert(i, cell)
                inserted = True
                break
        if not inserted:
            row.append(cell)
        self._cell_index.setdefault(id(root), {})[ref] = cell
        return cell

    def _remove_cells_in_range(self, root: ET.Element, c1: int, r1: int, c2: int, r2: int) -> None:
        sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
        if sheet_data is None:
            return
        for row in sheet_data.findall(f"{{{MAIN_NS}}}row"):
            rn = int(row.attrib.get("r", "0"))
            if rn < r1 or rn > r2:
                continue
            for c in list(row.findall(f"{{{MAIN_NS}}}c")):
                cn, _ = _parse_cell_ref(c.attrib["r"])
                if c1 <= cn <= c2:
                    self._cell_index.setdefault(id(root), {}).pop(c.attrib.get("r"), None)
                    row.remove(c)

    def replace_table_rows(self, table_name: str, rows: List[Dict[str, Any]]) -> None:
        """Replace a table's data body while keeping its header and formatting context.

        This is used for FullDataTradeAdj. It can grow a placeholder table in newer
        FABLE-C versions or shrink/update an already populated table in older versions.
        """
        info = self.table_info(table_name)
        c1, r1, c2, old_r2 = _parse_range_ref(info["ref"])
        sheet_path = info["sheet_path"]
        root = self._sheet_root(sheet_path)

        # Table headers are authoritative.
        headers = []
        for col in range(c1, c2 + 1):
            headers.append(str(self._cell_value(self._find_cell(root, _cell_ref(col, r1))) or "").strip())
        if not headers or any(not h for h in headers):
            raise FableWorkbookError(f"Could not read headers for table {table_name}")

        # Capture a style template from the first data row before clearing.
        styles: Dict[int, Optional[str]] = {}
        for col in range(c1, c2 + 1):
            cell = self._find_cell(root, _cell_ref(col, r1 + 1))
            styles[col] = cell.attrib.get("s") if cell is not None else None

        new_r2 = r1 + max(1, len(rows))
        clear_r2 = max(old_r2, new_r2)
        self._remove_cells_in_range(root, c1, r1 + 1, c2, clear_r2)

        # Always maintain at least one data row because Excel tables cannot be zero-row here.
        data_rows = rows or [{h: None for h in headers}]
        for offset, record in enumerate(data_rows, start=1):
            rn = r1 + offset
            for col, header in zip(range(c1, c2 + 1), headers):
                cell = self._ensure_cell(root, col, rn, styles.get(col))
                value = record.get(header)
                if value is None or value == "":
                    # Empty cell but keep ref/style.
                    for child in list(cell):
                        cell.remove(child)
                    cell.attrib.pop("t", None)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    self._write_number(cell, value)
                else:
                    self._write_inline_string(cell, str(value))

        # Update table XML range and autoFilter range.
        table_root = ET.fromstring(self._modified_xml.get(info["table_path"]) or self._zip.read(info["table_path"]))
        new_ref = f"{_cell_ref(c1, r1)}:{_cell_ref(c2, new_r2)}"
        table_root.attrib["ref"] = new_ref
        af = table_root.find(f"{{{MAIN_NS}}}autoFilter")
        if af is not None:
            af.attrib["ref"] = new_ref
        self._modified_xml[info["table_path"]] = ET.tostring(table_root, encoding="utf-8", xml_declaration=True)
        self._table_map[table_name.lower()]["ref"] = new_ref

    def update_pathway_trade_switch(self, pathway: str, value: str = "Yes") -> None:
        info = self.table_info("PathwaysSelection")
        c1, r1, c2, r2 = _parse_range_ref(info["ref"])
        root = self._sheet_root(info["sheet_path"])
        headers = {}
        for col in range(c1, c2 + 1):
            h = self._cell_value(self._find_cell(root, _cell_ref(col, r1)))
            headers[str(h).strip() if h is not None else ""] = col
        pathway_col = headers.get("PATHWAY") or headers.get("Pathway")
        s12_col = headers.get("S12")
        if pathway_col is None or s12_col is None:
            raise FableWorkbookError("PathwaysSelection table does not contain PATHWAY and S12")
        found = False
        for rn in range(r1 + 1, r2 + 1):
            p = self._cell_value(self._find_cell(root, _cell_ref(pathway_col, rn)))
            if str(p).strip() == pathway:
                cell = self._ensure_cell(root, s12_col, rn)
                self._write_inline_string(cell, value)
                found = True
                break
        if not found:
            raise FableWorkbookError(f"Pathway {pathway!r} not found in PathwaysSelection")

        # Keep the cached ScenSelection display consistent before Excel recalculates.
        try:
            s_info = self.table_info("ScenSelection")
            sc1, sr1, sc2, sr2 = _parse_range_ref(s_info["ref"])
            sroot = self._sheet_root(s_info["sheet_path"])
            sheaders = {}
            for col in range(sc1, sc2 + 1):
                h = self._cell_value(self._find_cell(sroot, _cell_ref(col, sr1)))
                sheaders[str(h).strip() if h is not None else ""] = col
            pcol = next((col for h, col in sheaders.items() if h.lower() == "pathway"), None)
            tcol = next((col for h, col in sheaders.items() if "trade adjustment" in h.lower()), None)
            if pcol and tcol:
                for rn in range(sr1 + 1, sr2 + 1):
                    pval = self._cell_value(self._find_cell(sroot, _cell_ref(pcol, rn)))
                    if str(pval).strip() == pathway:
                        c = self._ensure_cell(sroot, tcol, rn)
                        f = c.find(f"{{{MAIN_NS}}}f")
                        if f is not None:
                            # Preserve formula, replace cached result only.
                            c.attrib["t"] = "str"
                            v = c.find(f"{{{MAIN_NS}}}v")
                            if v is None:
                                v = ET.SubElement(c, f"{{{MAIN_NS}}}v")
                            v.text = value
                        else:
                            self._write_inline_string(c, value)
                        break
        except FableWorkbookError:
            pass

    def _force_recalculation(self) -> None:
        wb_path = "xl/workbook.xml"
        root = ET.fromstring(self._modified_xml.get(wb_path) or self._zip.read(wb_path))
        calc = root.find(f"{{{MAIN_NS}}}calcPr")
        if calc is None:
            calc = ET.SubElement(root, f"{{{MAIN_NS}}}calcPr")
        calc.attrib.update({"calcMode": "auto", "fullCalcOnLoad": "1", "forceFullCalc": "1"})
        self._modified_xml[wb_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def to_bytes(self) -> bytes:
        # Serialize cached modified sheet trees.
        for path, root in self._sheet_cache.items():
            self._modified_xml[path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self._force_recalculation()
        out = BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in self._zip.infolist():
                data = self._modified_xml.get(item.filename)
                if data is None:
                    data = self._zip.read(item.filename)
                zout.writestr(item, data)
        return out.getvalue()


def extract_calculator(source: bytes | str | Path, filename: Optional[str] = None) -> Tuple[CalculatorMeta, List[TradeRow]]:
    pkg = XlsxPackage(source)
    if filename:
        pkg.filename = filename
    try:
        report = pkg.read_table("Reporting_byproduct")
        if not report:
            raise FableWorkbookError("Reporting_byproduct table is empty")

        # Dynamically identify required fields across calculator versions.
        key_map = {k.lower(): k for k in report[0].keys()}
        required = ["location", "product", "year", "import_quantity", "export_quantity"]
        missing = [k for k in required if k not in key_map]
        if missing:
            raise FableWorkbookError(f"Reporting_byproduct is missing columns: {', '.join(missing)}")

        scen = pkg.read_table("ScenSelection")
        if not scen:
            raise FableWorkbookError("ScenSelection table is empty")
        scen_row = scen[0]
        pathway_key = next((k for k in scen_row if k.strip().lower() == "pathway"), None)
        if not pathway_key:
            raise FableWorkbookError("Could not detect the selected Pathway")
        pathway = str(scen_row[pathway_key]).strip()
        trade_setting_key = next((k for k in scen_row if "trade adjustment" in k.strip().lower()), None)
        trade_setting = str(scen_row.get(trade_setting_key)).strip() if trade_setting_key and scen_row.get(trade_setting_key) is not None else None

        countries = sorted({str(r[key_map["location"]]).strip() for r in report if r.get(key_map["location"]) not in (None, "")})
        if len(countries) != 1:
            raise FableWorkbookError(f"Expected one country in Reporting_byproduct, found {countries}")
        country = countries[0]
        country_id = _guess_iso3(pkg.filename, country)
        iteration = _guess_iteration(pkg.filename)

        rows: List[TradeRow] = []
        for r in report:
            product = str(r[key_map["product"]]).strip() if r.get(key_map["product"]) is not None else ""
            if not product:
                continue
            rows.append(TradeRow(
                country=country,
                country_id=country_id,
                pathway=pathway,
                product=product,
                year=_as_int(r[key_map["year"]]),
                import_quantity=_as_number(r[key_map["import_quantity"]]),
                export_quantity=_as_number(r[key_map["export_quantity"]]),
                source_filename=pkg.filename,
                iteration=iteration,
            ))

        meta = CalculatorMeta(
            filename=pkg.filename,
            country=country,
            country_id=country_id,
            pathway=pathway,
            iteration=iteration,
            trade_adjustment_setting=trade_setting,
            product_count=len({r.product for r in rows}),
            year_count=len({r.year for r in rows}),
            record_count=len(rows),
        )
        return meta, rows
    finally:
        pkg.close()


def inject_adjusted_trade(
    source: bytes | str | Path,
    adjusted_rows: Iterable[Dict[str, Any]],
    pathway: str,
    filename: Optional[str] = None,
    set_trade_adjustment_yes: bool = True,
) -> bytes:
    pkg = XlsxPackage(source)
    if filename:
        pkg.filename = filename
    try:
        rows = []
        for r in adjusted_rows:
            if str(r.get("Pathway", r.get("pathway", ""))).strip() != pathway:
                continue
            rows.append({
                "Pathway": pathway,
                "Product": str(r.get("Product", r.get("product", ""))).strip(),
                "Year": int(r.get("Year", r.get("year"))),
                "ImportsAdj": float(r.get("ImportsAdj", r.get("imports_adj"))),
                "ExportsAdj": float(r.get("ExportsAdj", r.get("exports_adj"))),
            })
        rows.sort(key=lambda x: (x["Product"], x["Year"]))
        if not rows:
            raise FableWorkbookError(f"No adjusted rows supplied for pathway {pathway}")
        pkg.replace_table_rows("FullDataTradeAdj", rows)
        if set_trade_adjustment_yes:
            pkg.update_pathway_trade_switch(pathway, "Yes")
        return pkg.to_bytes()
    finally:
        pkg.close()
