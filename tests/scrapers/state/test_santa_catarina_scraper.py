"Parser and metadata regression tests for santa_catarina_scraper."

from __future__ import annotations

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
from legisbr.scrapers.states.sc import SITUATIONS, TYPES, SantaCatarinaScraper


def _make_scraper(**kwargs) -> SantaCatarinaScraper:
    """Instantiate SantaCatarinaScraper with injected dependencies."""
    return make_scraper(SantaCatarinaScraper, **kwargs)


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 3
    REQUIRED_KEYS = {"Constituição Estadual", "Legislativo", "Executivo"}
    REQUIRE_INT_VALUES = False


class TestSituationsConstant(SituationsConstantTests):
    SITUATIONS = SITUATIONS
    EXPECTED_TYPE = list
    EXPECTED_EMPTY = True


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = SantaCatarinaScraper
    STATE_NAME = "Santa Catarina"


class TestFormatSearchUrl:
    def test_includes_path(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("legislativo", 2023)
        assert "legislativo" in url

    def test_includes_year(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("legislativo", 2023)
        assert "ano=2023" in url

    def test_starts_with_base_url(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("executivo", 2020)
        assert url.startswith("https://leis.alesc.sc.gov.br")

    def test_executivo_url(self):
        scraper = _make_scraper()
        url = scraper._format_search_url("executivo", 2022)
        assert url == "https://leis.alesc.sc.gov.br/executivo?ano=2022"


def _make_link_soup(links_html: str) -> BeautifulSoup:
    html = f"<html><body>{links_html}</body></html>"
    return BeautifulSoup(html, "lxml")


def _make_card_html(
    href: str = "/ato-normativo/legislativo/123",
    title: str = "Lei 1/2023",
    ementa: str = "Dispõe sobre algo.",
) -> str:
    return f'<div class="card-item"><a href="{href}">{title}</a><div class="ementa">{ementa}</div></div>'


_FOOTER = (
    '<footer><ul><li><a href="/ato-normativo/legislativo/52785">Completa</a></li>'
    '<li><a href="/ato-normativo/legislativo/52785">Constituição</a></li></ul></footer>'
)


def _listing(cards: str = "") -> str:
    """Every listing page repeats the constitution links in its footer."""
    return f"<html><body><p>Resultados da Pesquisa</p>{cards}{_FOOTER}</body></html>"


def _card(href: str, title: str) -> str:
    return (
        f'<div class="mb-3 card"><div class="card-body"><div class="card-title h5">'
        f'<span class="external-link-wrapper"><a href="{href}">{title}</a></span>'
        f"</div></div></div>"
    )


class TestListing:
    async def test_footer_constitution_links_are_not_listed(self):
        scraper = _make_scraper()
        scraper.http.request.side_effect = [
            make_mock_response(
                _listing(_card("/ato-normativo/legislativo/1", "LEI Nº 1"))
            ),
            make_mock_response(_listing()),
        ]
        refs = [
            ref async for ref in scraper.list_type(2024, "Legislativo", "legislativo")
        ]
        assert [ref["title"] for ref in refs] == ["LEI Nº 1"]

    async def test_constitution_listed_once(self):
        scraper = _make_scraper()
        scraper.http.request.side_effect = [
            make_mock_response(
                _listing(_card("/ato-normativo/legislativo/1", "LEI Nº 1"))
            ),
            make_mock_response(_listing()),
            make_mock_response(
                _listing(_card("/ato-normativo/executivo/2", "DEC-000002 01/01/2024"))
            ),
            make_mock_response(_listing()),
        ]
        refs = [ref async for ref in scraper.list_documents(2024)]
        constitutions = [ref for ref in refs if "52785" in ref["document_url"]]
        assert len(constitutions) == 1
        assert constitutions[0]["type"] == "Constituição Estadual"
        assert len(refs) == 3

    async def test_malformed_listing_raises(self):
        scraper = _make_scraper()
        scraper.http.request.side_effect = [make_mock_response("<html>erro</html>")]
        with pytest.raises(ValueError, match="listing"):
            [ref async for ref in scraper.list_type(2024, "Executivo", "executivo")]


def _doc_page(title: str) -> str:
    return (
        f"<html><head><title>{title}</title></head><body><nav>menu</nav>"
        f"<p>{title}</p><p>Art. 1º Fica regulamentado o procedimento administrativo "
        "estadual de teste.</p></body></html>"
    )


class TestFetch:
    @staticmethod
    def _ref(
        title: str, url: str = "https://leis.alesc.sc.gov.br/ato-normativo/executivo/1"
    ) -> dict:
        return {
            "year": 2012,
            "type": "Executivo",
            "title": title,
            "document_url": url,
            "situation": "Não consta",
        }

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("DEC-000952 08/05/2012", "Decreto"),
            ("DEL-000414 13/03/1947", "Decreto-Lei"),
            ("INT-000001 23/01/2012", "Instrução Normativa"),
            ("MPV-000180 28/04/2010", "Medida Provisória"),
            ("ONI-000001 07/03/2025", "Orientação Normativa"),
            ("PRT-001365 21/08/2002", "Portaria"),
            ("PRV-000001 09/08/2012", "Provimento"),
            ("RES-000006 03/12/2001", "Resolução"),
            ("LEI COMPLEMENTAR Nº 582, de 30 de novembro de 2012", "Lei Complementar"),
            ("ATO DA MESA Nº 191, de 12 de abril de 2012", "Ato da Mesa"),
        ],
    )
    async def test_type_from_site_code(self, title, expected):
        scraper = _make_scraper()
        body = _doc_page(title)
        scraper.http.request.side_effect = [make_mock_response(body)]
        doc = await scraper.fetch(self._ref(title))
        assert doc.type == expected
        assert doc.raw == body.encode()
        assert doc.raw_ext == ".html"
        assert "procedimento administrativo" in doc.text_markdown

    async def test_unknown_code_raises_instead_of_becoming_type(self):
        scraper = _make_scraper()
        scraper.http.request.side_effect = [make_mock_response(_doc_page("XYZ-000001"))]
        with pytest.raises(ValueError, match="XYZ"):
            await scraper.fetch(self._ref("XYZ-000001 23/01/2012"))

    async def test_constitution_keeps_its_type(self):
        scraper = _make_scraper()
        title = "CONSTITUIÇÃO DO ESTADO DE SANTA CATARINA DE 1989"
        scraper.http.request.side_effect = [make_mock_response(_doc_page(title))]
        ref = {
            **self._ref(title),
            "type": "Constituição Estadual",
            "constitution": True,
        }
        doc = await scraper.fetch(ref)
        assert doc.type == "Constituição Estadual"

    async def test_populates_summary_and_situation_from_executivo_meta_tags(self):
        scraper = _make_scraper()
        html = (
            '<html><head><meta name="Ementa" content="APROVA A CLASSIFICAÇÃO DA\n  DESPESA PUBLICA.">'
            '<meta name="Vigente" content="Sim"></head>'
            "<body><p>DECRETO Nº 1.323</p><p>Art. 1º Fica aprovada a classificação da despesa estadual.</p></body></html>"
        )
        scraper.http.request.side_effect = [make_mock_response(html)]
        doc = await scraper.fetch(self._ref("DEC-001323 21/12/2012"))
        assert doc.summary == "APROVA A CLASSIFICAÇÃO DA DESPESA PUBLICA."
        assert doc.situation == "Vigente"

        html_nao = html.replace('content="Sim"', 'content="Nao"')
        scraper.http.request.side_effect = [make_mock_response(html_nao)]
        doc_nao = await scraper.fetch(self._ref("PRV-000001 09/08/2012"))
        assert doc_nao.situation == "Não vigente"

    async def test_populates_summary_and_revocation_from_legislativo_html(self):
        scraper = _make_scraper()
        html = (
            "<html><body>"
            "<p>Consolidada e Revogada pela Lei nº 16.719/2015</p>"
            '<p class="ementa">Institui o Dia Estadual\n do Garçom.</p>'
            "<p>Art. 1º Fica instituído o Dia Estadual do Garçom no Estado de Santa Catarina.</p>"
            "</body></html>"
        )
        scraper.http.request.side_effect = [make_mock_response(html)]
        doc = await scraper.fetch(
            self._ref(
                "LEI Nº 15.805, de 13 de abril de 2012",
                "https://leis.alesc.sc.gov.br/ato-normativo/legislativo/1",
            )
        )
        assert doc.summary == "Institui o Dia Estadual do Garçom."
        assert doc.situation == "Revogada"

    async def test_extracts_revocation_after_stripping_large_header_nav_chrome(self):
        scraper = _make_scraper()
        nav_items = "".join(f"<span>Item {i}</span>" for i in range(50))
        html = (
            f"<html><body><header><nav>{nav_items}</nav></header>"
            "<main>"
            "<p>Consolidada e Revogada pela Lei nº 16.719/2015</p>"
            '<p class="ementa">Institui norma de teste.</p>'
            "<p>Art. 1º Fica instituído o programa estadual de teste em Santa Catarina.</p>"
            "</main></body></html>"
        )
        scraper.http.request.side_effect = [make_mock_response(html)]
        doc = await scraper.fetch(
            self._ref(
                "LEI Nº 15.805, de 13 de abril de 2012",
                "https://leis.alesc.sc.gov.br/ato-normativo/legislativo/1",
            )
        )
        assert doc.situation == "Revogada"

    async def test_search_refs_narrows_to_requested_type_only(self):
        assert SantaCatarinaScraper.list_documents is BaseScraper.list_documents
        scraper = _make_scraper(years=range(1946, 2026))
        scraper.http.request.side_effect = [
            make_mock_response(
                _listing(_card("/ato-normativo/legislativo/1", "LEI Nº 1"))
            ),
            make_mock_response(_listing()),
        ]
        refs = [
            ref
            async for ref in scraper.search_refs(
                Query(years=2024, type="Legislativo"), strict_native=True
            )
        ]
        assert [ref["title"] for ref in refs] == ["LEI Nº 1"]
        called_urls = [call.args[0] for call in scraper.http.request.await_args_list]
        assert all("/legislativo?ano=2024" in url for url in called_urls)
        assert not any("/executivo" in url for url in called_urls)

        # Constitution is searchable by year 1989 with zero HTTP calls.
        scraper.http.request.reset_mock()
        const = [
            ref
            async for ref in scraper.search_refs(
                Query(years=1989, type="Constituição Estadual"), strict_native=True
            )
        ]
        assert len(const) == 1 and "52785" in const[0]["document_url"]
        scraper.http.request.assert_not_awaited()
