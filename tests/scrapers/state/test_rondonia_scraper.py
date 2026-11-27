"Parser and metadata regression tests for rondonia_scraper."

from __future__ import annotations

from bs4 import BeautifulSoup
from conftest import make_mock_response, make_scraper
from parser_assertions import (
    ScraperClassTests,
    SituationsConstantTests,
    TypesConstantTests,
)

from legisbr.core.query import Query
from legisbr.core.scraper import BaseScraper
from legisbr.scrapers.states.ro import SITUATIONS, TYPES, RondoniaCotelScraper


def _make_scraper(**kwargs) -> RondoniaCotelScraper:
    """Instantiate RondoniaCotelScraper with injected dependencies."""
    return make_scraper(RondoniaCotelScraper, **kwargs)


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 5
    REQUIRED_KEYS = {
        "Constituição Estadual",
        "Decreto-Lei",
        "Lei Complementar",
        "Lei Ordinária",
        "Decreto Numerado",
    }
    REQUIRE_INT_VALUES = False


class TestSituationsConstant(SituationsConstantTests):
    SITUATIONS = SITUATIONS
    EXPECTED_TYPE = list
    EXPECTED_EMPTY = True


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = RondoniaCotelScraper
    STATE_NAME = "Rondônia"


class TestFormatSearchUrl:
    def test_includes_type_id(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("leiord", 2023)
        assert "leiord" in url

    def test_includes_year(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("leiord", 2023)
        assert "2023" in url

    def test_includes_base_url(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("declei", 2020)
        assert url.startswith("http://ditel.casacivil.ro.gov.br/COTEL")

    def test_correct_pattern(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("leicomp", 2022)
        assert (
            url
            == "http://ditel.casacivil.ro.gov.br/COTEL/Livros/listleicomp.aspx?ano=2022"
        )


def _make_list_soup(rows_html: str) -> BeautifulSoup:
    """Wrap rows in the expected table structure."""
    html = f'<html><body><table id="ContentPlaceHolder1_DataList1"><tbody>{rows_html}</tbody></table></body></html>'
    return BeautifulSoup(html, "lxml")


def _make_valid_row(
    title: str = "Lei Ordinária 1/2023",
    doc_id: str = "42",
    summary: str = "Dispõe sobre algo.",
    pdf_filename: str = "leiord1.pdf",
    detail_href: str = "/COTEL/Livros/detalhes.aspx?id=42",
) -> str:
    return f'<tr><td><div><a href="{detail_href}">{title}</a><a href="/COTEL/Livros/Files/{pdf_filename}">{pdf_filename}</a><span id="ementadocLabel">{summary}</span><span id="coddocLabel">{doc_id}</span></div></td></tr>'


class TestSearchRefsAndConstitution:
    async def test_search_refs_narrows_to_requested_type_only(self):
        assert RondoniaCotelScraper.list_documents is BaseScraper.list_documents
        scraper = _make_scraper(years=range(1981, 2026))
        html = f'<html><body><table id="ContentPlaceHolder1_DataList1"><tbody>{_make_valid_row(title="Lei Ordinária 1/2024")}</tbody></table></body></html>'
        scraper.http.request.side_effect = [make_mock_response(html)]

        refs = [
            ref
            async for ref in scraper.search_refs(
                Query(years=2024, type="Lei Ordinária"), strict_native=True
            )
        ]
        assert len(refs) == 1
        assert refs[0]["type"] == "Lei Ordinária"
        scraper.http.request.assert_awaited_once_with(
            "http://ditel.casacivil.ro.gov.br/COTEL/Livros/listleiord.aspx?ano=2024"
        )

    async def test_constitution_emitted_once_per_run_and_searchable_by_1989(self):
        scraper = _make_scraper(years=range(2024, 2026))
        y2024 = [
            ref
            async for ref in scraper.list_type(
                2024, "Constituição Estadual", TYPES["Constituição Estadual"]
            )
        ]
        y2025 = [
            ref
            async for ref in scraper.list_type(
                2025, "Constituição Estadual", TYPES["Constituição Estadual"]
            )
        ]
        assert len(y2024) == 1 and y2024[0]["year"] == 1989
        assert y2025 == []

        # Even on a full-range scraper (like Client), searching 1989 finds the Constitution without HTTP calls.
        full_scraper = _make_scraper(years=range(1981, 2026))
        found = [
            ref
            async for ref in full_scraper.search_refs(
                Query(years=1989, type="Constituição Estadual"), strict_native=True
            )
        ]
        assert len(found) == 1
        assert found[0]["document_url"].endswith("/Livros/CE1989-2014.pdf")
        full_scraper.http.request.assert_not_awaited()
