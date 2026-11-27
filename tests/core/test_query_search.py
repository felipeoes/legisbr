"""Unit tests for on-demand Query, SearchCapabilities, and native/fallback search."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import make_mock_json_response, make_mock_response, make_scraper

from legisbr.core.http import HttpClient, HttpError
from legisbr.core.query import (
    Query,
    SearchCapabilities,
    UnsupportedQueryError,
    matches_query,
    normalize_search_text,
)
from legisbr.core.scraper import DEFAULT_VALID_SITUATION, BaseScraper
from legisbr.core.store import Store
from legisbr.registry import SCRAPERS, get_spec, sources
from legisbr.scrapers.federal.camara import CamaraDepScraper
from legisbr.scrapers.states.ac import AcreLegisScraper
from legisbr.scrapers.states.ce import CearaAleceScraper
from legisbr.scrapers.states.mg import MGAlmgScraper
from legisbr.scrapers.states.pb import ParaibaAlpbScraper
from legisbr.scrapers.states.pr import ParanaCVScraper
from legisbr.scrapers.states.sp import SaoPauloAlespScraper


def test_query_year_range_and_limit_validation():
    default = range(2020, 2025)
    assert Query().year_range(default) == default
    assert Query(years=2023).year_range(default) == range(2023, 2024)
    assert Query(years=range(2021, 2023)).year_range(default) == range(2021, 2023)
    with pytest.raises(ValueError, match="limit"):
        Query(limit=-1)


def test_search_capabilities_supports_query():
    basic = SearchCapabilities(native_year=True)
    assert basic.supports_query(Query(years=2024))
    assert not basic.supports_query(Query(years=2024, number="10"))
    assert not basic.supports_query(Query(text="saúde"))

    fast = SearchCapabilities(native_year=False, fast_catalog=True)
    assert fast.supports_query(
        Query(years=2024, number="10", type="Lei", text="saúde", situation="Vigente")
    )

    heavy = SearchCapabilities(native_year=False, fast_catalog=False, heavy_setup=True)
    assert not heavy.supports_query(Query())
    assert not heavy.supports_query(Query(years=2024))


def test_normalize_and_matches_query_diacritics_and_numbers():
    assert (
        normalize_search_text("  Constituição   Estadual  ") == "constituicao estadual"
    )

    ref = {
        "year": 2021,
        "type": "Lei Ordinária",
        "title": "Lei Ordinária nº 14.133, de 1º de abril de 2021",
        "summary": "Licitações e Contratos Administrativos na Administração Pública.",
        "situation": "Não consta revogação expressa",
    }
    assert matches_query(
        ref,
        Query(
            years=2021,
            type="lei ordinaria",
            number="14133",
            text="licitacoes administracao publica",
            situation="nao consta",
        ),
    )
    assert matches_query(ref, Query(number="14.133"))
    assert matches_query(ref, Query(number="0014133"))
    assert not matches_query(ref, Query(number="999"))
    assert not matches_query(ref, Query(years=2022))
    assert not matches_query(ref, Query(type="Decreto"))
    assert not matches_query(ref, Query(situation="Revogada"))
    assert not matches_query(ref, Query(text="tributario"))

    # Explicit number field prevents matching the year token in the title.
    ref_with_num = {
        "year": 2024,
        "number": "42",
        "title": "Lei nº 42/2024",
        "type": "Lei",
        "summary": "",
        "situation": DEFAULT_VALID_SITUATION,
    }
    assert matches_query(ref_with_num, Query(number="042"))
    assert not matches_query(ref_with_num, Query(number="2024"))


async def test_camara_native_search_url_and_early_limit():
    scraper = make_scraper(CamaraDepScraper)
    assert scraper._format_search_url("2024", "Lei") == (
        "https://www.camara.leg.br/legislacao/busca?"
        "abrangencia=&geral=&ano=2024&situacao=&origem=&numero=&ordenacao=data%3AASC&tipo=Lei"
    )
    html = """
    <html><body>
      <div class="busca-info__resultado busca-info__resultado--informado">Mostrando 1-20 de 45</div>
      <ul>
        <li class="busca-resultados__item">
          <h3 class="busca-resultados__cabecalho">
            <a href="https://www2.camara.leg.br/legin/fed/lei/2021/lei-14133-1-abril-2021.html">Lei nº 14.133, de 1º de abril de 2021</a>
          </h3>
          <p class="busca-resultados__descricao js-fade-read-more">Ementa: Lei de Licitações e Contratos.</p>
          <p class="busca-resultados__situacao">Situação: Não consta revogação expressa</p>
        </li>
        <li class="busca-resultados__item">
          <h3 class="busca-resultados__cabecalho">
            <a href="https://www2.camara.leg.br/legin/fed/lei/2021/lei-14134-8-abril-2021.html">Lei nº 14.134, de 8 de abril de 2021</a>
          </h3>
          <p class="busca-resultados__descricao js-fade-read-more">Ementa: Nova Lei do Gás.</p>
          <p class="busca-resultados__situacao">Situação: Não consta revogação expressa</p>
        </li>
      </ul>
    </body></html>
    """
    scraper.http.request = AsyncMock(return_value=make_mock_response(html))

    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(
                years=2021,
                type="Lei Ordinária",
                number="14133",
                text="licitações",
                situation="Não consta revogação expressa",
                limit=1,
            ),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert "14.133" in refs[0]["title"]
    assert refs[0]["year"] == 2021
    assert refs[0]["type"] == "Lei Ordinária"
    # Only page 1 was requested despite total=45 because limit=1 stopped early.
    scraper.http.request.assert_awaited_once()
    called_url = scraper.http.request.await_args.args[0]
    qs = parse_qs(urlsplit(called_url).query, keep_blank_values=True)
    assert qs["ano"] == ["2021"]
    assert qs["numero"] == ["14133"]
    assert qs["tipo"] == ["Lei Ordinária"]
    assert qs["geral"] == ["licitações"]
    assert qs["situacao"] == ["Não consta revogação expressa"]


async def test_sapl_native_search_skips_assuntonorma_and_lazy_setup():
    scraper = make_scraper(ParaibaAlpbScraper)
    payload = {
        "pagination": {"total_pages": 3},
        "results": [
            {
                "id": 99,
                "numero": "1234",
                "__str__": "Lei Ordinária nº 1234/2024",
                "ementa": "Institui programa estadual de saúde pública.",
                "assuntos": [7],
                "data": "2024-03-01",
                "ano": 2024,
                "tipo": ParaibaAlpbScraper.types["Lei Ordinária"],
                "data_vigencia": None,
                "texto_integral": "https://sapl.al.pb.leg.br/media/lei1234.pdf",
            }
        ],
    }
    scraper.http.request = AsyncMock(return_value=make_mock_json_response(payload))

    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(
                years=2024,
                type="lei ordinaria",
                number="1234",
                text="saude",
                limit=1,
            ),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert refs[0]["id"] == 99
    # Must NOT have called assuntonorma or page 2.
    scraper.http.request.assert_awaited_once()
    called_url = scraper.http.request.await_args.args[0]
    assert "/api/norma/normajuridica/" in called_url
    assert "assuntonorma" not in called_url
    qs = parse_qs(urlsplit(called_url).query)
    assert qs["ano"] == ["2024"]
    assert qs["numero"] == ["1234"]
    assert qs["ementa"] == ["saude"]
    assert qs["tipo"] == [str(ParaibaAlpbScraper.types["Lei Ordinária"])]

    # Unsupported filter in strict_native mode raises immediately.
    with pytest.raises(UnsupportedQueryError):
        [
            ref
            async for ref in scraper.search_refs(
                Query(situation="Revogada"), strict_native=True
            )
        ]

    # Lazy _ensure_subjects() is idempotent on repeated setup() calls.
    scraper.http.request = AsyncMock(
        return_value=make_mock_json_response(
            {
                "pagination": {"total_pages": 1},
                "results": [{"id": 7, "assunto": "Saúde"}],
            }
        )
    )
    await scraper.setup()
    await scraper.setup()
    assert scraper.subjects == {7: "Saúde"}
    scraper.http.request.assert_awaited_once()


async def test_sp_native_search_avoids_situation_loop_and_filters_natively():
    scraper = make_scraper(SaoPauloAlespScraper)
    payload = {
        "totalElements": 1,
        "content": [
            {
                "idNorma": 555,
                "tipo": "Lei",
                "nomeNorma": "Lei nº 17.890, de 10/04/2024",
                "dsEmenta": "Dispõe sobre transparência fiscal.",
                "nuNorma": "17890",
                "ano": 2024,
                "data": "2024-04-10",
                "urlTextoAtualizado": "https://www.al.sp.gov.br/repositorio/legislacao/lei/2024/lei-17890.html",
            }
        ],
    }
    scraper.http.request = AsyncMock(return_value=make_mock_json_response(payload))

    # Without situation filter, SP issues 1 request omitting idsSituacao (not 6 loops!).
    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(years=2024, type="Lei", number="17890", limit=5),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert refs[0]["document_url"] == "https://www.al.sp.gov.br/norma/555"
    scraper.http.request.assert_awaited_once()
    qs = parse_qs(urlsplit(scraper.http.request.await_args.args[0]).query)
    assert qs["ano"] == ["2024"]
    assert qs["numero"] == ["17890"]
    assert qs["idTipoNorma"] == ["9"]
    assert "idsSituacao" not in qs

    # With situation filter, idsSituacao is passed natively.
    scraper.http.request = AsyncMock(return_value=make_mock_json_response(payload))
    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(years=2024, situation="Revogada", limit=5), strict_native=True
        )
    ]
    assert len(refs) == 1 and refs[0]["situation"] == "Revogada"
    qs = parse_qs(urlsplit(scraper.http.request.await_args.args[0]).query)
    assert qs["idsSituacao"] == ["5"]

    # Non-native text filter raises under strict_native=True.
    with pytest.raises(UnsupportedQueryError):
        [
            ref
            async for ref in scraper.search_refs(
                Query(text="transparência"), strict_native=True
            )
        ]


async def test_mg_native_search_builds_num_q_ano_tipo_url():
    scraper = make_scraper(MGAlmgScraper)
    html = """
    <html><body>
      <p>20 artigos encontrados</p>
      <article>
        <a href="/legislacao-mineira/LEI/24500/2024/">Lei nº 24.500, de 2024</a>
        Institui política estadual de educação básica.
      </article>
    </body></html>
    """
    scraper.http.request = AsyncMock(return_value=make_mock_response(html))

    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(
                years=2024,
                type="Lei",
                number="24500",
                text="educação",
                limit=1,
            ),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert refs[0]["type"] == "Lei"
    assert refs[0]["year"] == 2024
    scraper.http.request.assert_awaited_once()
    qs = parse_qs(
        urlsplit(scraper.http.request.await_args.args[0]).query, keep_blank_values=True
    )
    assert qs["ano"] == ["2024"]
    assert qs["num"] == ["24500"]
    assert qs["q"] == ["educação"]
    assert qs["tipo"] == ["LEI"]

    with pytest.raises(UnsupportedQueryError):
        [
            ref
            async for ref in scraper.search_refs(
                Query(situation="Revogada"), strict_native=True
            )
        ]


async def test_acre_fast_catalog_search_single_get():
    scraper = make_scraper(AcreLegisScraper)
    catalog_html = """
    <html><body>
      <div id="lei_ordinarias"><table>
        <tr class="visaoQuadrosTr">
          <td><a href="/lei/101">Lei Ordinária nº 101, de 10 de maio de 2024</a></td>
          <td>Dispõe sobre proteção ambiental no Acre.</td>
        </tr>
        <tr class="visaoQuadrosTr">
          <td><a href="/lei/102">Lei Ordinária nº 102, de 12 de maio de 2023</a></td>
          <td>Outra ementa qualquer.</td>
        </tr>
      </table></div>
      <div id="lei_complementares"><table></table></div>
      <div id="lei_decretos"><table></table></div>
      <div id="emendas"><table></table></div>
    </body></html>
    """
    scraper.http.request = AsyncMock(return_value=make_mock_response(catalog_html))

    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(years=2024, number="101", text="ambiental", limit=5),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert refs[0]["title"] == "Lei Ordinária nº 101, de 10 de maio de 2024"
    scraper.http.request.assert_awaited_once()


async def test_parana_native_search_and_fetch_by_id():
    scraper = make_scraper(ParanaCVScraper)
    results_html = """
    <html><body>
      <table id="list_tabela">
        <tr class="list_cor_sim">
          <td><a href="JavaScript:exibir('/legislacao/pesquisarAto.do?action=exibir&amp;codAto=342000');">Ver</a></td>
          <td>Lei 21900 - 15 de março de 2024</td>
          <td>Dispõe sobre diretrizes estaduais.</td>
          <td>15/03/2024</td>
        </tr>
      </table>
    </body></html>
    """
    scraper.http.request = AsyncMock(return_value=make_mock_response(results_html))

    refs = [
        ref
        async for ref in scraper.search_refs(
            Query(years=2024, type="Lei", number="21900", limit=5),
            strict_native=True,
        )
    ]
    assert len(refs) == 1
    assert refs[0]["id"] == "342000"
    assert refs[0]["year"] == 2024
    scraper.http.request.assert_awaited_once()
    call = scraper.http.request.await_args
    assert "pesquisarAto.do?action=pesquisar" in call.args[0]
    assert "listarAtosAno.do" not in call.args[0]
    assert call.kwargs["data"]["codTipoAto"] == "1"
    assert call.kwargs["data"]["numeroAto"] == "21900"
    assert call.kwargs["data"]["anoAto"] == "2024"

    # Direct fetch_by_id issues a single GET and builds a validated Document.
    act_html = """
    <html><body>
      <form name="pesquisarAtoForm">
        <p>Lei 21900 - 15 de março de 2024</p>
        <p>Art. 1º Fica instituída a política estadual de modernização administrativa no Paraná.</p>
      </form>
    </body></html>
    """
    scraper.http.request = AsyncMock(return_value=make_mock_response(act_html))
    doc = await scraper.fetch_by_id("342000")
    scraper.http.request.assert_awaited_once_with(
        "https://www.legislacao.pr.gov.br/legislacao/pesquisarAto.do?action=exibir&codAto=342000"
    )
    assert doc.year == 2024
    assert doc.type == "Lei"
    assert "modernização administrativa" in doc.text_markdown


async def test_base_fallback_narrows_list_type_stops_early_and_routes_errors_without_store():
    called_types: list[tuple[int | None, str]] = []

    class DummyTypedScraper(BaseScraper):
        name = "DUMMY_TYPED"
        base_url = "https://example.org"
        types = {"Lei Ordinária": "lei", "Decreto": "dec"}
        capabilities = SearchCapabilities(native_year=True, native_type=True)

        async def list_type(self, year, name, value):
            called_types.append((year, name))
            if year == 2023:
                raise HttpError("https://example.org", 503, "down")
            for i in range(1, 10):
                yield {
                    "year": year,
                    "type": name,
                    "title": f"{name} nº {i}/{year}",
                    "summary": "Texto de exemplo",
                    "situation": DEFAULT_VALID_SITUATION,
                    "document_url": f"https://example.org/{value}/{year}/{i}",
                }

    errors: list[tuple[dict, Exception]] = []

    async def on_error(ref: dict, exc: Exception) -> None:
        errors.append((ref, exc))

    async with Store(":memory:") as store:
        scraper = DummyTypedScraper(
            store=store, http=HttpClient(), years=range(2023, 2026)
        )
        refs = [
            ref
            async for ref in scraper.search_refs(
                Query(years=range(2023, 2026), type="lei ordinaria", limit=2),
                on_error=on_error,
            )
        ]
        # Narrowed strictly to Lei Ordinária (never called Decreto) and stopped at limit=2 in 2024 (never hit 2025).
        assert called_types == [(2023, "Lei Ordinária"), (2024, "Lei Ordinária")]
        assert len(refs) == 2
        assert len(errors) == 1 and errors[0][0]["year"] == 2023
        # Store errors table must remain completely untouched.
        assert (
            store.connection.execute("SELECT count(*) FROM errors").fetchone()[0] == 0
        )


async def test_strict_native_rejects_ceara_and_unsupported_queries():
    ce = make_scraper(CearaAleceScraper)
    with pytest.raises(UnsupportedQueryError):
        [ref async for ref in ce.search_refs(Query(years=2024), strict_native=True)]
    with pytest.raises(UnsupportedQueryError):
        [ref async for ref in ce.search_refs(Query(), strict_native=True)]


def test_registry_capabilities_match_sources_and_stay_lazy():
    infos = sources()
    assert len(infos) == 30
    assert all(isinstance(info.capabilities, SearchCapabilities) for info in infos)

    for spec in SCRAPERS:
        cls = spec.load_cls()
        assert spec.capabilities == cls.capabilities, (
            f"Capabilities mismatch for {spec.code}: "
            f"registry={spec.capabilities!r} vs class={cls.capabilities!r}"
        )
        assert spec.info().capabilities == spec.capabilities
        if spec.capabilities.native_type and cls.search_refs is BaseScraper.search_refs:
            assert cls.list_documents is BaseScraper.list_documents, (
                f"{spec.code} declares native_type=True without overriding search_refs "
                "but overrides list_documents(), bypassing list_type() narrowing"
            )
            assert isinstance(cls.types, dict) and bool(cls.types)

    assert get_spec("camara").capabilities.native_text is True
    assert get_spec("ac").capabilities.fast_catalog is True
    assert get_spec("pr").capabilities.direct_id is True
    assert get_spec("ce").capabilities.heavy_setup is True
    assert len(SCRAPERS) == 30

    code = (
        "import sys, time, legisbr; "
        "t = time.perf_counter(); "
        "s = legisbr.sources(); "
        "dt = (time.perf_counter() - t) * 1000; "
        "assert len(s) == 30; "
        "assert 'aiohttp' not in sys.modules and 'bs4' not in sys.modules and 'pydantic' not in sys.modules; "
        "print(f'ok {dt:.2f}ms')"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


async def test_icmbio_strict_native_rejects_year_filter_and_am_accepts_year_type():
    from legisbr.scrapers.environment.icmbio import ICMBioScraper
    from legisbr.scrapers.states.am import LegislaAMScraper

    icmbio = make_scraper(ICMBioScraper)
    icmbio.http.request = AsyncMock()
    with pytest.raises(UnsupportedQueryError):
        [ref async for ref in icmbio.search_refs(Query(years=2024), strict_native=True)]
    icmbio.http.request.assert_not_awaited()

    am = make_scraper(LegislaAMScraper)
    am.http.request = AsyncMock(
        return_value=make_mock_response("<html><body><ul></ul></body></html>")
    )
    refs = [
        ref
        async for ref in am.search_refs(
            Query(years=2024, type="Lei Ordinária"), strict_native=True
        )
    ]
    assert refs == []
    am.http.request.assert_awaited_once()
    assert "/diario_am/12/2024?page=1" in am.http.request.await_args.args[0]
