"""Tests for core infrastructure modules.

Covers:
- clean_md_tag utility
- RateLimiter concurrency primitive
- BaseScraper markdown utilities and helpers
"""

from __future__ import annotations

import asyncio

import pytest

from legisbr.core.http import RateLimiter
from legisbr.core.ocr import clean_md_tag


class TestCleanMdTag:
    def test_strip_markdown_fence(self):
        assert clean_md_tag("```markdown\n# Hello\n```") == "# Hello"

    def test_strip_md_fence(self):
        assert clean_md_tag("```md\n# Hello\n```") == "# Hello"

    def test_no_fence(self):
        assert clean_md_tag("# Hello") == "# Hello"

    def test_only_opening_fence(self):
        assert clean_md_tag("```markdown\n# Hello") == "# Hello"

    def test_only_closing_fence(self):
        assert clean_md_tag("# Hello\n```") == "# Hello"

    def test_empty_string(self):
        assert clean_md_tag("") == ""

    def test_preserves_inner_backticks(self):
        result = clean_md_tag("```markdown\nsome `code` here\n```")
        assert "`code`" in result


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_does_not_block_below_limit(self):
        limiter = RateLimiter(1000)
        start = asyncio.get_event_loop().time()
        for _ in range(5):
            await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 0.5

    @pytest.mark.asyncio
    async def test_acquire_rate_limits(self):
        limiter = RateLimiter(2)
        start = asyncio.get_event_loop().time()
        for _ in range(3):
            await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 0.4

    @pytest.mark.asyncio
    async def test_fractional_rps(self):
        limiter = RateLimiter(0.5)
        start = asyncio.get_event_loop().time()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 1.5

    def test_stores_requests_per_second(self):
        limiter = RateLimiter(42)
        assert limiter.requests_per_second == 42

    @pytest.mark.asyncio
    async def test_no_burst_even_spacing(self):
        """Verify requests are evenly spaced (no burst).

        With rps=5, each request should be spaced ~200ms apart.
        If burst were allowed, all 5 would fire instantly then wait 1s.
        """
        limiter = RateLimiter(5)
        timestamps = []
        for _ in range(4):
            await limiter.acquire()
            timestamps.append(asyncio.get_event_loop().time())
        gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
        expected_gap = 1.0 / 5
        for gap in gaps:
            assert gap >= expected_gap - 0.1, (
                f"Gap {gap:.3f}s too short (expected ~{expected_gap:.3f}s)"
            )

    @pytest.mark.asyncio
    async def test_concurrent_acquires_are_serialized(self):
        """Multiple concurrent acquire() calls should still be rate-limited."""
        limiter = RateLimiter(5)
        timestamps = []

        async def worker():
            await limiter.acquire()
            timestamps.append(asyncio.get_event_loop().time())

        await asyncio.gather(*[worker() for _ in range(4)])
        total_elapsed = timestamps[-1] - timestamps[0]
        assert total_elapsed >= 0.4, (
            f"Elapsed {total_elapsed:.3f}s — concurrent requests were not serialized"
        )

    @pytest.mark.asyncio
    async def test_independent_limiters_do_not_interfere(self):
        """Two separate RateLimiter instances should not affect each other."""
        RateLimiter(1)
        fast = RateLimiter(100)
        start = asyncio.get_event_loop().time()
        for _ in range(5):
            await fast.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 0.5

    @pytest.mark.asyncio
    async def test_very_low_fractional_rps(self):
        """Verify rps=0.33 (~1 request every 3 seconds) works."""
        limiter = RateLimiter(0.33)
        start = asyncio.get_event_loop().time()
        await limiter.acquire()
        await limiter.acquire()
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed >= 2.5


class TestBaseScraperHelpers:
    """Test BaseScraper static/class methods and utilities without full init."""

    def test_detect_extension_pdf(self):
        from legisbr.core.convert import detect_extension

        assert detect_extension("application/pdf") == ".pdf"

    def test_detect_extension_html(self):
        from legisbr.core.convert import detect_extension

        assert detect_extension("text/html") == ".html"

    def test_detect_extension_from_filename(self):
        from legisbr.core.convert import detect_extension

        assert detect_extension("", "document.docx") == ".docx"

    def test_detect_extension_unknown(self):
        from legisbr.core.convert import detect_extension

        assert detect_extension("application/octet-stream") == ".bin"

    def test_valid_markdown_empty(self):
        from legisbr.core.convert import valid_markdown

        valid, reason = valid_markdown(None)
        assert not valid
        assert "None or empty" in reason

    def test_valid_markdown_too_short(self):
        from legisbr.core.convert import valid_markdown

        valid, reason = valid_markdown("Short text", min_length=50)
        assert not valid
        assert "too short" in reason

    def test_valid_markdown_server_error(self):
        from legisbr.core.convert import valid_markdown

        text = "The requested URL was not found on this server. " * 5
        valid, reason = valid_markdown(text)
        assert not valid
        assert "server error" in reason

    def test_valid_markdown_dotnet_error(self):
        from legisbr.core.convert import valid_markdown

        text = "Object reference not set to an instance of an object\nNossa equipe resolverá o problema, você pode tentar mais tarde."
        valid, reason = valid_markdown(text)
        assert not valid
        assert "server error" in reason

    def test_valid_markdown_ok(self):
        from legisbr.core.convert import valid_markdown

        text = "Art. 1º Esta lei dispõe sobre a organização básica do Estado. " * 3
        valid, reason = valid_markdown(text)
        assert valid
        assert reason == ""

    def test_wrap_html(self):
        from legisbr.core.convert import wrap_html

        assert wrap_html("<p>Hello</p>") == "<html><body><p>Hello</p></body></html>"

    def test_clean_markdown_strips_links(self):
        from legisbr.core.convert import clean_markdown

        result = clean_markdown("[link text](http://example.com)")
        assert result == "link text"
        assert "http" not in result

    def test_clean_markdown_custom_replacements(self):
        from legisbr.core.convert import clean_markdown

        result = clean_markdown("HEADER text content", replace=[("HEADER", "")])
        assert "HEADER" not in result
        assert "text content" in result

    def test_clean_markdown_normalizes_nbsp_and_strips_soft_hyphen(self):
        from legisbr.core.convert import clean_markdown

        result = clean_markdown("Adminis\u00adtrativo\u00a0Art.\u00a01º")
        assert result == "Administrativo Art. 1º"
        assert "\u00a0" not in result
        assert "\u00ad" not in result


class TestHtmlConversionFixes:
    @pytest.mark.asyncio
    async def test_word_soft_newlines_collapse_while_br_survives(self):
        from unittest.mock import AsyncMock

        from legisbr.core.convert import html_to_markdown

        html = (
            "<p><b>DECRETO\r\nNº 1.323</b> de 21 de\ndezembro de 2012</p>"
            "<p>I\r\n- o PAI originário;</p>"
            "<p><span>Art. 1º Primeiro;</span><br><span>Art. 2º Segundo.</span></p>"
        )
        result = await html_to_markdown(html, "https://x.org", AsyncMock())
        assert "**DECRETO Nº 1.323** de 21 de dezembro de 2012" in result
        assert "I - o PAI originário;" in result
        assert "\n- o PAI" not in result
        assert "Primeiro;" in result and "\nArt. 2º Segundo." in result

    @pytest.mark.asyncio
    async def test_css_line_through_becomes_strikethrough_even_with_strip_styles(self):
        from unittest.mock import AsyncMock

        from bs4 import BeautifulSoup

        from legisbr.core.convert import clean_norm_soup, html_to_markdown

        raw = (
            '<p><strong><span style="text-decoration:line-through;">Art. 4º</span></strong>'
            '<span style="text-decoration: line-through;"> Cláusula revogada.</span></p>'
            '<p style="text-decoration: line-through">Parágrafo único revogado.</p>'
        )
        soup = clean_norm_soup(BeautifulSoup(raw, "lxml"), strip_styles=True)
        result = await html_to_markdown(str(soup), "https://x.org", AsyncMock())
        assert "~~Art. 4º~~" in result
        assert "~~Cláusula revogada.~~" in result
        assert "~~Parágrafo único revogado.~~" in result

    @pytest.mark.asyncio
    async def test_nested_strikethrough_collapses_and_never_emits_quad_tildes(self):
        from unittest.mock import AsyncMock

        from legisbr.core.convert import html_to_markdown

        html = (
            "<p><s><s>Art. 1º Revogado duplamente.</s></s></p>"
            "<p><del><del>Art. 2º Também revogado.</del></del></p>"
        )
        result = await html_to_markdown(html, "https://x.org", AsyncMock())
        assert "~~~~" not in result
        assert "~~Art. 1º Revogado duplamente.~~" in result
        assert "~~Art. 2º Também revogado.~~" in result

    @pytest.mark.asyncio
    async def test_adjacent_strikethrough_tags_merge_and_never_emit_quad_tildes(self):
        from unittest.mock import AsyncMock

        from legisbr.core.convert import html_to_markdown

        html = (
            "<p><s>Art. 1º </s><s>Revogado pela Lei 10.</s></p>"
            "<p><del>Inciso I </del><strike>também revogado.</strike></p>"
            "<p><span><s>Comando-</s></span><s>Geral revogado.</s></p>"
            '<p><s><a href="/lei">Lei 467/0</a></s><s>8, revogada.</s></p>'
            "<p><s><span> </span></s></p>"
        )
        result = await html_to_markdown(html, "https://x.org", AsyncMock())
        assert "~~~~" not in result
        assert "~~Art. 1º Revogado pela Lei 10.~~" in result
        assert "~~Inciso I também revogado.~~" in result
        assert "~~Comando-Geral revogado.~~" in result
        assert "~~Lei 467/08, revogada.~~" in result

    @pytest.mark.asyncio
    async def test_ordinal_only_strikethrough_is_unwrapped(self):
        from unittest.mock import AsyncMock

        from legisbr.core.convert import html_to_markdown

        html = (
            "<p>LEI N<s>º</s> 15.944, Art. 1<s><font>º</font></s>, "
            "4<s>ª</s> Secretária, § 2<s>°</s> e <s>cláusula revogada</s>.</p>"
        )
        result = await html_to_markdown(html, "https://x.org", AsyncMock())
        assert "~~º~~" not in result
        assert "~~ª~~" not in result
        assert "~~°~~" not in result
        assert "LEI Nº 15.944, Art. 1º, 4ª Secretária, § 2°" in result
        assert "~~cláusula revogada~~" in result

    def test_remove_summary_element_handles_inline_punctuation_and_art_in_ementa(self):
        from bs4 import BeautifulSoup

        from legisbr.core.convert import remove_summary_element

        # Inline tag boundary creates space before '.' in get_text(" ").
        html1 = (
            '<div><p>Altera a <a href="/lei">Lei nº 06, de 28 de abril de 1997</a>.</p>'
            "<p>Art. 1º Conteúdo da lei.</p></div>"
        )
        soup1 = BeautifulSoup(html1, "lxml")
        remove_summary_element(soup1, "Altera a Lei nº 06, de 28 de abril de 1997.")
        assert "1997" not in soup1.get_text()
        assert "Art. 1º Conteúdo da lei." in soup1.get_text()

        # Dedicated ementa referencing "art." IS stripped, while actual Art. 1º survives.
        html2 = (
            "<div><p>ACRESCENTA O §5º AO ART. 76 DA CONSTITUIÇÃO ESTADUAL.</p>"
            "<p>Art. 1º Fica acrescido o §5º ao art. 76.</p></div>"
        )
        soup2 = BeautifulSoup(html2, "lxml")
        remove_summary_element(
            soup2, "ACRESCENTA O §5º AO ART. 76 DA CONSTITUIÇÃO ESTADUAL."
        )
        assert "ACRESCENTA" not in soup2.get_text()
        assert "Art. 1º Fica acrescido" in soup2.get_text()

        # Art. 1º mirroring summary must NOT be deleted.
        html3 = "<div><p>Art. 1º Denomina José Facundo Filho a rodovia estadual CE 166.</p></div>"
        soup3 = BeautifulSoup(html3, "lxml")
        remove_summary_element(
            soup3, "Denomina José Facundo Filho a rodovia estadual CE 166."
        )
        assert "Art. 1º Denomina" in soup3.get_text()

        # Operative clauses without "art." (Artigo 1º, § 1º, Parágrafo único, I -) must NOT be deleted!
        for clause in (
            "Artigo 1º Denomina José Facundo Filho a rodovia estadual.",
            "§ 1º Denomina José Facundo Filho a rodovia estadual.",
            "Parágrafo único. Denomina José Facundo Filho a rodovia estadual.",
            "I - Denomina José Facundo Filho a rodovia estadual.",
        ):
            soup_op = BeautifulSoup(f"<div><p>{clause}</p></div>", "lxml")
            remove_summary_element(
                soup_op, "Denomina José Facundo Filho a rodovia estadual."
            )
            assert clause in soup_op.get_text(), f"Deleted operative clause: {clause}"

        # Large container containing the summary + non-article body text must NOT be deleted.
        big_html = (
            "<div><p>Resumo curto da norma estadual.</p>"
            "<p>" + ("Disposição detalhada sem abreviação. " * 15) + "</p></div>"
        )
        soup_big = BeautifulSoup(big_html, "lxml")
        remove_summary_element(soup_big, "Resumo curto da norma estadual.")
        assert "Resumo curto" not in soup_big.get_text()
        assert "Disposição detalhada" in soup_big.get_text()

    @pytest.mark.asyncio
    async def test_pre_blocks_and_text_plain_preserve_newlines(self):
        from unittest.mock import AsyncMock

        from legisbr.core.convert import bytes_to_markdown, html_to_markdown

        html = (
            "<p>Soft\nwrapped prose.</p>"
            "<pre>COL_A   COL_B\n100     200\n300     400</pre>"
        )
        md = await html_to_markdown(html, "https://x.org", AsyncMock())
        assert "Soft wrapped prose." in md
        assert "COL_A   COL_B\n100     200\n300     400" in md

        plain_bytes = b"Art. 1\xba Primeira linha.\r\nArt. 2\xba Segunda linha.\nArt. 3\xba Terceira linha."
        plain_md = await bytes_to_markdown(
            plain_bytes,
            "text/plain; charset=windows-1252",
            "https://x.org/norma.txt",
            None,  # type: ignore[arg-type]
        )
        assert (
            plain_md
            == "Art. 1º Primeira linha.\nArt. 2º Segunda linha.\nArt. 3º Terceira linha."
        )

        # Plain text starting with "BM" or "RIFF....WAVE" must not be misdetected as images.
        bm_md = await bytes_to_markdown(
            b"BM25 texto normativo\nSegunda linha.",
            "text/plain; charset=utf-8",
            "https://x.org/bm.txt",
            None,  # type: ignore[arg-type]
        )
        assert bm_md == "BM25 texto normativo\nSegunda linha."

    @pytest.mark.asyncio
    async def test_mixed_div_and_table_list_strikethrough_normalization(self):
        from unittest.mock import AsyncMock

        from bs4 import BeautifulSoup

        from legisbr.core.convert import clean_norm_soup, html_to_markdown

        raw = (
            '<div style="text-decoration:line-through">Direct text<p>Inner para</p></div>'
            '<table style="text-decoration:line-through">'
            "<thead><tr><th>Col A</th><th>Col B</th></tr></thead>"
            "<tbody><tr><td>Val 1</td><td>Val 2</td></tr></tbody>"
            "</table>"
            '<ul style="text-decoration:line-through"><li>Item 1</li><li>Item 2</li></ul>'
        )
        soup = clean_norm_soup(BeautifulSoup(raw, "lxml"), strip_styles=True)
        # Never wrap <table> or <ul> in <s>.
        assert (
            soup.find("table") is not None
            and soup.find("table").find_parent("s") is None
        )
        assert soup.find("ul") is not None and soup.find("ul").find_parent("s") is None
        md = await html_to_markdown(str(soup), "https://x.org", AsyncMock())
        assert "~~Direct text~~" in md
        assert "~~Inner para~~" in md
        assert "| ~~Col A~~ | ~~Col B~~ |" in md
        assert "| ~~Val 1~~ | ~~Val 2~~ |" in md
        assert "~~Item 1~~" in md and "~~Item 2~~" in md
