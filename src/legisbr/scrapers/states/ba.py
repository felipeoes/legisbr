from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import unquote, urlencode, urljoin

from bs4 import BeautifulSoup, Tag

from legisbr.core.convert import (
    clean_norm_soup,
    normalize_type,
    remove_summary_from_markdown,
    wrap_html,
)
from legisbr.core.document import Document
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper

TYPES = {
    "Constituição Estadual Atual 1989": 11,
    "Constituição Estadual 1967": 33,
    "Constituição Estadual 1947": 32,
    "Constituição Estadual 1935": 31,
    "Constituição Estadual 1891": 30,
    "Emenda Constitucional": 4,
    "Lei Complementar": 5,
    "Lei Ordinária": 7,
    "Lei Delegada": 6,
    "Decreto Financeiro": 1,
    "Decreto": 2,
    "Decreto Simples": 3,
    "Portaria do Gabinete do Governador": 26,
    "Portaria Conjunta Casa Civil": 20,
    "Portaria Casa Civil": 19,
    "Instrução Normativa Casa Civil": 92,
}
SITUATIONS: dict[str, str] = {}
_TYPE_LABELS = {name.casefold(): name for name in TYPES}
_TYPE_LABELS.update(
    {
        "emendas constitucionais": "Emenda Constitucional",
        "leis complementares": "Lei Complementar",
        "leis ordinárias": "Lei Ordinária",
        "leis delegadas": "Lei Delegada",
        "decretos financeiros": "Decreto Financeiro",
        "decretos numerados": "Decreto",
        "decretos simples": "Decreto Simples",
        "portarias do gabinete do governador": "Portaria do Gabinete do Governador",
        "portarias conjuntas casa civil": "Portaria Conjunta Casa Civil",
        "portarias casa civil": "Portaria Casa Civil",
        "instruções normativas casa civil": "Instrução Normativa Casa Civil",
    }
)


class BahiaLegislaScraper(BaseScraper):
    """Webscraper for Bahia state legislation website (https://www.legislabahia.ba.gov.br/).

    The site is a Drupal CMS serving static HTML — no JavaScript rendering needed,
    so all fetches use pure aiohttp (no Playwright browser).

    **Listing**: a single year-only query (no type/situation filter) returns all
    document types via ``/documentos?data[min]=YYYY-01-01&data[max]=YYYY-12-31``.
    Pagination is parsed from the ``ul.pagination`` element.

    **Document detail**: each document page contains structured metadata fields
    (category, number, date, publication date, ementa) and a ``div.field--name-body``
    with the norm text. Category from the detail page overrides the listing type.
    Revogado status is detected via ``span.revogado`` or ``div.alteracao`` text.

    **Rate limit**: capped at 1 rps — the server is unstable and returns frequent
    HTTP 500 errors at higher rates.

    Year start (earliest on source): 1891
    """

    name = "BAHIA"
    types = TYPES
    situations = {}
    base_url = "https://www.legislabahia.ba.gov.br"
    _REVOGADO_RE = re.compile("\\brevogad[ao]\\b", re.IGNORECASE)
    _REVOGADO_MARKER_RE = re.compile("<REVOGAD[AO]>", re.IGNORECASE)
    _DECORATIVE_RULE_IMG_RE = re.compile(r"DECS\d+.*image\d+\.gif$", re.IGNORECASE)
    _REVOGADO_IMG_RE = re.compile(r"/revogad[ao]\.gif$", re.IGNORECASE)
    _REPUBLICACAO_IMG_RE = re.compile(r"/republica[çc][ãa]o\.gif$", re.IGNORECASE)
    _PROVISION_PREFIX_RE = re.compile(
        r"^(?:Art\.?|§|Par[aá]grafo|Inciso|Al[ií]nea|Item|Anexo|Caput|Tabela|[IVXLCDM]+\s*[-–.]|[a-z]\))\b",
        re.IGNORECASE,
    )
    _NORM_TITLE_RE = re.compile(
        r"\b(?:LEI|DECRETO|PORTARIA|RESOLU[ÇC][ÃA]O|EMENDA|INSTRU[ÇC][ÃA]O|CONSTITUI[ÇC][ÃA]O)\b",
        re.IGNORECASE,
    )
    _DOC_REVOKED_ALT_RE = re.compile(
        r"^\s*Revogad[ao]\s+pel[ao]\b|\b(?:esta\s+lei|este\s+decreto|esta\s+norma|esta\s+portaria)\b.*\brevogad[ao]\b",
        re.IGNORECASE,
    )
    _TRUNCATED_NBSP_RE = re.compile(r"&nbs(?![a-zA-Z0-9;])")

    def _normalize_type(self, raw_type: str) -> str:
        return normalize_type(raw_type, aliases=_TYPE_LABELS, known_types=self.types)

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

    def _clean_layout_tables(self, soup: Tag) -> None:
        """Unwrap MS Word layout tables while preserving genuine multi-column data tables."""
        for table in list(soup.find_all("table")):
            if table.decomposed:
                continue
            has_empty_cell_style = table.find("p", class_="EMPTYCELLSTYLE") is not None
            rows = self._direct_rows(table)
            max_cols = max(
                (
                    len(
                        [
                            c
                            for c in tr.children
                            if isinstance(c, Tag) and c.name in ("td", "th")
                        ]
                    )
                    for tr in rows
                ),
                default=0,
            )
            is_single_cell_wrapper = len(rows) == 1 and max_cols <= 1
            if not has_empty_cell_style and not is_single_cell_wrapper:
                continue
            for p in list(table.find_all("p", class_="EMPTYCELLSTYLE")):
                if not p.decomposed:
                    p.decompose()
            for tr in rows:
                for cell in list(tr.children):
                    if isinstance(cell, Tag) and cell.name in ("td", "th"):
                        cell.name = "div"
                tr.name = "div"
            for child in list(table.children):
                if isinstance(child, Tag) and child.name in ("tbody", "thead", "tfoot"):
                    child.name = "div"
            table.name = "div"

    @classmethod
    def _is_before_first_article(cls, node: Tag, root: Tag) -> bool:
        """Return True if *node* precedes the first operative article in *root*."""
        for prev in node.find_all_previous(["div", "p"]):
            if prev is root or root not in prev.parents:
                continue
            if "artpar" in (prev.get("class") or []):
                return False
            txt = " ".join(prev.get_text(" ", strip=True).split())
            if re.match(r"^(?:<REVOGAD[AO]>\s*)?Art\.?\s*\d+", txt, re.IGNORECASE):
                return False
        return True

    @classmethod
    def _is_doc_level_revocation_node(cls, node: Tag, root: Tag) -> bool:
        """Return True when a revocation badge/span marks the entire norm (not a single article)."""
        parent = node.parent if isinstance(node.parent, Tag) else None
        if parent is not None and parent is not root:
            # Remove the node's own text to inspect the surrounding line/container.
            parent_text = " ".join(parent.get_text(" ", strip=True).split())
            own_text = " ".join(node.get_text(" ", strip=True).split())
            rest = parent_text
            if own_text and rest.lower().startswith(own_text.lower()):
                rest = rest[len(own_text) :].strip()
            rest = cls._REVOGADO_MARKER_RE.sub("", rest).strip()
            if rest and cls._PROVISION_PREFIX_RE.match(rest):
                return False
            if rest and cls._NORM_TITLE_RE.search(rest[:80]):
                return cls._is_before_first_article(node, root)
        return cls._is_before_first_article(node, root)

    @classmethod
    def _is_decorative_divider_img(cls, img: Tag) -> bool:
        """Identify MS Word horizontal-rule divider GIFs (e.g. 498x6 DECS*image*.gif)."""
        src = unquote(str(img.get("src") or "")).strip()
        if cls._DECORATIVE_RULE_IMG_RE.search(src):
            return True
        height = img.get("height")
        if isinstance(height, str):
            m = re.match(r"^\s*(\d+)(?:px)?\s*$", height, re.IGNORECASE)
            if m and int(m.group(1)) <= 10:
                return True
        style = str(img.get("style") or "")
        m_style = re.search(r"height\s*:\s*(\d+)\s*px", style, re.IGNORECASE)
        if m_style and int(m_style.group(1)) <= 10:
            return True
        return False

    def _strip_revogado(self, soup: Tag) -> bool:
        """Clean decorative/status GIFs and `<REVOGADO>` markers and detect whole-norm revocation.

        Only marks the document-level situation as revoked when the revocation badge,
        `span.revogado`, or top-level `alteracao` notice applies to the whole norm
        (ignoring partial single-article revocations).
        """
        is_revogado = False

        # 1. Handle decorative horizontal-rule GIFs and status badge GIFs.
        for img in list(soup.find_all("img")):
            if img.decomposed:
                continue
            src = unquote(str(img.get("src") or "")).strip()
            if self._is_decorative_divider_img(img):
                img.decompose()
            elif self._REVOGADO_IMG_RE.search(src):
                if self._is_doc_level_revocation_node(img, soup):
                    is_revogado = True
                img.replace_with("[Revogado] ")
            elif self._REPUBLICACAO_IMG_RE.search(src):
                img.replace_with("[Republicação] ")

        # 2. Handle `span.revogado` / `span.revogada` elements.
        for span in list(soup.find_all("span", class_=self._REVOGADO_RE)):
            if span.decomposed:
                continue
            if self._is_doc_level_revocation_node(span, soup):
                is_revogado = True
                span.decompose()
            else:
                # Partial provision revocation badge (e.g., Art. 3º): keep readable text
                # if it wasn't just an inline `<REVOGADO>` wrapper.
                span_txt = span.get_text(" ", strip=True)
                if self._REVOGADO_MARKER_RE.fullmatch(span_txt):
                    span.decompose()
                else:
                    span.replace_with("[Revogado] ")

        # 3. Check top-level `alteracao` notices for whole-norm revocation.
        for el in soup.find_all(["p", "div"]):
            cls = " ".join(el.get("class", []))
            if not re.search("\\balteracao\\b", cls, re.IGNORECASE):
                continue
            text = " ".join(el.get_text(" ", strip=True).split())
            if not self._REVOGADO_RE.search(text):
                continue
            if self._PROVISION_PREFIX_RE.match(text):
                continue
            if self._is_before_first_article(el, soup) and (
                self._DOC_REVOKED_ALT_RE.search(text) or len(text) < 200
            ):
                is_revogado = True
                break

        # 4. Strip ONLY the literal `<REVOGAD[AO]>` marker substring (never erasing
        # adjacent article/paragraph numbers like `Art. 179` or `§ 3º`).
        for el in list(soup.find_all(string=self._REVOGADO_MARKER_RE)):
            cleaned = self._REVOGADO_MARKER_RE.sub("", str(el))
            if cleaned:
                el.replace_with(cleaned)
            else:
                el.extract()

        return is_revogado

    def _build_search_url(self, norm_type_id: int | None, year: int, page: int) -> str:
        """Build search URL from arguments (no shared state mutation)."""
        params = {
            "num": "",
            "ementa": "",
            "exp": "",
            "data[min]": f"{year}-01-01",
            "data[max]": f"{year}-12-31",
            "page": page,
        }
        if norm_type_id is not None:
            params["categoria[]"] = norm_type_id
        return f"{self.base_url}/documentos?{urlencode(params)}"

    @staticmethod
    def _get_field_item_text(soup: BeautifulSoup | Tag, class_name: str) -> str:
        field = soup.find("div", class_=class_name)
        if not field:
            return ""
        item = field.find("div", class_="field--item")
        if not item:
            return ""
        return item.get_text(" ", strip=True)

    def _get_total_pages(self, soup: BeautifulSoup) -> int:
        pagination = soup.find("ul", class_="pagination js-pager__items")
        if not pagination:
            # The view header reads "1 - 20 de 1138"; more results than rows needs a pager.
            header = soup.find("div", class_="view-header")
            total = re.search(r"de\s+(\d+)", header.get_text()) if header else None
            if total and int(total.group(1)) > len(soup.select("tbody tr")):
                raise ValueError("Listing has more results than rows but no pager")
            return 1
        last_page = 0
        for page_link in pagination.find_all("a", href=True):
            href = page_link.get("href")
            if not isinstance(href, str) or "page=" not in href:
                continue
            try:
                page = int(href.split("page=")[-1].split("&")[0])
            except ValueError:
                continue
            last_page = max(last_page, page)
        return last_page + 1

    async def _get_docs_links(
        self, url: str, *, soup: BeautifulSoup | None = None
    ) -> list[dict]:
        """Get documents html links from a given search page."""
        if soup is None:
            soup = (await self.http.request(url)).soup()
        empty = soup.find("td", class_="views-empty")
        if empty and "Nenhum resultado encontrado" in empty.get_text():
            return []
        tbody = soup.find("tbody")
        if not tbody:
            raise ValueError(f"No results table at {url}")
        docs = []
        for item in tbody.find_all("tr"):
            tds = item.find_all("td")
            if len(tds) != 2:
                continue
            link_tag = tds[0].find("a", href=True)
            if not link_tag:
                continue
            href = link_tag.get("href")
            if not isinstance(href, str):
                continue
            title_tag = tds[0].find("b")
            title = (title_tag or link_tag).get_text(" ", strip=True)
            norm_type = self._normalize_type(tds[1].get_text(" ", strip=True))
            if not title:
                continue
            docs.append({"title": title, "type": norm_type, "_html_link": href})
        if not docs:
            raise ValueError(f"No document rows in results table at {url}")
        return docs

    async def fetch(self, doc_info: dict) -> Document | None:
        """Get document data from a given document dict."""
        doc_info = dict(doc_info)
        html_link = doc_info.pop("_html_link")
        url = urljoin(self.base_url, html_link)
        resp = await self.http.request(url)
        body, _ = (resp.content, resp)
        soup = BeautifulSoup(body, "lxml")
        category = self._get_field_item_text(soup, "field--name-field-categoria-doc")
        if category:
            doc_info["type"] = self._normalize_type(category)
        norm_number = self._get_field_item_text(soup, "field--name-field-numero-doc")
        date = self._get_field_item_text(soup, "field--name-field-data-doc")
        publication_date = self._get_field_item_text(
            soup, "field--name-field-data-de-publicacao-no-doe"
        )
        summary = self._get_field_item_text(soup, "field--name-field-ementa")
        norm_text_tag = soup.find("div", class_="field--name-body")
        if not isinstance(norm_text_tag, Tag):
            raise ValueError("Could not find div.field--name-body in document page")
        for text_node in list(norm_text_tag.find_all(string=self._TRUNCATED_NBSP_RE)):
            cleaned_txt = self._TRUNCATED_NBSP_RE.sub(" ", str(text_node))
            text_node.replace_with(cleaned_txt)
        is_revogado = self._strip_revogado(norm_text_tag)
        if is_revogado:
            doc_info["situation"] = "Revogado"
        clean_norm_soup(
            norm_text_tag,
            unwrap_links=True,
            strip_styles=True,
            remove_style_tags=True,
            remove_script_tags=True,
        )
        self._clean_layout_tables(norm_text_tag)
        html_string = wrap_html(str(norm_text_tag))
        doc_info["norm_number"] = norm_number
        doc_info["date"] = date
        doc_info["publication_date"] = publication_date
        doc_info["summary"] = summary
        text_markdown = await self.markdown(html=html_string, base_url=url)
        result = Document(
            **{
                **doc_info,
                "document_url": url,
                "text_markdown": text_markdown,
                "raw": body,
                "raw_ext": ".html",
            }
        )
        if result is not None and summary:
            md = result.text_markdown
            if md:
                result.text_markdown = remove_summary_from_markdown(md, summary)
        return result

    async def list_documents(self, year: int) -> AsyncIterator[dict]:
        url = self._build_search_url(None, year, 0)
        soup = (await self.http.request(url)).soup()
        pages = self._get_total_pages(soup)

        async def fetch_page(page: int) -> list[dict]:
            return (
                await self._get_docs_links(url, soup=soup)
                if page == 0
                else await self._get_docs_links(
                    self._build_search_url(None, year, page)
                )
            )

        async for ref in self.paginate(fetch_page, pages - 1, start=0):
            yield {
                "year": year,
                "situation": DEFAULT_VALID_SITUATION,
                **ref,
                "document_url": urljoin(self.base_url, ref["_html_link"]),
            }
