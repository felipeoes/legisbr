"Parser and metadata regression tests for mato_grosso_do_sul_scraper."

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import make_mock_response, make_scraper
from parser_assertions import (
    ScraperClassTests,
    SituationsConstantTests,
    TypesConstantTests,
)

from legisbr.core.scraper import DEFAULT_INVALID_SITUATION
from legisbr.scrapers.states.ms import SITUATIONS, TYPES, MSAlemsScraper

_NSF_BASE = "/appls/legislacao/secoge/govato.nsf"


def _make_scraper(**kwargs) -> MSAlemsScraper:
    """Instantiate MSAlemsScraper with injected dependencies."""
    defaults = dict(
        _nsf=f"https://aacpdappls.net.ms.gov.br{_NSF_BASE}", _type_year_index={}
    )
    defaults.update(kwargs)
    return make_scraper(MSAlemsScraper, **defaults)


def _make_category_xml(
    categories: list[tuple[int, int, int]], *, year_field_name: str = "wano"
) -> str:
    """Build ReadViewEntries XML with year categories.

    *categories* is a list of (year, position, descendants).
    """
    entries = ""
    for year, pos, desc in categories:
        entries += f'<viewentry position="{pos}" noteid="ABCD{pos:04X}" children="{desc}" descendants="{desc}" siblings="{len(categories)}">\n  <entrydata columnnumber="0" name="{year_field_name}" category="true"><number>{year}</number></entrydata>\n  <entrydata columnnumber="4" name="wtotal"><number>{desc}</number></entrydata>\n</viewentry>'
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<viewentries toplevelentries="{len(categories)}">\n{entries}\n</viewentries>'


def _make_docs_xml(
    expand_pos: int, docs: list[dict], *, summary_field_name: str = "wementa"
) -> str:
    """Build ReadViewEntries XML with document entries inside a category.

    *docs* is a list of {"unid": ..., "title": ..., "summary": ...}.
    """
    cat_entry = f'<viewentry position="{expand_pos}" noteid="ABCD{expand_pos:04X}" children="{len(docs)}" descendants="{len(docs)}">\n  <entrydata columnnumber="0" name="wano" category="true"><number>2025</number></entrydata>\n</viewentry>\n'
    doc_entries = ""
    for i, doc in enumerate(docs, 1):
        doc_entries += f'''<viewentry position="{expand_pos}.{i}" unid="{doc["unid"]}" noteid="50{i:04X}" siblings="{len(docs)}">\n  <entrydata columnnumber="2" name="wnumero"><text>{doc["title"]}</text></entrydata>\n  <entrydata columnnumber="3" name="{summary_field_name}"><text>{doc.get("summary", "")}</text></entrydata>\n</viewentry>\n'''
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<viewentries toplevelentries="{len(docs)}">\n{cat_entry}{doc_entries}</viewentries>'


def _make_fetch_bytes_result(
    html: str, *, charset: str = "utf-8"
) -> tuple[bytes, MagicMock]:
    body = html.encode(charset)
    response = MagicMock()
    response.charset = charset
    return (body, response)


def _make_xml_response(xml_content: str) -> MagicMock:
    response = MagicMock()
    response.__bool__ = lambda s: True
    response.text = AsyncMock(return_value=xml_content)
    return response


def _make_doc_html(
    *,
    title: str = "LEI Nº 1/2025",
    summary: str = "Resumo da norma.",
    notes_html: str = "<b><font face='Tahoma'>Publicada no Diário Oficial.</font></b><br>",
    body_html: str = "<font face='Tahoma'>Art. 1.</font><br><font face='Tahoma'>Art. 2.</font>",
) -> str:
    return f"""\n<html><body>\n<form action=''>\n<table border="1"><tr><td>nav actions</td></tr></table>\n<hr>\n<ul><ul>\n<table border="0" cellspacing="0" cellpadding="0">\n<tr valign="top"><td width="559"><b><font face="Tahoma">{title}</font></b>\n<table border="0" cellspacing="0" cellpadding="0">\n<tr valign="top"><td width="276"><img src="/icons/ecblank.gif" border="0" height="1" width="1" alt=""></td><td width="282"><i><font face="Tahoma">{summary}</font></i></td></tr>\n</table>\n<font face="Tahoma">Notas:</font><br>\n{notes_html}\n</td></tr></table>\n</ul></ul>\n<table border="0" cellspacing="0" cellpadding="0">\n<tr valign="top"><td width="562">{body_html}</td></tr>\n</table>\n</form>\n</body></html>\n""".strip()


class TestTypesConstant(TypesConstantTests):
    TYPES = TYPES
    EXPECTED_COUNT = 12
    REQUIRED_KEYS = {"Emenda Constitucional", "Lei Estadual", "Decreto E Conjunto"}
    REQUIRE_INT_VALUES = False


class TestSituationsConstant(SituationsConstantTests):
    SITUATIONS = SITUATIONS
    EXPECTED_TYPE = dict
    EXPECTED_EMPTY = False

    def test_nao_consta_key_present(self):
        assert "Não consta" in SITUATIONS

    def test_revogada_key_present(self):
        assert DEFAULT_INVALID_SITUATION in SITUATIONS


class TestClassAttributes(ScraperClassTests):
    SCRAPER_CLS = MSAlemsScraper
    STATE_NAME = "Mato Grosso do Sul"


class TestViewEntriesUrl:
    def test_basic_url_structure(self):
        scraper = _make_scraper()
        url = scraper._view_entries_url("/Decreto")
        assert url.startswith("https://aacpdappls.net.ms.gov.br")
        assert "/Decreto?ReadViewEntries" in url

    def test_default_start_and_count(self):
        scraper = _make_scraper()
        url = scraper._view_entries_url("/Lei%20Estadual")
        assert "Start=1" in url
        assert "Count=200" in url

    def test_custom_start_and_count(self):
        scraper = _make_scraper()
        url = scraper._view_entries_url("/Decreto", start=5, count=100)
        assert "Start=5" in url
        assert "Count=100" in url

    def test_expand_included_when_provided(self):
        scraper = _make_scraper()
        url = scraper._view_entries_url("/Decreto", expand=3)
        assert "Expand=3" in url

    def test_expand_absent_when_not_provided(self):
        scraper = _make_scraper()
        url = scraper._view_entries_url("/Decreto")
        assert "Expand" not in url


class TestDocUrl:
    def test_uses_zero_prefix(self):
        scraper = _make_scraper()
        url = scraper._doc_url("ABCDEF1234567890ABCDEF1234567890")
        assert "/0/ABCDEF1234567890ABCDEF1234567890?OpenDocument" in url

    def test_includes_base_url(self):
        scraper = _make_scraper()
        url = scraper._doc_url("ABCD")
        assert url.startswith("https://aacpdappls.net.ms.gov.br")


class TestEntryText:
    def _make_entry(self, col_name: str, value: str, tag: str = "text") -> ET.Element:
        entry = ET.Element("viewentry")
        data = ET.SubElement(entry, "entrydata", name=col_name)
        el = ET.SubElement(data, tag)
        el.text = value
        return entry

    def test_returns_text_value(self):
        entry = self._make_entry("wnumero", "LEI Nº 1/2025")
        assert MSAlemsScraper._entry_text(entry, "wnumero") == "LEI Nº 1/2025"

    def test_returns_number_value(self):
        entry = self._make_entry("wico1", "42", tag="number")
        assert MSAlemsScraper._entry_text(entry, "wico1") == "42"

    def test_missing_col_returns_empty(self):
        entry = self._make_entry("wnumero", "some value")
        assert MSAlemsScraper._entry_text(entry, "nonexistent") == ""

    def test_strips_whitespace(self):
        entry = self._make_entry("wnumero", "  Lei 1  ")
        assert MSAlemsScraper._entry_text(entry, "wnumero") == "Lei 1"

    def test_tuple_of_field_names_uses_first_nonempty_match(self):
        entry = ET.Element("viewentry")
        ET.SubElement(
            ET.SubElement(entry, "entrydata", name="wementa"), "text"
        ).text = ""
        ET.SubElement(
            ET.SubElement(entry, "entrydata", name="$246"), "text"
        ).text = "Resumo alternativo"
        assert (
            MSAlemsScraper._entry_text(entry, ("wementa", "$246"))
            == "Resumo alternativo"
        )


@pytest.mark.parametrize("stage", ["categories", "entries"])
async def test_malformed_view_xml_fails_instead_of_empty_year(stage):
    from legisbr.core.http import Response

    good = _make_category_xml([(2012, 1, 2)])
    bodies = (
        [b"<html>maintenance", b""]
        if stage == "categories"
        else [good.encode(), b"<html>maintenance"]
    )
    scraper = _make_scraper()
    scraper.http.request = AsyncMock(
        side_effect=[Response("u", 200, {}, body) for body in bodies]
    )
    with pytest.raises(ET.ParseError):
        await scraper._get_type_year_docs("Lei", "lei", 2012)
    assert "Lei" not in scraper._type_year_index or stage == "entries"


class TestTitleNormalization:
    def test_inserts_space_before_glued_numero(self):
        assert (
            MSAlemsScraper._normalize_title(
                "MENSAGEM GABGOV/MSnº 57, de 26 de Novembro de 2012."
            )
            == "MENSAGEM GABGOV/MS nº 57, de 26 de Novembro de 2012."
        )
        assert (
            MSAlemsScraper._normalize_title(
                "RESOLUÇÃO SEGOV/MSnº 81, de 23 de Novembro de 2012."
            )
            == "RESOLUÇÃO SEGOV/MS nº 81, de 23 de Novembro de 2012."
        )
        assert (
            MSAlemsScraper._normalize_title(
                "DECRETO Nº 13.488, DE 6 DE SETEMBRO DE 2012."
            )
            == "DECRETO Nº 13.488, DE 6 DE SETEMBRO DE 2012."
        )


class TestFetchDominoCleaning:
    @pytest.mark.asyncio
    async def test_unwraps_layout_tables_preserves_data_tables_and_cleans_chrome(self):
        scraper = _make_scraper()
        html = """
        <html><body>
        <form action="">
          <table border="1" cellspacing="2" cellpadding="2">
            <tr>
              <td><a href="" onclick="return _doClick('x/$V5ACTIONS/0.1', this, null)"><img src="/icons/actn112.gif"/> Sair</a></td>
              <td><a href="/doc?EditDocument"><img src="/icons/actn005.gif"/> Editar</a></td>
              <td><a href="" onclick="return _doClick('x/$V5ACTIONS/0.2', this, null)">Excluir</a></td>
              <td><a href="/open?OpenForm">Novo Anexo</a></td>
            </tr>
          </table>
          <hr>
          <ul><ul>
            <b><i><font face="Tahoma">(*) ESTE TEXTO NÃO SUBSTITUI O ORIGINAL PUBLICADO NO DIÁRIO OFICIAL DO ESTADO.</font></i></b><br>
            <table border="0"><tr><td><div><b><font face="Tahoma">ESTADO DE MATO GROSSO DO SUL</font></b></div></td></tr></table>
            <table border="0"><tr><td>
              <b><font face="Tahoma">DECRETO N<s><font>º</font></s> 13.442, DE 5 DE JUNHO DE 2012.</font></b>
              <table border="0"><tr><td><img src="/icons/ecblank.gif"/></td><td><i><font face="Tahoma">Dispõe sobre cadastros ambientais.</font></i></td></tr></table>
              <font face="Tahoma">Notas:</font><br>
              <b><font face="Tahoma">Publicada no Diário Oficial.</font></b><br>
            </td></tr></table>
          </ul></ul>
          <table border="0">
            <tr><td>
              <font face="Tahoma">O GOVERNADOR DO ESTADO DE MATO GROSSO DO SUL decreta:</font><br><br>
              <font face="Tahoma">Art. 1<s><font>º</font></s> Ficam aprovadas as tabelas do Anexo I.</font><br><br>
              <font face="Tahoma">Art. 2<s><font>º</font></s> Este Decreto entra em vigor na data de sua publicação.</font><br><br>
              <table border="1">
                <tr><th>Código</th><th>Categoria</th></tr>
                <tr><td>01</td><td>Extração e Tratamento de Minerais</td></tr>
                <tr><td>02</td><td>Indústria Química</td></tr>
              </table>
              <a href="/appls/legislacao/secoge/govato.nsf/0/abc/$FILE/anexo.doc"><img src="/icons/fileatt.gif"/><br>anexo.doc</a>
            </td></tr>
          </table>
        </form>
        </body></html>
        """
        url = "https://aacpdappls.net.ms.gov.br/appls/legislacao/secoge/govato.nsf/0/abc?OpenDocument"
        scraper.http.request = AsyncMock(return_value=make_mock_response(html))
        doc = await scraper.fetch(
            {
                "year": 2012,
                "type": "Decreto",
                "title": "DECRETO Nº 13.442, DE 5 DE JUNHO DE 2012.",
                "summary": "Dispõe sobre cadastros ambientais.",
                "situation": "Não consta",
                "_html_link": url,
            }
        )
        assert doc is not None
        md = doc.text_markdown
        # Domino toolbar, disclaimer, and state banner stripped.
        assert "Sair" not in md
        assert "Novo Anexo" not in md
        assert "ESTE TEXTO NÃO SUBSTITUI" not in md
        assert "ESTADO DE MATO GROSSO DO SUL\n" not in md
        # Layout tables unwrapped so Art. 1º and Art. 2º are on separate lines (not pipe-collapsed).
        assert any("Art. 1º" in ln and "Art. 2º" not in ln for ln in md.splitlines())
        assert "Art. 2º" in md
        # Real border="1" data table preserved!
        assert re.search(r"\|\s*01\s*\|\s*Extração e Tratamento de Minerais\s*\|", md)
        assert re.search(r"\|\s*02\s*\|\s*Indústria Química\s*\|", md)
        # $FILE attachment icon link does not produce broken image syntax.
        assert "![anexo.doc]" not in md
        assert "/$FILE/" not in md
        assert "anexo.doc" in md
        # Strikethrough ordinals cleaned.
        assert "~~º~~" not in md

    @pytest.mark.asyncio
    async def test_hr_prefix_check_ignores_head_style_and_title_text(self):
        scraper = _make_scraper()
        html = (
            "<html><head>"
            "<title>Sistema de Legislação Estadual do Mato Grosso do Sul - Governo de MS</title>"
            "<style>body { font-family: Tahoma, Arial, Helvetica, sans-serif; margin: 12px; }</style>"
            "</head><body>"
            "<hr>"
            "<p>DECRETO Nº 13.442, DE 5 DE JUNHO DE 2012.</p>"
            "<p>Art. 1º Fica instituída a política estadual de gestão documental no Estado.</p>"
            "<hr>"
            "<p>Art. 2º Este Decreto entra em vigor na data de sua publicação.</p>"
            "</body></html>"
        )
        url = "https://aacpdappls.net.ms.gov.br/appls/legislacao/secoge/govato.nsf/0/abc?OpenDocument"
        scraper.http.request = AsyncMock(return_value=make_mock_response(html))
        doc = await scraper.fetch(
            {
                "year": 2012,
                "type": "Decreto",
                "title": "DECRETO Nº 13.442, DE 5 DE JUNHO DE 2012.",
                "summary": "",
                "situation": "Não consta",
                "_html_link": url,
            }
        )
        assert doc is not None
        # Leading Domino <hr> stripped even though <head> had >50 chars; body <hr> preserved.
        assert not doc.text_markdown.lstrip().startswith("---")
        assert "---" in doc.text_markdown
