"""Extract BusinessRisksTextBlock from XBRL zip / CSV zip and convert HTML to structured text."""
from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser

EXTRACTOR_VER = "extract-1"   # extracts.extractor_ver (TextBlock lookup + html_to_text)
ELEMENT = "BusinessRisksTextBlock"
_JPCRP_COR_NS = re.compile(r"^http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jpcrp/[^/]+/jpcrp_cor$")
_TITLE_RE = re.compile(r"^[0-9０-９]*[\s　]*【事業等のリスク】$")


@dataclass
class TextBlock:
    html: str
    context_ref: str | None
    member: str
    count: int  # number of exact-name matches found


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def find_textblocks(xbrl_bytes: bytes, local_name: str = ELEMENT) -> list[tuple[str, str | None]]:
    """All jpcrp_cor:<local_name> facts as (html, contextRef). Exact namespace+localname match."""
    out = []
    for _, el in ET.iterparse(io.BytesIO(xbrl_bytes), events=("end",)):
        ns, local = _split(el.tag)
        if local == local_name and _JPCRP_COR_NS.match(ns):
            out.append((el.text or "", el.get("contextRef")))
        el.clear()
    return out


def extract_from_xbrl_zip(zip_bytes_or_path) -> TextBlock:
    zf = zipfile.ZipFile(zip_bytes_or_path if not isinstance(zip_bytes_or_path, bytes)
                         else io.BytesIO(zip_bytes_or_path))
    members = [n for n in zf.namelist() if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")]
    if not members:
        raise ValueError("no XBRL/PublicDoc/*.xbrl in ZIP (iXBRL-only fallback not implemented in M0)")
    found = []
    for m in members:
        for html, ctx in find_textblocks(zf.read(m)):
            found.append((html, ctx, m))
    if len(found) != 1:
        raise ValueError(f"expected exactly 1 {ELEMENT}, found {len(found)}")
    html, ctx, m = found[0]
    return TextBlock(html=html, context_ref=ctx, member=m, count=len(found))


# --- HTML -> text -------------------------------------------------------------
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table",
          "thead", "tbody", "tr", "br", "section", "article", "blockquote", "pre", "dl", "dt", "dd"}
_CELL = {"td", "th"}
_SKIP = {"script", "style"}


class _TextBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.cur: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.skip = 0
        self.nested = 0  # depth of tables opened inside a cell; flattened into that cell

    def _flush(self):
        s = re.sub(r"[ \t\r\n\xa0]+", " ", "".join(self.cur)).strip(" ")
        if s:
            self.lines.append(s)
        self.cur = []

    def _end_cell(self):
        if self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"[ \t\r\n\xa0]+", " ", "".join(self.cell)).strip(" "))
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif self.cell is not None and (tag == "table" or (self.nested and tag in _BLOCK | _CELL)):
            if tag == "table":
                self.nested += 1
            self.cell.append(" ")  # nested table -> its cells stay inside the outer cell
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in _CELL:
            self._end_cell()
            self.cell = []
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.handle_data(f"[画像: {alt}]")
        elif tag in _BLOCK:
            if self.cell is not None:
                self.cell.append(" ")  # block inside a table cell -> keep the row on one line
            else:
                self._flush()
                if tag == "li":
                    self.cur.append("・")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif self.nested and (tag == "table" or tag in _BLOCK | _CELL):
            if tag == "table":
                self.nested -= 1
            self.cell.append(" ")
        elif tag in _CELL:
            self._end_cell()
        elif tag == "tr":
            self._end_cell()
            if self.row is not None and any(c for c in self.row):
                self.lines.append("\t".join(self.row))
            self.row = None
        elif tag in _BLOCK and self.cell is None:
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        (self.cell if self.cell is not None else self.cur).append(data)

    def result(self) -> list[str]:
        self._end_cell()
        self._flush()
        # "・・" when the source <li> already carries a bullet
        return [re.sub(r"^・(?=[・●○■□◆◇▪•‐\-－])", "", ln) for ln in self.lines]


def html_to_text(html: str, *, drop_title: bool = True) -> tuple[str, bool]:
    """Return (text, title_removed). One block/table-row per line; table cells joined by TAB."""
    b = _TextBuilder()
    b.feed(html)
    b.close()
    lines = b.result()
    removed = False
    if drop_title and lines and _TITLE_RE.match(lines[0].replace(" ", "")):
        lines = lines[1:]
        removed = True
    return "\n".join(lines) + ("\n" if lines else ""), removed


# --- CSV (type=5) -------------------------------------------------------------
@dataclass
class CsvFact:
    element_id: str
    label: str
    context_id: str
    value: str
    member: str
    extra: dict = field(default_factory=dict)


def find_csv_facts(zip_path, element_id: str = f"jpcrp_cor:{ELEMENT}") -> list[CsvFact]:
    """type=5 ZIP: XBRL_TO_CSV/*.csv, UTF-16 (LE, BOM) TSV. Exact element-ID match."""
    zf = zipfile.ZipFile(zip_path)
    out = []
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        raw = zf.read(name)
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-16-le")
        rows = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
        header = next(rows, None)
        if not header:
            continue
        for r in rows:
            if r and r[0] == element_id:
                rec = dict(zip(header, r))
                out.append(CsvFact(element_id=r[0], label=r[1] if len(r) > 1 else "",
                                   context_id=r[2] if len(r) > 2 else "", value=r[-1], member=name,
                                   extra=rec))
    return out
