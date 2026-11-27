"Parser and metadata regression tests for tocantins_scraper."

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from bs4 import BeautifulSoup
from conftest import make_mock_response, make_scraper
from parser_assertions import (
    ScraperClassTests,
    SituationsConstantTests,
    TypesConstantTests,
)

from legisbr.core.query import Query
from legisbr.core.scraper import BaseScraper
from legisbr.scrapers.states.to import SITUATIONS, TYPES, TocantinsScraper


def _make_scraper(**kwargs) -> TocantinsScraper:
    """Instantiate TocantinsScraper with injected dependencies."""
    return make_scraper(
        TocantinsScraper,
        search_url="https://www.al.to.leg.br/legislacaoEstadual",
        **kwargs,
    )


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 3
    REQUIRED_KEYS = {"Constituição Estadual", "Lei Ordinária", "Lei Complementar"}
    REQUIRE_INT_VALUES = False


class TestSituationsConstant(SituationsConstantTests):
    SITUATIONS = SITUATIONS
    EXPECTED_TYPE = dict
    EXPECTED_EMPTY = False


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = TocantinsScraper
    STATE_NAME = "Tocantins"


class TestFormatSearchPayload:
    def test_includes_type_id(self):
        scraper = _make_scraper()
        payload = scraper._format_search_payload("ordinaria", 2023)
        assert payload["documento.tipo"] == "ordinaria"

    def test_includes_year(self):
        scraper = _make_scraper()
        payload = scraper._format_search_payload("ordinaria", 2023)
        assert payload["documento.ano"] == "2023"

    def test_default_page_is_1(self):
        scraper = _make_scraper()
        payload = scraper._format_search_payload("ordinaria", 2023)
        assert payload["pagPaginaAtual"] == "1"

    def test_custom_page(self):
        scraper = _make_scraper()
        payload = scraper._format_search_payload("complementar", 2022, page=3)
        assert payload["pagPaginaAtual"] == "3"

    def test_required_keys_present(self):
        scraper = _make_scraper()
        payload = scraper._format_search_payload("ordinaria", 2023)
        for key in (
            "pagPaginaAtual",
            "documento.ano",
            "documento.tipo",
            "documento.numero",
        ):
            assert key in payload


def _make_row_html(
    title: str = "Lei Ordinária 1/2023",
    doc_link: str = "/legislacaoEstadual/detalhe?id=1",
    date: str = "01/01/2023",
    summary: str = "Dispõe sobre algo.",
    pdf_href: str = "/arquivos/lei1.pdf",
) -> str:
    return f'<div class="row"><h4><a href="{doc_link}">{title}</a></h4><small>Data: {date}</small><em><strong>{summary}</strong></em><a href="{pdf_href}" title="Download">Download</a></div>'


class TestExtractDocsFromSoup:
    def test_empty_html_returns_empty(self):
        scraper = _make_scraper()
        result = scraper._extract_docs_from_soup(b"<html><body></body></html>")
        assert result == []

    def test_row_without_h4_skipped(self):
        scraper = _make_scraper()
        html = b'<html><body><div class="row"><p>No h4</p></div></body></html>'
        result = scraper._extract_docs_from_soup(html)
        assert result == []

    def test_row_without_link_in_h4_skipped(self):
        scraper = _make_scraper()
        html = b'<html><body><div class="row"><h4>No link</h4></div></body></html>'
        result = scraper._extract_docs_from_soup(html)
        assert result == []

    def test_valid_row_returns_doc(self):
        scraper = _make_scraper()
        html = _make_row_html().encode("utf-8")
        result = scraper._extract_docs_from_soup(html)
        assert len(result) == 1

    def test_doc_has_expected_fields(self):
        scraper = _make_scraper()
        html = _make_row_html(
            title="Lei Ordinária 42/2023",
            doc_link="/legislacaoEstadual/detalhe?id=42",
            date="15/06/2023",
            summary="Dispõe sobre algo.",
            pdf_href="/arquivos/lei42.pdf",
        ).encode("utf-8")
        result = scraper._extract_docs_from_soup(html)
        doc = result[0]
        assert doc["title"] == "Lei Ordinária 42/2023"
        assert "15/06/2023" in doc["date"]
        assert doc["summary"] == "Dispõe sobre algo."
        assert doc["situation"] == "Não consta"
        assert "_pdf_link" in doc

    def test_relative_pdf_link_prefixed(self):
        scraper = _make_scraper()
        html = _make_row_html(pdf_href="/arquivos/lei1.pdf").encode("utf-8")
        result = scraper._extract_docs_from_soup(html)
        assert result[0]["_pdf_link"].startswith("https://www.al.to.leg.br")

    def test_absolute_pdf_link_unchanged(self):
        scraper = _make_scraper()
        html = _make_row_html(pdf_href="https://cdn.example.com/lei1.pdf").encode(
            "utf-8"
        )
        result = scraper._extract_docs_from_soup(html)
        assert result[0]["_pdf_link"] == "https://cdn.example.com/lei1.pdf"

    def test_multiple_rows(self):
        scraper = _make_scraper()
        html = (
            _make_row_html(
                title="Lei 1/2023", doc_link="/detalhe?id=1", pdf_href="/a1.pdf"
            )
            + _make_row_html(
                title="Lei 2/2023", doc_link="/detalhe?id=2", pdf_href="/a2.pdf"
            )
        ).encode("utf-8")
        result = scraper._extract_docs_from_soup(html)
        assert len(result) == 2


class TestExtractTotalCount:
    def test_no_registros_div_returns_none(self):
        soup = BeautifulSoup("<html><body><p>Sem resultados</p></body></html>", "lxml")
        result = TocantinsScraper._extract_total_count(soup)
        assert result is None

    def test_extracts_count_from_strong(self):
        html = "<html><body><div>Registros encontrados: <strong>42</strong></div></body></html>"
        soup = BeautifulSoup(html, "lxml")
        result = TocantinsScraper._extract_total_count(soup)
        assert result == 42

    def test_ignores_div_without_strong(self):
        html = "<html><body><div>Registros encontrados: sem strong</div></body></html>"
        soup = BeautifulSoup(html, "lxml")
        result = TocantinsScraper._extract_total_count(soup)
        assert result is None


def _make_redesigned_listing_html(
    *,
    count_banner: str = "138 resultados Mostrando 1–20",
    articles_html: str | None = None,
    nav_html: str = (
        '<nav aria-label="Paginação" class="paginacao">'
        '<p class="paginacao-info">Mostrando 1–20 de 138 · página 1 de 7</p>'
        '<ul class="pagination">'
        '<li class="page-item"><a class="page-link" href="?pagPaginaAtual=2#dados">2</a></li>'
        '<li class="page-item"><a class="page-link" href="?pagPaginaAtual=7#dados">7</a></li>'
        "</ul></nav>"
    ),
) -> bytes:
    if articles_html is None:
        articles_html = (
            '<article class="resultado">'
            '<p class="resultado-meta"><span class="resultado-categoria">Legislação Estadual</span>'
            '<time datetime="2012-12-21">21 dez 2012</time></p>'
            '<h2 class="h5 resultado-titulo"><a class="stretched-link" href="/arquivo/31849">'
            "Lei nº 2699 de 2012 (ordinária)</a></h2>"
            '<p class="resultado-ementa">Altera a Lei 2.538/2011.</p>'
            '<p class="resultado-acoes"><a href="/arquivo/31849">Abrir PDF</a></p>'
            "</article>"
        )
    banner = f'<p class="busca-contagem">{count_banner}</p>' if count_banner else ""
    return f"<html><body>{banner}{articles_html}{nav_html}</body></html>".encode()


class TestRedesignedPortalParsing:
    def test_extracts_article_resultado_cards(self):
        scraper = _make_scraper()
        docs = scraper._extract_docs_from_soup(_make_redesigned_listing_html())
        assert len(docs) == 1
        assert docs[0] == {
            "title": "Lei nº 2699 de 2012 (ordinária)",
            "summary": "Altera a Lei 2.538/2011.",
            "date": "2012-12-21",
            "situation": "Não consta",
            "_pdf_link": "https://www.al.to.leg.br/arquivo/31849",
        }

    def test_extracts_total_count_from_busca_contagem(self):
        soup = BeautifulSoup(_make_redesigned_listing_html(), "lxml")
        assert TocantinsScraper._extract_total_count(soup) == 138

    def test_parse_total_pages_from_redesigned_nav_without_contagem(self):
        scraper = _make_scraper()
        html = _make_redesigned_listing_html(count_banner="")
        soup = BeautifulSoup(html, "lxml")
        assert scraper._parse_total_pages(soup, first_page_size=20) == 7

    def test_explicit_empty_results_returns_zero_pages(self):
        scraper = _make_scraper()
        html = b'<html><body><div class="busca-vazio">Nenhum resultado encontrado</div></body></html>'
        soup = BeautifulSoup(html, "lxml")
        assert scraper._parse_total_pages(soup, first_page_size=0) == 0

    def test_raises_when_count_positive_but_zero_items_parsed(self):
        scraper = _make_scraper()
        html = _make_redesigned_listing_html(articles_html="")
        soup = BeautifulSoup(html, "lxml")
        with pytest.raises(ValueError, match="reported 138 results but parsed 0 items"):
            scraper._parse_total_pages(soup, first_page_size=0)

    def test_raises_on_unrecognized_nonempty_html_without_empty_marker(self):
        scraper = _make_scraper()
        html = b"<html><body><main><div class='new-redesign'>Unexpected markup</div></main></body></html>"
        soup = BeautifulSoup(html, "lxml")
        with pytest.raises(ValueError, match="0 parsed items"):
            scraper._parse_total_pages(soup, first_page_size=0)

    @pytest.mark.asyncio
    async def test_fetch_forwards_pdf_executor_and_workers(self):
        scraper = _make_scraper(pdf_executor="thread", pdf_workers=3)
        scraper.http.request = AsyncMock(
            return_value=make_mock_response(b"%PDF-1.5 fake")
        )
        valid_text = "LEI Nº 2.699, DE 21 DE DEZEMBRO DE 2012.\n\n" + (
            "Art. 1º Fica alterada a norma estadual. " * 5
        )
        with patch(
            "legisbr.scrapers.states.to.bytes_to_markdown",
            new=AsyncMock(return_value=valid_text),
        ) as mock_b2m:
            doc = await scraper.fetch(
                {
                    "year": 2012,
                    "type": "Lei Ordinária",
                    "title": "Lei nº 2699 de 2012",
                    "situation": "Não consta",
                    "_pdf_link": "https://www.al.to.leg.br/arquivo/31849",
                }
            )
        assert doc is not None
        assert mock_b2m.await_args.kwargs["pdf_executor"] == "thread"
        assert mock_b2m.await_args.kwargs["pdf_workers"] == 3

    @pytest.mark.asyncio
    async def test_fetch_preserves_valid_gfm_tables_and_chains_exception(self):
        scraper = _make_scraper()
        scraper.http.request = AsyncMock(
            return_value=make_mock_response(b"%PDF-1.5 fake")
        )
        rows = "\n".join(
            f"| {i:02d} | Cargo Técnico Estadual {i} | R$ {i}.000,00 |"
            for i in range(1, 15)
        )
        gfm_markdown = (
            "LEI Nº 2.698, DE 21 DE DEZEMBRO DE 2012.\n\n"
            "| ITEM | DESCRIÇÃO | VALOR |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n"
        )
        with patch(
            "legisbr.scrapers.states.to.bytes_to_markdown",
            new=AsyncMock(return_value=gfm_markdown),
        ):
            doc = await scraper.fetch(
                {
                    "year": 2012,
                    "type": "Lei Ordinária",
                    "title": "Lei nº 2698 de 2012",
                    "situation": "Não consta",
                    "_pdf_link": "https://www.al.to.leg.br/arquivo/31849",
                }
            )
        assert doc is not None
        assert "| ITEM | DESCRIÇÃO | VALOR |" in doc.text_markdown
        assert "| --- | --- | --- |" in doc.text_markdown
        assert "| 14 | Cargo Técnico Estadual 14 | R$ 14.000,00 |" in doc.text_markdown

        # Verify exception chaining (`raise ValueError(...) from e`).
        orig = RuntimeError("pdf decode failure")
        with (
            patch(
                "legisbr.scrapers.states.to.bytes_to_markdown",
                new=AsyncMock(side_effect=orig),
            ),
            pytest.raises(ValueError, match="pdf decode failure") as exc_info,
        ):
            await scraper.fetch(
                {
                    "year": 2012,
                    "type": "Lei Ordinária",
                    "title": "Lei nº 2698 de 2012",
                    "situation": "Não consta",
                    "_pdf_link": "https://www.al.to.leg.br/arquivo/31849",
                }
            )
        assert exc_info.value.__cause__ is orig

    @pytest.mark.asyncio
    async def test_search_refs_narrows_to_requested_type_only(self):
        assert TocantinsScraper.list_documents is BaseScraper.list_documents
        scraper = _make_scraper(years=range(1989, 2026))
        html = _make_redesigned_listing_html(
            count_banner="1 resultado Mostrando 1–1", nav_html=""
        )
        scraper.http.request = AsyncMock(return_value=make_mock_response(html))

        refs = [
            ref
            async for ref in scraper.search_refs(
                Query(years=2024, type="Lei Complementar"), strict_native=True
            )
        ]
        assert len(refs) == 1
        assert refs[0]["type"] == "Lei Complementar"
        scraper.http.request.assert_awaited_once()
        assert scraper.http.request.await_args.kwargs["data"]["documento.tipo"] == (
            "complementar"
        )

        # Constitution is emitted once on narrow range and searchable at 1989 with 0 HTTP calls.
        scraper.http.request.reset_mock()
        const = [
            ref
            async for ref in scraper.search_refs(
                Query(years=1989, type="Constituição Estadual"), strict_native=True
            )
        ]
        assert len(const) == 1 and "documento_68367.PDF" in const[0]["document_url"]
        scraper.http.request.assert_not_awaited()
