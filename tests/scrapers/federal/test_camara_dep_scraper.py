"Parser and metadata regression tests for camara_dep_scraper."

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup
from conftest import make_mock_response, make_scraper

from legisbr.core.sink import NullSink
from legisbr.scrapers.federal.camara import TYPES, CamaraDepScraper

TEST_YEAR = 2020
TEST_TYPE = "Decreto"
TEST_TYPE_ID = TYPES[TEST_TYPE]
PER_PAGE = 20


def build_unit_scraper():
    return make_scraper(CamaraDepScraper)


def test_types_dict_completeness():
    """TYPES must contain 'Decreto' and other key legislative types."""
    required = [
        "Decreto",
        "Lei Ordinária",
        "Lei Complementar",
        "Decreto-Lei",
        "Emenda Constitucional",
        "Medida Provisória",
        "Decreto Legislativo",
        "Ato da Mesa",
        "Ato do Presidente Sem Número",
        "Ato da Presidência Sem Número",
        "Decisão da Mesa Sem Número",
        "Resolução",
        "Ato",
        "Ato Sem Número",
    ]
    for t in required:
        assert t in TYPES, f"TYPES missing required key: {t!r}"


class TestTypeResolution:
    def test_resolve_norm_type_prefers_metadata_slug(self):
        scraper = build_unit_scraper()
        assert (
            scraper._resolve_norm_type(
                "Lei nº 15.323, de 6 de Janeiro de 2026",
                metadata_url="https://www2.camara.leg.br/legin/fed/lei/2026/lei-15323-6-janeiro-2026-798626-norma-pl.html",
            )
            == "Lei Ordinária"
        )

    def test_resolve_norm_type_maps_slug_only_titles(self):
        scraper = build_unit_scraper()
        assert (
            scraper._resolve_norm_type(
                "Carta de Lei  de 29 de Novembro de 1808",
                metadata_url="https://www2.camara.leg.br/legin/fed/carlei_sn/anterioresa1824/cartadelei-40273-29-novembro-1808-572462-norma-pe.html",
            )
            == "Carta de Lei"
        )


class TestListingParsing:
    def test_parse_export_documents_extracts_metadata(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <html><body>\n              <div id="impressaoPDF">\n                <ul class="busca-resultados">\n                  <li>\n                    <a href="https://www2.camara.leg.br/legin/fed/decret/example-norma.html">\n                      Decreto nº 1\n                    </a>\n                    <p><span class="bold color-gray">Ementa:</span> Dispõe sobre teste.</p>\n                    <p><span class="bold color-gray">Situação: </span>Não consta revogação expressa</p>\n                  </li>\n                </ul>\n              </div>\n            </body></html>\n            ',
            "lxml",
        )
        docs = scraper._parse_export_documents(soup)
        assert docs == [
            {
                "title": "Decreto nº 1",
                "summary": "Dispõe sobre teste.",
                "metadata_url": "https://www2.camara.leg.br/legin/fed/decret/example-norma.html",
                "situation": "Não consta revogação expressa",
            }
        ]


class TestExtractTotalResults:
    """Unit tests for CamaraDepScraper._extract_total_results."""

    @staticmethod
    def _wrap(inner_html: str) -> BeautifulSoup:
        return BeautifulSoup(f"<html><body>{inner_html}</body></html>", "lxml")

    def test_extracts_count_from_well_formed_div(self):
        soup = self._wrap(
            '<div class="busca-info__resultado busca-info__resultado--informado">  Foram encontrados 42</div>'
        )
        assert CamaraDepScraper._extract_total_results(soup) == 42

    def test_returns_none_when_div_missing(self):
        soup = self._wrap("<p>nothing here</p>")
        assert CamaraDepScraper._extract_total_results(soup) is None

    def test_returns_none_when_text_has_no_number(self):
        soup = self._wrap(
            '<div class="busca-info__resultado busca-info__resultado--informado">  Nenhum documento encontrado</div>'
        )
        assert CamaraDepScraper._extract_total_results(soup) is None

    def test_extracts_large_count(self):
        soup = self._wrap(
            '<div class="busca-info__resultado busca-info__resultado--informado">  Encontrados 12345</div>'
        )
        assert CamaraDepScraper._extract_total_results(soup) == 12345


class TestParseListingPageDocuments:
    """Unit tests for CamaraDepScraper._parse_listing_page_documents."""

    def test_parses_single_result_item(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <ul>\n              <li class="busca-resultados__item">\n                <h3 class="busca-resultados__cabecalho">\n                  <a href="https://www2.camara.leg.br/legin/fed/decret/2020/norma.html">\n                    Decreto nº 10.000\n                  </a>\n                </h3>\n                <p class="busca-resultados__descricao js-fade-read-more">\n                  Ementa: Dispõe sobre coisas.\n                </p>\n                <p class="busca-resultados__situacao">\n                  Situação: Não consta revogação expressa\n                </p>\n              </li>\n            </ul>\n            ',
            "lxml",
        )
        docs = scraper._parse_listing_page_documents(soup)
        assert len(docs) == 1
        assert docs[0]["title"] == "Decreto nº 10.000"
        assert docs[0]["summary"] == "Dispõe sobre coisas."
        assert docs[0]["situation"] == "Não consta revogação expressa"
        assert docs[0]["metadata_url"].endswith("norma.html")

    def test_skips_items_without_heading(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <ul>\n              <li class="busca-resultados__item">\n                <p>No heading here</p>\n              </li>\n            </ul>\n            ',
            "lxml",
        )
        assert scraper._parse_listing_page_documents(soup) == []

    def test_skips_items_without_anchor(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <ul>\n              <li class="busca-resultados__item">\n                <h3 class="busca-resultados__cabecalho">\n                  Plain text, no link\n                </h3>\n              </li>\n            </ul>\n            ',
            "lxml",
        )
        assert scraper._parse_listing_page_documents(soup) == []

    def test_handles_missing_summary_and_situation(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <ul>\n              <li class="busca-resultados__item">\n                <h3 class="busca-resultados__cabecalho">\n                  <a href="https://example.com/doc">Decreto nº 5</a>\n                </h3>\n              </li>\n            </ul>\n            ',
            "lxml",
        )
        docs = scraper._parse_listing_page_documents(soup)
        assert len(docs) == 1
        assert docs[0]["summary"] == ""
        assert docs[0]["situation"] == ""

    def test_parses_multiple_items(self):
        scraper = build_unit_scraper()
        soup = BeautifulSoup(
            '\n            <ul>\n              <li class="busca-resultados__item">\n                <h3 class="busca-resultados__cabecalho">\n                  <a href="https://example.com/d1">Lei nº 1</a>\n                </h3>\n              </li>\n              <li class="busca-resultados__item">\n                <h3 class="busca-resultados__cabecalho">\n                  <a href="https://example.com/d2">Lei nº 2</a>\n                </h3>\n              </li>\n            </ul>\n            ',
            "lxml",
        )
        docs = scraper._parse_listing_page_documents(soup)
        assert len(docs) == 2
        assert docs[0]["title"] == "Lei nº 1"
        assert docs[1]["title"] == "Lei nº 2"


class TestFacetsAndDeterministicListing:
    def test_extract_type_facets_parses_refine_busca_links(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <aside class="refine-busca">
                <a href="https://www.camara.leg.br/legislacao/busca?tipo=Decreto Legislativo&ano=2012">
                  Decreto Legislativo 590 filtros encontrados
                </a>
                <a href="https://www.camara.leg.br/legislacao/busca?tipo=Lei Ordinária&ano=2012">
                  Lei Ordinária 193 filtros encontrados
                </a>
                <a href="https://www.camara.leg.br/legislacao/busca?tipo=&ano=2012">Todos 783</a>
              </aside>
            </body></html>
            """,
            "lxml",
        )
        facets = CamaraDepScraper._extract_type_facets(soup)
        assert facets == [
            ("Decreto+Legislativo", 590),
            ("Lei+Ordin%C3%A1ria", 193),
        ]

    @pytest.mark.asyncio
    async def test_list_documents_uses_export_for_small_year(self):
        scraper = build_unit_scraper()
        listing_html = """
        <html><body>
          <div class="busca-info__resultado busca-info__resultado--informado">Foram encontrados 25</div>
        </body></html>
        """
        export_items = "".join(
            f'<li><a href="https://www2.camara.leg.br/legin/fed/lei/2012/lei-{i}.html">Lei nº {i}</a>'
            f"<p>Ementa: Ementa {i}.</p><p>Situação: Vigente</p></li>"
            for i in range(1, 26)
        )
        export_html = f'<html><body><div id="impressaoPDF"><ul>{export_items}</ul></div></body></html>'

        async def fake_request(url: str, **_kw):
            if "busca/exportar?" in url:
                return make_mock_response(export_html)
            return make_mock_response(listing_html)

        scraper.http.request = AsyncMock(side_effect=fake_request)
        refs = [r async for r in scraper.list_documents(2012)]
        assert len(refs) == 25
        assert scraper._result.errors == 0
        assert any(
            "busca/exportar?" in call.args[0]
            for call in scraper.http.request.await_args_list
        )

    @pytest.mark.asyncio
    async def test_list_documents_partitions_by_facets_and_bisects_large_type(self):
        scraper = build_unit_scraper()
        year_html = """
        <html><body>
          <div class="busca-info__resultado busca-info__resultado--informado">Foram encontrados 305</div>
          <aside class="refine-busca">
            <a href="https://www.camara.leg.br/legislacao/busca?tipo=Decreto Legislativo&ano=2012">
              Decreto Legislativo 302 filtros encontrados
            </a>
            <a href="https://www.camara.leg.br/legislacao/busca?tipo=Lei Ordinária&ano=2012">
              Lei Ordinária 3 filtros encontrados
            </a>
          </aside>
        </body></html>
        """

        def _make_export(prefix: str, start: int, count: int) -> str:
            lis = "".join(
                f'<li><a href="https://www2.camara.leg.br/legin/fed/{prefix}/2012/{prefix}-{i}.html">'
                f"Norma {prefix} {i}</a><p>Ementa: E {i}.</p></li>"
                for i in range(start, start + count)
            )
            return (
                f'<html><body><div id="impressaoPDF"><ul>{lis}</ul></div></body></html>'
            )

        async def fake_request(url: str, **_kw):
            if "tipo=&" in url or url.endswith("tipo="):
                return make_mock_response(year_html)
            if "tipo=Lei+Ordin%C3%A1ria" in url:
                return make_mock_response(_make_export("lei", 1, 3))
            if "dataInicio=01/01/2012&dataFim=31/12/2012" in url:
                return make_mock_response(
                    '<div class="busca-info__resultado busca-info__resultado--informado">302</div>'
                )
            if "dataInicio=01/01/2012&dataFim=01/07/2012" in url:
                if "exportar" in url:
                    return make_mock_response(_make_export("decleg", 1, 150))
                return make_mock_response(
                    '<div class="busca-info__resultado busca-info__resultado--informado">150</div>'
                )
            if "dataInicio=02/07/2012&dataFim=31/12/2012" in url:
                if "exportar" in url:
                    return make_mock_response(_make_export("decleg", 151, 152))
                return make_mock_response(
                    '<div class="busca-info__resultado busca-info__resultado--informado">152</div>'
                )
            raise AssertionError(f"Unexpected URL: {url}")

        scraper.http.request = AsyncMock(side_effect=fake_request)
        refs = [r async for r in scraper.list_documents(2012)]
        assert len(refs) == 305
        assert len({r["document_url"] for r in refs}) == 305
        assert scraper._result.errors == 0

    @pytest.mark.asyncio
    async def test_list_documents_records_error_when_unique_count_short(self):
        scraper = make_scraper(CamaraDepScraper, store=NullSink())
        listing_html = """
        <html><body>
          <div class="busca-info__resultado busca-info__resultado--informado">Foram encontrados 5</div>
          <ul>
            <li class="busca-resultados__item">
              <h3 class="busca-resultados__cabecalho">
                <a href="https://www2.camara.leg.br/legin/fed/lei/2012/lei-1.html">Lei nº 1</a>
              </h3>
            </li>
          </ul>
        </body></html>
        """
        scraper.http.request = AsyncMock(return_value=make_mock_response(listing_html))
        refs = [r async for r in scraper.list_documents(2012)]
        assert len(refs) == 1
        assert scraper._result.errors == 1


class TestFetchFormattingAndEmenta:
    @pytest.mark.asyncio
    async def test_fetch_separates_dir_amended_articles_and_drops_nested_ementa(self):
        scraper = build_unit_scraper()
        summary = "Altera dispositivos do Regulamento dos Serviços de Radiodifusão."
        doc_html = f"""
        <html><body>
          <div id="content">
            <h1>Decreto nº 7.670, de 16 de Janeiro de 2012</h1>
            <p class="ementa"><p>{summary}</p></p>
            <div class="texto">
              <p>A PRESIDENTA DA REPÚBLICA decreta:</p>
              <p>Art. 1º O regulamento passa a vigorar com as seguintes alterações:</p>
              <dir>"Art. 28. Texto do artigo vinte e oito." (NR)</dir><dir>"Art. 29. Texto do artigo vinte e nove." (NR)</dir>
            </div>
          </div>
        </body></html>
        """
        text_url = "https://www2.camara.leg.br/legin/fed/decret/2012/decreto-7670-publicacaooriginal.html"
        scraper.http.request = AsyncMock(return_value=make_mock_response(doc_html))
        scraper._get_document_text_link = AsyncMock(
            return_value={
                "title": "Decreto nº 7.670, de 16 de Janeiro de 2012",
                "_norm_type": "Decreto",
                "summary": summary,
                "situation": "Não consta revogação expressa",
                "metadata_url": "https://www2.camara.leg.br/legin/fed/decret/2012/decreto-7670-norma.html",
                "document_url": text_url,
            }
        )
        doc = await scraper.fetch(
            {
                "year": 2012,
                "type": "Decreto",
                "title": "Decreto nº 7.670, de 16 de Janeiro de 2012",
                "summary": summary,
                "situation": "Não consta revogação expressa",
                "metadata_url": "https://www2.camara.leg.br/legin/fed/decret/2012/decreto-7670-norma.html",
            }
        )
        assert doc is not None
        assert summary not in doc.text_markdown
        assert '(NR)"Art. 29' not in doc.text_markdown
        assert '"Art. 28.' in doc.text_markdown
        assert '"Art. 29.' in doc.text_markdown
        # Verify Art. 28 and Art. 29 are on separate lines.
        for line in doc.text_markdown.splitlines():
            assert not ("Art. 28." in line and "Art. 29." in line)
