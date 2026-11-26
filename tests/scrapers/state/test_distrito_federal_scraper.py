"""Tests for DFSinjScraper (Distrito Federal)."""

from __future__ import annotations

from conftest import make_scraper
from parser_assertions import (
    ScraperClassTests,
    SituationsConstantTests,
    TypesConstantTests,
)

from legisbr.scrapers.states.df import (
    INVALID_SITUATIONS,
    SITUATIONS,
    TYPES,
    VALID_SITUATIONS,
    DFSinjScraper,
)


def _make_scraper(**kwargs) -> DFSinjScraper:
    defaults = {
        "search_url": "https://www.sinj.df.gov.br/sinj/ashx/Datatable/ResultadoDePesquisaNormaDatatable.ashx",
        "_display_length": 5000,
        "max_workers": 20,
    }
    return make_scraper(DFSinjScraper, **{**defaults, **kwargs})


def _make_source(
    *,
    ch_norma: str = "67024",
    norm_type: str = "Decreto",
    number: str = "32712",
    date: str = "30/12/2010",
    situation: str = "Sem Revogação Expressa",
    summary: str = "Dispõe sobre a criação da faculdade.",
    file_id: str | None = "e25991dc-b67b-393e-83a5-b0a39fb1f6bc",
    mimetype: str = "text/html",
    fontes: list | None = None,
) -> dict:
    return {
        "ch_norma": ch_norma,
        "nm_tipo_norma": norm_type,
        "nr_norma": number,
        "dt_assinatura": date,
        "nm_situacao": situation,
        "ds_ementa": summary,
        "ar_atualizado": {
            "id_file": file_id,
            "filename": "document.html" if mimetype == "text/html" else "document.pdf",
            "mimetype": mimetype,
            "filesize": 1234,
            "uuid": None,
        },
        "fontes": fontes or [],
    }


def _make_search_response(*sources: dict, total: int | None = None) -> dict:
    return {
        "aaData": [{"_source": source} for source in sources],
        "iTotalDisplayRecords": total if total is not None else len(sources),
    }


def _make_html_text_body(
    title_line: str,
    summary: str = "",
    *,
    download_href: str = "./Norma/67028/document.html",
) -> bytes:
    summary_tag = f"\n<p>{summary}</p>" if summary else ""
    download_tag = (
        f'\n<a title="baixar arquivo" class="baixarArquivo" target="_blank" href="{download_href}">baixar arquivo</a>'
        if download_href
        else ""
    )
    return f'<html><body>{download_tag}<div id="div_texto">\nSistema Integrado de Normas Jurídicas do Distrito Federal - SINJ-DF\n{title_line}{summary_tag}\nArt. 1º Texto principal da norma com conteúdo suficiente para validação.\nArt. 2º Disposições complementares da norma do Distrito Federal.\nEste texto não substitui o publicado no DODF.\n</div></body></html>'.encode(
        "utf-8"
    )


def _make_structured_html_body(title_line: str, summary: str = "") -> bytes:
    """HTML with <p> tags in div_texto — matches the download_url format."""
    summary_tag = f"\n<p>{summary}</p>" if summary else ""
    return f'<html><body><div id="div_texto">\n<p>Sistema Integrado de Normas Jurídicas do Distrito Federal - SINJ-DF</p>\n<p>{title_line}</p>{summary_tag}\n<p>Art. 1º Texto principal da norma com conteúdo suficiente para validação.</p>\n<p>Art. 2º Disposições complementares da norma do Distrito Federal.</p>\n<p>Este texto não substitui o publicado no DODF.</p>\n</div></body></html>'.encode(
        "utf-8"
    )


def _make_maintenance_body() -> bytes:
    return b'<html><body><div class="titulo-container">\n    <div style="text-align: center; font-weight: bold; width: 100%; font-size: 25px; margin-bottom: 5px;">\n        <small style="color: black; text-decoration: underline;">\n            Instru\xc3\xa7\xc3\xa3o Normativa N\xc2\xba 20, de 17 de fevereiro de 2025\n        </small>\n    </div>\n    <br><h1>TEXTO EM</h1>\n    <div class="underline-text">MANUTEN\xc3\x87\xc3\x83O</div>\n    <h2>O TEXTO DA NORMA EST\xc3\x81 SENDO REVISADO OU</h2>\n    <h2>ATUALIZADO POR CONTER INFORMA\xc3\x87\xc3\x95ES</h2>\n    <h2>IMPRECISAS.</h2><br>\n    <div class="ComDodf">\n        <h3>O TEXTO DA NORMA PODE SER ACESSADO NA</h3>\n        <h3>PUBLICA\xc3\x87\xc3\x83O DO DI\xc3\x81RIO OFICIAL, CLICANDO\n            <a target="_blank" href="/sinj/BaixarArquivoDiario.aspx?id_file=ef4cb2b7-8d0d-31c1-b786-00e78a974a2c">AQUI.</a>\n        </h3>\n    </div>\n    </div></body></html>'


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 38
    REQUIRE_INT_VALUES = False

    def test_lei_maps_to_expected_value(self):
        assert TYPES["Lei"] == 46000000


class TestSituationsConstant(SituationsConstantTests):
    SITUATIONS = SITUATIONS
    EXPECTED_TYPE = dict
    EXPECTED_EMPTY = False

    def test_situations_count_matches_valid_plus_invalid(self):
        assert len(SITUATIONS) == len(VALID_SITUATIONS) + len(INVALID_SITUATIONS)

    def test_known_situations_present(self):
        assert "Revogado" in SITUATIONS
        assert "Sem Revogação Expressa" in SITUATIONS


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = DFSinjScraper
    STATE_NAME = "Distrito Federal"


class TestBuildPayload:
    def test_year_only_payload(self):
        scraper = _make_scraper()
        payload = scraper._build_payload(2010, offset=5000)
        payload_dict = dict(payload)
        assert payload_dict["bbusca"] == "sinj_norma"
        assert payload_dict["tipo_pesquisa"] == "avancada"
        assert payload_dict["iDisplayStart"] == 5000
        assert payload_dict["iDisplayLength"] == 5000
        assert any((k == "argumento" and "2010" in str(v) for k, v in payload))


class TestHelpers:
    def test_pick_file_info_prefers_ar_atualizado(self):
        scraper = _make_scraper()
        source = _make_source(
            file_id="primary-id",
            fontes=[
                {
                    "ar_fonte": {
                        "id_file": "secondary-id",
                        "filename": "secondary.html",
                        "mimetype": "text/html",
                    }
                }
            ],
        )
        assert scraper._pick_file_info(source)["id_file"] == "primary-id"

    def test_pick_file_info_falls_back_to_fontes(self):
        scraper = _make_scraper()
        source = _make_source(
            file_id=None,
            fontes=[
                {
                    "ar_fonte": {
                        "id_file": "fonte-id",
                        "filename": "fonte.html",
                        "mimetype": "text/html",
                    }
                }
            ],
        )
        assert scraper._pick_file_info(source)["id_file"] == "fonte-id"

    def test_clean_extracted_text_removes_header_and_disclaimer(self):
        scraper = _make_scraper()
        raw = "Legislação correlata - Decreto 28007 de 30/05/2007\nDECRETO Nº 32.712, DE 30 DE DEZEMBRO DE 2010.\nArt. 1º Conteúdo válido da norma.\nEste texto não substitui o publicado no DODF.\n## img-0000 ##"
        cleaned = scraper._clean_extracted_text(
            raw, {"type": "Decreto", "number": "32712"}
        )
        assert cleaned.startswith("DECRETO Nº 32.712")
        assert "Legislação correlata" not in cleaned
        assert "Este texto não substitui" not in cleaned
        assert "## img-0000 ##" not in cleaned

    def test_remove_summary_element_handles_raw_text_nodes(self):
        from bs4 import BeautifulSoup

        scraper = _make_scraper()
        summary = "Dispõe sobre a alteração da estrutura administrativa."
        html = f'<div id="div_texto">DECRETO Nº 48.124, DE 31 DE DEZEMBRO DE 2025\n {summary}\n O GOVERNADOR DO DISTRITO FEDERAL, DECRETA:\n Art. 1º Texto principal.</div>'
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("div", id="div_texto")
        scraper._remove_summary_element(root, summary)
        text = root.get_text(" ", strip=True)
        assert summary not in text
        assert "Art. 1º" in text
        assert "DECRETO Nº 48.124" in text

    def test_clean_extracted_text_does_not_jump_to_late_adi_reference(self):
        scraper = _make_scraper()
        raw = "EMENTA: Ação direta de inconstitucionalidade relevante.\nI. Caso em exame.\n1. Texto principal do julgamento.\nADI 5598 MC/DF (não aplicável ao caso concreto)."
        cleaned = scraper._clean_extracted_text(
            raw, {"type": "ADI", "number": "0713444-19.2025.8.07.0000"}
        )
        assert not cleaned.startswith("ADI 5598")
        assert cleaned.startswith("Ação direta de inconstitucionalidade")
        assert "ADI 5598" in cleaned

    def test_clean_pdf_fallback_text_trims_bulletin_noise(self):
        scraper = _make_scraper()
        raw = "PORTARIA Nº 692, DE 10 DE DEZEMBRO DE 2025\nTexto anterior de outro ato.\n\nPORTARIA Nº 708, DE 17 DE DEZEMBRO DE 2025\nProrroga a vigência do Plano Estratégico Institucional.\nArt. 1º Esta Portaria entra em vigor na data de sua publicação.\nDocumento assinado digitalmente por autoridade competente.\nATOS DA SUBSECRETARIA-GERAL DE ADMINISTRAÇÃO\nORDEM DE SERVIÇO Nº 18, DE 04 DE ABRIL DE 2025\nTexto seguinte."
        cleaned = scraper._clean_pdf_fallback_text(
            raw, {"type": "Portaria", "number": "708"}
        )
        assert cleaned.startswith("PORTARIA Nº 708")
        assert "PORTARIA Nº 692" not in cleaned
        assert "ATOS DA SUBSECRETARIA" not in cleaned
        assert "ORDEM DE SERVIÇO Nº 18" not in cleaned

    def test_remove_summary_element_decomposes_matching_tag(self):
        from bs4 import BeautifulSoup

        scraper = _make_scraper()
        html = '<div id="div_texto">\n        <p>DECRETO Nº 100, DE 01 DE JANEIRO DE 2025</p>\n        <p>Dispõe sobre a criação da faculdade.</p>\n        <p>Art. 1º Texto principal.</p>\n        </div>'
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("div", id="div_texto")
        scraper._remove_summary_element(root, "Dispõe sobre a criação da faculdade.")
        text = root.get_text(" ", strip=True)
        assert "Dispõe sobre a criação da faculdade" not in text
        assert "Art. 1º" in text

    def test_remove_summary_element_skips_tag_with_art(self):
        from bs4 import BeautifulSoup

        scraper = _make_scraper()
        html = '<div id="div_texto">\n        <p>Dispõe sobre a criação. Art. 1º Texto.</p>\n        </div>'
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("div", id="div_texto")
        scraper._remove_summary_element(root, "Dispõe sobre a criação.")
        text = root.get_text(" ", strip=True)
        assert "Dispõe sobre a criação" in text

    def test_strip_summary_text_removes_from_pdf(self):
        scraper = _make_scraper()
        text = "DECRETO Nº 100, DE 01 DE JANEIRO DE 2025\nDispõe sobre a criação da faculdade.\nArt. 1º Texto principal da norma."
        result = scraper._strip_summary_text(
            text, "Dispõe sobre a criação da faculdade."
        )
        assert "Dispõe sobre a criação" not in result
        assert result.startswith("DECRETO Nº 100")
        assert "Art. 1º" in result

    def test_detects_maintenance_placeholder(self):
        from bs4 import BeautifulSoup

        scraper = _make_scraper()
        soup = BeautifulSoup(_make_maintenance_body(), "lxml")
        assert scraper._is_maintenance_soup(soup) is True

    def test_trim_to_title_preserves_opening_bold_markers(self):
        scraper = _make_scraper()
        raw = (
            "Sistema Integrado de Normas Jurídicas do Distrito Federal – SINJ-DF\n\n"
            "**PORTARIA Nº 207, DE 14 DE DEZEMBRO DE 2012.**\n\n"
            "Art. 1º Conteúdo da portaria."
        )
        cleaned = scraper._clean_extracted_text(
            raw, {"type": "Portaria", "number": "207"}
        )
        assert cleaned.startswith("**PORTARIA Nº 207, DE 14 DE DEZEMBRO DE 2012.**")
        assert cleaned.count("**") % 2 == 0

    def test_clean_extracted_text_strips_many_legislacao_correlata_before_trim(self):
        scraper = _make_scraper()
        correlatas = "\n\n".join(
            f"Legislação Correlata - Lei {5200 + i} de 16/12/2013" for i in range(25)
        )
        raw = (
            f"{correlatas}\n\n"
            "LEI Nº 5.011, DE 28 DE DEZEMBRO DE 2012\n\n"
            "Art. 1º Conteúdo verdadeiro da Lei 5.011."
        )
        cleaned = scraper._clean_extracted_text(raw, {"type": "Lei", "number": "5011"})
        assert cleaned.startswith("LEI Nº 5.011, DE 28 DE DEZEMBRO DE 2012")
        assert "Legislação Correlata" not in cleaned
        assert "Lei 5200" not in cleaned

    async def test_extract_html_markdown_passes_fetched_url_as_base_url(self):
        from unittest.mock import AsyncMock

        from bs4 import BeautifulSoup

        scraper = _make_scraper()
        scraper.markdown = AsyncMock(
            return_value="PORTARIA Nº 191, DE 26 DE NOVEMBRO DE 2012.\n\nArt. 1º Texto suficiente para passar na validação de markdown do SINJ-DF."
        )
        soup = BeautifulSoup(
            '<div id="div_texto"><p>PORTARIA Nº 191, DE 26 DE NOVEMBRO DE 2012.</p>'
            '<p><img src="img001.gif">Art. 1º Texto suficiente.</p></div>',
            "lxml",
        )
        download_url = "https://www.sinj.df.gov.br/sinj/Norma/72881/f618b106.html"
        await scraper._extract_html_markdown(
            soup,
            {"type": "Portaria", "number": "191", "document_url": download_url},
            base_url=download_url,
        )
        assert scraper.markdown.await_args.kwargs["base_url"] == download_url

    async def test_direct_pdf_paths_clean_sinj_header_and_disclaimer(self, monkeypatch):
        from unittest.mock import AsyncMock

        from conftest import make_mock_response

        from legisbr.scrapers.states import df as df_mod

        raw_pdf_md = (
            "Sistema Integrado de Normas Jurídicas do Distrito Federal – SINJ-DF\n\n"
            "**PORTARIA Nº 1, DE 2 DE JANEIRO DE 2012**\n\n"
            "Art. 1º Fixa prazos de vencimento do IPVA com texto suficiente para validação.\n\n"
            "Este texto não substitui o publicado no DODF nº 2."
        )
        monkeypatch.setattr(
            df_mod, "bytes_to_markdown", AsyncMock(return_value=raw_pdf_md)
        )
        scraper = _make_scraper()
        scraper.http.request = AsyncMock(
            return_value=make_mock_response(
                b"%PDF-1.5 fake", content_type="application/pdf"
            )
        )
        doc_info = {
            "year": 2012,
            "type": "Portaria",
            "number": "1",
            "title": "Portaria 1 de 02/01/2012",
            "summary": "",
            "situation": "Sem Revogação Expressa",
            "document_url": "https://www.sinj.df.gov.br/sinj/Norma/70283/arquivo",
        }
        doc1 = await scraper._try_text_endpoint(
            dict(doc_info),
            "https://www.sinj.df.gov.br/sinj/TextoArquivoNorma.aspx?id_file=abc",
            doc_info["document_url"],
            True,
            "abc",
        )
        assert "SINJ-DF" not in doc1.text_markdown
        assert "não substitui" not in doc1.text_markdown
        assert doc1.text_markdown.startswith(
            "**PORTARIA Nº 1, DE 2 DE JANEIRO DE 2012**"
        )

        doc2 = await scraper._try_raw_pdf_path(
            dict(doc_info),
            b"%PDF-1.5 fake",
            "norma.pdf",
            "application/pdf",
            doc_info["document_url"],
        )
        assert "SINJ-DF" not in doc2.text_markdown
        assert "não substitui" not in doc2.text_markdown
        assert doc2.text_markdown.startswith(
            "**PORTARIA Nº 1, DE 2 DE JANEIRO DE 2012**"
        )


class TestGetDocsLinks:
    def test_infer_norm_type_uses_title_prefix(self):
        scraper = _make_scraper()
        assert scraper._infer_norm_type("Portaria 708 de 14/01/2025") == "Portaria"
