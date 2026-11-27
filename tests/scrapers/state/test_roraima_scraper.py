"""Tests for RoraimaAlerScraper.

Covers:
- TYPES constant: 12 types with integer IDs
- Class attributes and inherited SAPL year URL formatting
- Roraima-specific header/footer stripping
"""

from __future__ import annotations

from conftest import make_scraper
from parser_assertions import ScraperClassTests, TypesConstantTests

from legisbr.scrapers.states.rr import TYPES, RoraimaAlerScraper


def _make_scraper(**kwargs) -> RoraimaAlerScraper:
    """Instantiate RoraimaAlerScraper with injected dependencies."""
    kwargs.setdefault("subjects", {})
    kwargs.setdefault("_id_to_type", {v: k for k, v in TYPES.items()})
    kwargs.setdefault("_page_size", 100)
    return make_scraper(RoraimaAlerScraper, **kwargs)


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 12
    REQUIRED_KEYS = {
        "Lei Ordinária",
        "Lei Complementar",
        "Constituição Estadual",
        "Emenda à Constituição",
        "Decreto Legislativo",
        "Resolução",
        "Resolução Legislativa",
    }


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = RoraimaAlerScraper
    STATE_NAME = "Roraima"

    def test_situations_empty(self):
        scraper = _make_scraper()
        assert scraper.situations == {}

    def test_subjects_initialized_empty(self):
        scraper = _make_scraper()
        assert scraper.subjects == {}

    def test_id_to_type_built_correctly(self):
        scraper = _make_scraper()
        assert scraper._id_to_type[2] == "Lei Ordinária"
        assert scraper._id_to_type[3] == "Lei Complementar"
        assert len(scraper._id_to_type) == len(TYPES)


class TestFormatYearUrl:
    def test_no_tipo_param(self):
        scraper = _make_scraper()
        url = scraper._format_year_url(2023)
        assert "tipo=" not in url

    def test_includes_year(self):
        scraper = _make_scraper()
        url = scraper._format_year_url(2023)
        assert "ano=2023" in url

    def test_default_page_is_1(self):
        scraper = _make_scraper()
        url = scraper._format_year_url(2023)
        assert "page=1" in url

    def test_includes_page_size(self):
        scraper = _make_scraper()
        url = scraper._format_year_url(2023)
        assert "page_size=100" in url

    def test_custom_page(self):
        scraper = _make_scraper()
        url = scraper._format_year_url(2023, page=5)
        assert "page=5" in url


def test_footer_patterns_strip_roraima_header_and_ocr_glued_footer():
    scraper = _make_scraper()
    text = (
        '"Amazônia: Patrimônio dos Brasileiros"\n'
        "LEI Nº 864, DE 2 DE AGOSTO DE 2012.\n"
        "Art. 1º Fica instituído o programa estadual.\n"
        "Palácio Senador Hélio Campos\n"
        "Praça do CentroCívicos/n° — CEP 69301-380\n"
        "Art. 2º Esta lei entra em vigor na data de sua publicação."
    )
    cleaned = scraper._clean_sapl_pdf_markdown(
        text, "Lei Ordinária nº 864, de 02 de agosto de 2012"
    )
    assert "Amazônia: Patrimônio dos Brasileiros" not in cleaned
    assert "Palácio Senador Hélio Campos" not in cleaned
    assert "CentroCívico" not in cleaned
    assert "Art. 1º Fica instituído" in cleaned
    assert "Art. 2º Esta lei entra em vigor" in cleaned
