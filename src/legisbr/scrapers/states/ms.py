from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import clean_norm_soup, wrap_html
from legisbr.core.document import Document
from legisbr.core.scraper import (
    DEFAULT_INVALID_SITUATION,
    DEFAULT_VALID_SITUATION,
    BaseScraper,
)

TYPES = {
    "Constituição Estadual": "/Web%5CConstituição%20Estadual",
    "Decreto": "/Decreto",
    "Decreto E": "/DecretoE",
    "Decreto E Conjunto": "/Web%5CDecretoE%20Conjunto",
    "Decreto-Lei": "/Decreto-Lei",
    "Deliberação Conselho de Governança": "/Web%5CDeliberacaoConselhoGov",
    "Emenda Constitucional": "/Emenda",
    "Lei Complementar": "/Lei%20Complementar",
    "Lei Estadual": "/Lei%20Estadual",
    "Mensagem Vetada": "/Mensagem%20Veto",
    "Resolução": "/Resolucoes",
    "Resolução Conjunta": "/Web%5CResolução%20Conjunta",
}
SITUATIONS = {
    "Não consta": "Não consta",
    DEFAULT_INVALID_SITUATION: DEFAULT_INVALID_SITUATION,
}
_NSF_BASE = "/appls/legislacao/secoge/govato.nsf"
_YEAR_FIELD_NAMES = ("wano", "$246")
_SUMMARY_FIELD_NAMES = ("wementa", "Ato_Ementa", "$246")
_SUMMARY_NORMALIZE_RE = re.compile("[^\\w]", re.UNICODE)
_RE_REVOKED_NOTE = re.compile(
    "\\bRevogad[ao]\\b\\s+(?:pela|pelo|por)\\b", re.IGNORECASE
)
_GLUED_NUMERO_RE = re.compile(r"(?<=[A-Za-zÀ-ÿ])(n[º°])", re.IGNORECASE)
_DISCLAIMER_RE = re.compile(
    r"ESTE\s+TEXTO\s+N[AÃ]O\s+SUBSTITUI\s+O\s+ORIGINAL", re.IGNORECASE
)
_STATE_BANNER_RE = re.compile(
    r"^ESTADO\s+DE\s+MATO\s+GROSSO\s+DO\s+SUL$", re.IGNORECASE
)
_TOOLBAR_TOKENS = ("sair", "editar", "excluir", "novo anexo")
_NON_IMAGE_ATTACH_RE = re.compile(
    r"\.(?:docx?|xlsx?|pdf|rtf|zip|rar|odt|ods|txt)(?:$|[?#])", re.IGNORECASE
)
_ORDINAL_ONLY_RE = re.compile(r"^[ºª°]+$")


class MSAlemsScraper(BaseScraper):
    """Webscraper for Mato Grosso do Sul state legislation (Domino/Notes web server).

    Year start (earliest on source): 1979

    Uses the Domino ``?ReadViewEntries`` XML API to list documents per type and
    year without relying on session-based full-text search or HTML view parsing.

    Example entry endpoint:
        GET https://aacpdappls.net.ms.gov.br/appls/legislacao/secoge/govato.nsf/Lei%20Estadual?ReadViewEntries&Count=200&Start=2&Expand=2

    Documents are fetched by UNID:
        GET https://aacpdappls.net.ms.gov.br/appls/legislacao/secoge/govato.nsf/0/{UNID}?OpenDocument
    """

    types = TYPES
    situations = SITUATIONS
    name = "MATO_GROSSO_DO_SUL"
    base_url = "https://aacpdappls.net.ms.gov.br"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._nsf = f"{self.base_url}{_NSF_BASE}"
        self._type_year_index: dict[str, dict[int, tuple[str, int]]] = {}

    def _view_entries_url(
        self,
        type_path: str,
        start: int | str = 1,
        count: int = 200,
        expand: int | str | None = None,
    ) -> str:
        """Build a ``?ReadViewEntries`` URL for a given type view."""
        url = f"{self._nsf}{type_path}?ReadViewEntries&Count={count}&Start={start}"
        if expand is not None:
            url += f"&Expand={expand}"
        return url

    def _doc_url(self, unid: str) -> str:
        return f"{self._nsf}/0/{unid}?OpenDocument"

    @staticmethod
    def _entrydata_text(data: ET.Element) -> str:
        for tag_name in ("text", "number"):
            el = data.find(tag_name)
            if el is not None and el.text:
                value = el.text.strip()
                if value:
                    return value
        return ""

    @classmethod
    def _entry_text(cls, entry: ET.Element, col_name: str | tuple[str, ...]) -> str:
        names = (col_name,) if isinstance(col_name, str) else col_name
        for name in names:
            for data in entry.findall("entrydata"):
                if data.get("name") == name:
                    value = cls._entrydata_text(data)
                    if value:
                        return value
        return ""

    @classmethod
    def _entry_year(cls, entry: ET.Element) -> int | None:
        raw_year: str = cls._entry_text(entry, _YEAR_FIELD_NAMES)
        if not raw_year:
            return None
        try:
            return int(raw_year)
        except ValueError:
            return None

    @classmethod
    def _build_type_year_index(cls, root: ET.Element) -> dict[int, tuple[str, int]]:
        year_index: dict[int, tuple[str, int]] = {}
        for entry in root.findall("viewentry"):
            position = (entry.get("position") or "").strip()
            if not position or "." in position:
                continue
            year = cls._entry_year(entry)
            if year is None:
                continue
            try:
                doc_count = int(entry.get("descendants") or "0")
            except (TypeError, ValueError):
                continue
            if doc_count <= 0:
                continue
            year_index[year] = (position, doc_count)
        return year_index

    @staticmethod
    def _decode_html_bytes(body: bytes, charset: str | None) -> str:
        for encoding in dict.fromkeys([charset, "utf-8", "latin-1", "cp1252"]):
            if not encoding:
                continue
            try:
                return body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _notes_container(body: Tag) -> Tag | None:
        notes_root = body.find("ul")
        if isinstance(notes_root, Tag):
            return notes_root
        notes_label = body.find(string=re.compile("^\\s*Notas:\\s*$", re.IGNORECASE))
        if notes_label is None:
            return None
        notes_cell = notes_label.find_parent("td")
        return notes_cell if isinstance(notes_cell, Tag) else None

    @staticmethod
    def _normalize_for_compare(text: str) -> str:
        return _SUMMARY_NORMALIZE_RE.sub("", text).casefold()

    @classmethod
    def _remove_summary_element(cls, body: Tag, summary: str) -> None:
        if not summary:
            return
        notes_root = cls._notes_container(body)
        if not isinstance(notes_root, Tag):
            return
        normalized_summary = cls._normalize_for_compare(summary)
        if not normalized_summary:
            return
        tag_priority = {
            "table": 0,
            "td": 1,
            "div": 2,
            "p": 3,
            "i": 4,
            "span": 5,
            "font": 6,
        }
        candidates: list[tuple[int, int, int, Tag]] = []
        for tag in notes_root.find_all(
            ["table", "td", "div", "p", "i", "span", "font"]
        ):
            tag_text = tag.get_text(" ", strip=True)
            normalized_tag = cls._normalize_for_compare(tag_text)
            if not normalized_tag or normalized_summary not in normalized_tag:
                continue
            if "notas" in tag_text.casefold():
                continue
            candidates.append(
                (
                    0 if normalized_tag == normalized_summary else 1,
                    tag_priority.get(tag.name, 99),
                    len(normalized_tag),
                    tag,
                )
            )
        if not candidates:
            return
        _, _, _, best_tag = min(candidates, key=lambda item: item[:3])
        best_tag.decompose()

    @classmethod
    def _has_revogada_note(cls, body: Tag) -> bool:
        notes_root = cls._notes_container(body)
        if not isinstance(notes_root, Tag):
            return False
        return bool(_RE_REVOKED_NOTE.search(notes_root.get_text(" ", strip=True)))

    async def _load_type_year_index(
        self, type_name: str, type_path: str
    ) -> dict[int, tuple[str, int]]:
        cached = self._type_year_index.get(type_name)
        if cached is not None:
            return cached
        cat_url = self._view_entries_url(type_path, start=1, count=200)
        cat_root = ET.fromstring((await self.http.request(cat_url)).text)
        year_index = self._build_type_year_index(cat_root)
        self._type_year_index[type_name] = year_index
        return year_index

    async def _get_type_year_docs(
        self, type_name: str, type_path: str, year: int
    ) -> list[dict]:
        """Return all documents for *type_name* + *year* using ReadViewEntries."""
        year_index = await self._load_type_year_index(type_name, type_path)
        expand_info = year_index.get(year)
        if expand_info is None:
            return []
        expand_pos, doc_count = expand_info
        if doc_count <= 0:
            return []
        docs_url = self._view_entries_url(
            type_path, start=expand_pos, count=doc_count + 1, expand=expand_pos
        )
        docs_root = ET.fromstring((await self.http.request(docs_url)).text)
        prefix = f"{expand_pos}."
        docs: list[dict] = []
        for entry in docs_root.findall("viewentry"):
            pos = entry.get("position", "")
            if not pos.startswith(prefix):
                continue
            unid = entry.get("unid")
            if not unid:
                continue
            title = self._normalize_title(self._entry_text(entry, "wnumero"))
            summary = self._entry_text(entry, _SUMMARY_FIELD_NAMES)
            if not title:
                continue
            docs.append(
                {"title": title, "summary": summary, "_html_link": self._doc_url(unid)}
            )
        return docs

    @staticmethod
    def _normalize_title(title: str) -> str:
        """Insert missing space before glued `nº` / `n°` (e.g. `MSnº` -> `MS nº`)."""
        return _GLUED_NUMERO_RE.sub(r" \1", title).strip()

    @staticmethod
    def _direct_rows(table: Tag) -> list[Tag]:
        rows: list[Tag] = []
        for child in table.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "tr":
                rows.append(child)
            elif child.name in ("tbody", "thead", "tfoot"):
                rows.extend(
                    tr
                    for tr in child.children
                    if isinstance(tr, Tag) and tr.name == "tr"
                )
        return rows

    @classmethod
    def _is_domino_toolbar(cls, table: Tag) -> bool:
        """Identify Domino's 1-row top action toolbar (`Sair Editar Excluir Novo Anexo`)."""
        rows = cls._direct_rows(table)
        if len(rows) != 1:
            return False
        text = " ".join(table.get_text(" ", strip=True).split()).casefold()
        if len(text) > 120:
            return False
        if text == "nav actions":
            return True
        if sum(1 for tok in _TOOLBAR_TOKENS if tok in text) >= 2:
            return True
        for a in table.find_all("a"):
            href = str(a.get("href") or "")
            onclick = str(a.get("onclick") or "")
            if "$V5ACTIONS" in onclick or "?EditDocument" in href:
                return True
        return False

    @classmethod
    def _strip_domino_chrome(cls, body: Tag) -> None:
        """Remove Domino toolbar, disclaimer banner, header chrome, UI icons, and `$FILE` icon wrappers."""
        for table in list(body.find_all("table")):
            if table.decomposed:
                continue
            if cls._is_domino_toolbar(table):
                table.decompose()
                continue
            txt = " ".join(table.get_text(" ", strip=True).split())
            if _STATE_BANNER_RE.match(txt):
                table.decompose()

        for hr in list(body.find_all("hr")):
            if hr.decomposed:
                continue
            # Only remove the top Domino separator before the disclaimer/body.
            prev_text = "".join(
                s.strip() for s in hr.find_all_previous(string=True) if s.strip()
            )
            if len(prev_text) < 50:
                hr.decompose()

        for tag in list(body.find_all(["b", "i", "font", "p", "div", "span"])):
            if tag.decomposed:
                continue
            txt = " ".join(tag.get_text(" ", strip=True).split())
            if len(txt) < 300 and _DISCLAIMER_RE.search(txt):
                # Prefer removing the outermost inline wrapper carrying only the disclaimer.
                target = tag
                while (
                    isinstance(target.parent, Tag)
                    and target.parent.name in ("b", "i", "strong", "em", "font", "span")
                    and len(" ".join(target.parent.get_text(" ", strip=True).split()))
                    < 300
                ):
                    target = target.parent
                target.decompose()

        for a in list(body.find_all("a", href=True)):
            if a.decomposed:
                continue
            href = str(a.get("href") or "")
            if "$FILE" in href:
                for img in list(a.find_all("img")):
                    src = str(img.get("src") or "").lower()
                    if "/icons/" in src or _NON_IMAGE_ATTACH_RE.search(href):
                        img.decompose()
                for br in list(a.find_all("br")):
                    br.decompose()
                if a.get_text(strip=True):
                    a.unwrap()
                else:
                    a.decompose()

        for img in list(body.find_all("img")):
            if img.decomposed:
                continue
            src = str(img.get("src") or "").lower()
            if src.startswith("/icons/") or "/icons/" in src:
                img.decompose()

    @classmethod
    def _unwrap_layout_tables(cls, body: Tag) -> None:
        """Unwrap Domino's 1-row `border="0"` layout wrapper tables while preserving real data tables."""
        for table in list(body.find_all("table")):
            if table.decomposed:
                continue
            rows = cls._direct_rows(table)
            if len(rows) != 1:
                continue
            border = str(table.get("border") or "").strip()
            cells = [
                c
                for c in rows[0].children
                if isinstance(c, Tag) and c.name in ("td", "th")
            ]
            is_layout = (
                border == "0"
                or len(cells) <= 1
                or (
                    len(cells) == 2
                    and any(
                        not c.get_text(strip=True) and not c.find("img") for c in cells
                    )
                )
            )
            if not is_layout:
                continue
            for tr in rows:
                for cell in list(tr.children):
                    if not isinstance(cell, Tag) or cell.name not in ("td", "th"):
                        continue
                    if not cell.get_text(strip=True) and not cell.find("img"):
                        cell.decompose()
                    else:
                        cell.name = "div"
                tr.unwrap()
            for child in list(table.children):
                if isinstance(child, Tag) and child.name in ("tbody", "thead", "tfoot"):
                    child.unwrap()
            table.unwrap()

        # Domino uses nested `<ul>` without `<li>` solely for left indentation around headers.
        for ul in list(body.find_all("ul")):
            if not ul.decomposed and ul.find("li") is None:
                ul.unwrap()

    async def fetch(self, doc_info: dict) -> Document | None:
        """Fetch and convert an individual Domino document page to markdown."""
        doc_info = dict(doc_info)
        url = doc_info.pop("_html_link")
        result = await self.http.request(url)
        raw_body, response = (result.content, result)
        html = response.text
        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        if not isinstance(body, Tag):
            raise ValueError("No <body> tag found in document page")
        if self._has_revogada_note(body):
            doc_info["situation"] = DEFAULT_INVALID_SITUATION
        self._remove_summary_element(body, doc_info.get("summary", ""))
        for tag in body.find_all(["script", "applet"]):
            tag.decompose()
        self._strip_domino_chrome(body)
        self._unwrap_layout_tables(body)
        clean_norm_soup(
            body,
            remove_disclaimers=True,
            unwrap_links=True,
            remove_images=False,
            remove_empty_tags=True,
            unwrap_fonts=True,
            strip_styles=True,
            remove_style_tags=True,
            remove_script_tags=True,
        )
        for s_tag in list(body.find_all("s")):
            if not s_tag.decomposed and _ORDINAL_ONLY_RE.match(
                s_tag.get_text(strip=True)
            ):
                s_tag.unwrap()
        html_string = wrap_html(body.decode_contents())
        text_markdown = await self.markdown(html=html_string, base_url=url)
        return Document(
            **{
                **doc_info,
                "document_url": url,
                "text_markdown": text_markdown,
                "raw": raw_body,
                "raw_ext": ".html",
            }
        )

    async def list_type(
        self, year: int, norm_type: str, path: str
    ) -> AsyncIterator[dict]:
        for ref in await self._get_type_year_docs(norm_type, path, year):
            yield {
                "year": year,
                "type": norm_type,
                "situation": DEFAULT_VALID_SITUATION,
                **ref,
                "document_url": urljoin(self.base_url, ref["_html_link"]),
            }

    async def setup(self) -> None:
        self._type_year_index = {}  # filled lazily per type by list_type
