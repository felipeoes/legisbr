"Parser and metadata regression tests for sapl_scraper."

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from conftest import make_scraper

from legisbr.core.sapl import SAPLBaseScraper

TYPES: dict[str, int] = {"Lei Ordinária": 2, "Lei Complementar": 1, "Decreto": 3}


def _make_scraper(**kwargs) -> SAPLBaseScraper:
    """Create a SAPLBaseScraper instance bypassing __init__."""
    kwargs.setdefault("subjects", {})
    kwargs.setdefault("_id_to_type", {v: k for k, v in TYPES.items()})
    kwargs.setdefault("_page_size", 100)
    return make_scraper(SAPLBaseScraper, **kwargs)


def _make_api_item(
    doc_id: int = 1,
    texto_integral: str | None = "https://sapl.example.leg.br/media/lei.pdf",
    tipo: int = 2,
    title_str: str = "Lei Ordinária 1/2023",
    ano: int = 2023,
    **kwargs,
) -> dict:
    """Build a single SAPL API result item."""
    return {
        "id": doc_id,
        "numero": str(doc_id),
        "__str__": title_str,
        "link_detail_backend": f"/norma/{doc_id}",
        "ementa": "Ementa da norma.",
        "assuntos": [],
        "data": "2023-01-01",
        "ano": ano,
        "esfera_federacao": None,
        "veiculo_publicacao": None,
        "texto_integral": texto_integral,
        "data_vigencia": None,
        "tipo": tipo,
        **kwargs,
    }


class TestFindContentStart:
    """Tests for _find_content_start (CC=26, ~83 lines)."""

    def _find(self, lines: list[str], expected_title: str | None = None) -> int:
        scraper = _make_scraper()
        return scraper._find_content_start(lines, expected_title)

    def test_exact_title_match(self):
        lines = [
            "GOVERNO DO ESTADO",
            "GABINETE DO GOVERNADOR",
            "Lei Ordinária nº 123/2023",
            "Art. 1º Fica criado...",
        ]
        idx = self._find(lines, expected_title="Lei Ordinária nº 123/2023")
        assert idx == 2

    def test_title_match_case_insensitive(self):
        lines = ["Cabeçalho qualquer", "LEI ORDINÁRIA Nº 123/2023", "Art. 1º Algo"]
        idx = self._find(lines, expected_title="Lei Ordinária nº 123/2023")
        assert idx == 1

    def test_fuzzy_title_score_match(self):
        """When exact match fails, token scoring picks the best candidate."""
        lines = ["Página do estado", "LEI COMPLEMENTAR 456 de 2023", "Art. 1º Texto."]
        idx = self._find(lines, expected_title="Lei Complementar nº 456/2023")
        assert idx == 1

    def test_fallback_to_numbered_sapl_title(self):
        """Without an expected title, picks the last numbered SAPL title line."""
        lines = ["GOVERNO DO ESTADO", "DECRETO 99/2022", "Art. 1º Algo qualquer."]
        idx = self._find(lines, expected_title=None)
        assert idx == 1

    def test_returns_zero_for_empty_lines(self):
        assert self._find([], expected_title="Decreto 1/2023") == 0

    def test_returns_zero_when_no_markers(self):
        """Lines with no SAPL title patterns should return 0."""
        lines = ["Algum texto qualquer", "Mais texto sem padrão"]
        assert self._find(lines, expected_title=None) == 0

    def test_prefers_candidate_before_first_article(self):
        """When multiple title-like lines exist, picks one before Art. 1º."""
        lines = [
            "LEI 10/2020",
            "Art. 1º Primeiro artigo.",
            "LEI 20/2020",
            "Art. 2º Segundo artigo.",
        ]
        idx = self._find(lines, expected_title=None)
        assert idx == 0

    def test_title_starts_with_expected(self):
        """A line that starts with the expected title should be matched."""
        lines = ["Preâmbulo", "Decreto nº 5 - Regulamenta a Lei X", "Art. 1º Texto."]
        idx = self._find(lines, expected_title="Decreto nº 5")
        assert idx == 1


class TestGetDocsLinks:
    """Tests for _get_docs_links — JSON parsing from SAPL API."""

    @pytest.mark.asyncio
    async def test_parses_single_item(self):
        scraper = _make_scraper()
        item = _make_api_item(doc_id=10, tipo=1)
        data = {"results": [item]}
        result = await scraper._get_docs_links("https://api.example.com/", data=data)
        assert len(result) == 1
        assert result[0]["id"] == 10
        assert result[0]["type"] == scraper._resolve_type_name(1)
        assert result[0]["_pdf_link"] == item["texto_integral"]
        assert result[0]["title"] == "Lei Ordinária 1/2023"

    @pytest.mark.asyncio
    async def test_infers_valid_situation_when_no_data_vigencia(self):
        scraper = _make_scraper()
        item = _make_api_item(data_vigencia=None)
        data = {"results": [item]}
        result = await scraper._get_docs_links("https://api.example.com/", data=data)
        assert result[0]["situation"] == "Não consta revogação expressa"

    @pytest.mark.asyncio
    async def test_infers_invalid_situation_when_data_vigencia_set(self):
        scraper = _make_scraper()
        item = _make_api_item(data_vigencia="2024-01-01")
        data = {"results": [item]}
        result = await scraper._get_docs_links("https://api.example.com/", data=data)
        assert result[0]["situation"] == "Revogada"


async def test_fetch_failure_names_each_variant_cause():
    """The errors table must say why every attachment variant failed."""
    scraper = _make_scraper(base_url="https://sapl.example.leg.br")
    scraper.download = AsyncMock(
        side_effect=[ValueError("Expected PDF"), ValueError("PDF OCR failed")]
    )
    ref = {"title": "Lei 1", "_pdf_link": "http://sapl.example.leg.br/media/lei.pdf"}
    with pytest.raises(ValueError) as caught:
        await scraper.fetch(ref)
    assert "Expected PDF" in str(caught.value)
    assert "PDF OCR failed" in str(caught.value)


BODY = "\n".join(
    f"Art. {n}º Fica estabelecida a disposição número {n} desta norma estadual."
    for n in range(1, 40)
)


class TestCleanSaplPdfMarkdownKeepsTheLaw:
    """Regressions from the 2012 live run: 63 PB/PI/RR documents lost most of their text."""

    def test_late_sentence_starting_with_lei_does_not_cut_the_body(self):
        # The PDF text layer splits the title across lines, so only the late sentence looks numbered.
        text = f"ESTADO DA PARAÍBA\nLEI N°\n9.725\n, DE\n29\nDE\nMAIO\nDE 2012\n{BODY}\nLei no prazo de 90 (noventa) dias.\nPALÁCIO DO GOVERNO"
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(
            text, "Lei Ordinária nº 9.725, de 29 de maio de 2012"
        )
        assert "Art. 1º" in cleaned and "Art. 39º" in cleaned

    def test_title_found_late_in_a_reversed_text_layer_keeps_everything(self):
        text = f"{BODY}\nDECRETO LEGISLATIVO N° 337 DE 3 DE JULHO DE 2012\nASSEMBLEIA"
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(
            text, "Decreto Legislativo nº 337, de 03 de julho de 2012"
        )
        assert "Art. 1º" in cleaned

    def test_footer_block_does_not_swallow_annex_tables(self):
        table = "\n".join(
            f"| 4313/{n} | Cargo {n} | R$ 325,00 |" for n in range(10, 30)
        )
        text = f"LEI Nº 877\n{BODY}\nAssinatura eletrônica do Presidente\n{table}"
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(text, "Lei nº 877")
        assert "Cargo 29" in cleaned and "Assinatura" not in cleaned

    def test_extra_document_is_cut_only_after_the_closing_formula(self):
        text = (
            f"LEI Nº 9.884\n{BODY[:200]}\nexpediente normal na repartição fiscal, observado o § 2°\n"
            f"{BODY[200:]}\nPALÁCIO DO GOVERNO DO ESTADO, em João Pessoa\nOfício Pres. SGM nº 12/2012\nSenhor Governador"
        )
        scraper = _make_scraper(_EXTRA_DOC_START_PATTERNS=("ofício pres. sgm",))
        cleaned = scraper._clean_sapl_pdf_markdown(text, "Lei nº 9.884")
        assert "Art. 39º" in cleaned and "expediente normal" in cleaned
        assert "Senhor Governador" not in cleaned

    def test_markdown_formatted_heading_trims_preceding_diario_oficial_tail(self):
        text = (
            "§ 2º Resto de norma anterior no Diário Oficial.\n\n"
            "## LEI Nº 6.292, DE 19 DE DEZEMBRO DE 2012\n\n"
            f"{BODY}"
        )
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(
            text, "Lei nº 6.292, de 19 de dezembro de 2012"
        )
        assert cleaned.startswith("## LEI Nº 6.292, DE 19 DE DEZEMBRO DE 2012")
        assert "Resto de norma anterior" not in cleaned

    def test_ocr_merged_lein0_header_is_matched_and_cross_reference_is_ignored(self):
        text = (
            '"Amazônia: Patrimônio dos Brasileiros"\n'
            "LEIN0 869 DE 13 DE NOVEMBRO DE 2012.\n"
            'Altera a redação da\nLei n° 431/2004, que "Declara área turística".\n'
            "O GOVERNADOR DO ESTADO DE RORAIMA\n"
            "Faço saber que a Assembleia aprovou e eu sanciono a seguinte\n"
            "Lei:\n"
            f"{BODY}"
        )
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(
            text, "Lei Ordinária nº 869, de 13 de novembro de 2012"
        )
        assert cleaned.startswith("LEIN0 869 DE 13 DE NOVEMBRO DE 2012.")
        assert "Lei n° 431/2004" in cleaned

    def test_mid_body_promulgating_lei_colon_never_chops_header(self):
        text = (
            "ESTADO DA PARAÍBA\n"
            "LEI N°\n"
            "9.755\n"
            "Dispõe sobre utilidade pública.\n"
            "Faço saber que a Assembleia decreta e eu sanciono a seguinte\n"
            "Lei:\n"
            f"{BODY}"
        )
        cleaned = _make_scraper()._clean_sapl_pdf_markdown(
            text, "Lei Ordinária nº 9.755, de 08 de junho de 2012"
        )
        assert not cleaned.startswith("Lei:")
        assert "Dispõe sobre utilidade pública." in cleaned
        assert "Art. 1º" in cleaned

    def test_five_digit_unformatted_norm_number_does_not_conflict_with_day(self):
        scraper = _make_scraper()
        assert (
            scraper._conflicts_with_expected_number(
                "LEI Nº 13500, DE 15 DE MAIO DE 2024", "13500"
            )
            is False
        )
        assert (
            scraper._select_best_title_candidate(
                ["ESTADO DA PARAÍBA", "LEI Nº 13500"],
                [1],
                expected_title="Lei Ordinária nº 13500",
            )
            == 1
        )
        text = (
            "DIÁRIO OFICIAL DO ESTADO\n"
            "TEXTO DE ATO ANTERIOR\n"
            "LEI Nº 13500, DE 15 DE MAIO DE 2024\n"
            f"{BODY}"
        )
        cleaned = scraper._clean_sapl_pdf_markdown(
            text, "Lei Ordinária nº 13500, de 15 de maio de 2024"
        )
        assert cleaned.startswith("LEI Nº 13500, DE 15 DE MAIO DE 2024")
        assert "TEXTO DE ATO ANTERIOR" not in cleaned
