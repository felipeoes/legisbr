"""Unit tests for the permissive ``pypdfium2`` + ``pypdf`` + ``pdfplumber`` + ``pillow`` PDF engine."""

from __future__ import annotations

import asyncio
import os
import struct
import time
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import AsyncMock

import pypdfium2 as pdfium
import pytest
from pdf_fixtures import (
    make_1bit_ccitt_masthead_pdf,
    make_1page_centered_watermark_pdf,
    make_blank_pdf,
    make_digital_pdf,
    make_numbered_multipage_pdf,
    make_partial_ocr_pdf,
    make_ruled_table_pdf,
    make_scanned_pdf,
    make_watermark_and_figure_pdf,
)

from legisbr.core import pdf as pdf_mod
from legisbr.core.pdf import (
    PdfRunner,
    _restore_legal_line_breaks,
    is_pdf_scanned,
    pdf_page_count,
    pdf_to_markdown,
    readable_text,
    render_pdf_batch,
    shutdown_pdf_pool,
    slice_pdf_pages,
)

ZERO_PAGE_PDF = (
    b"%PDF-1.0\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
    b"xref\n0 3\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000052 00000 n \n"
    b"trailer<</Size 3/Root 1 0 R>>\nstartxref\n99\n%%EOF"
)

# Real Paraíba OCR garbage sample vs clean appointee table (from existing calibration).
GARBAGE_LAYER = (
    "{.lIni r.1;tC:'V tl.l C;I· ... ' :; :.,. : 11., t j' ,\"' (: I. '.' .Jo\"l t "
    'LEI N" 9 .!l1 7 ,DF. 0 (; IJI~ .I UI.IIO IlI ~ 20 J 2 AUTO IUA: DEPUTADO '
    "Certiíico. p:: r('! os dr'!vh~ o ~ fint; , (:1 1;' I.\"~:' L E I f o i I "
)
NAME_LIST = (
    "NOME CARGO CÓDIGO AYRES ALVES DA LUZ ASSISTENTE DE GABINETE ASGRP "
    "CHRISTIANE VERENA LORENZUTTI SUPERVISOR GERAL DE GABINETE SGGRP "
)


def _crash_worker() -> None:
    os._exit(1)


class TestRestoreLegalLineBreaks:
    def test_splits_dense_merged_articles_paragraphs_and_incisos(self):
        dense = (
            "CAPÍTULO I DAS DEFINIÇÕES Art. 1º Esta lei dispõe sobre normas gerais. "
            "Parágrafo único. O órgão competente poderá: "
            "I - estabelecer critérios técnicos; "
            "II - fiscalizar atividades; e "
            "III - aplicar sanções. "
            "Art. 2º Aplica-se o disposto nesta lei. "
            "§ 1º O prazo será de trinta dias. "
            "§ 2º Fica revogado o regulamento anterior. "
            "CAPÍTULO II DAS DISPOSIÇÕES FINAIS Art. 3º Entra em vigor hoje."
        )
        restored = _restore_legal_line_breaks(dense)
        lines = [line.strip() for line in restored.splitlines() if line.strip()]
        assert any(line.startswith("Art. 1º") for line in lines)
        assert any(line.startswith("Parágrafo único.") for line in lines)
        assert any(line.startswith("I -") for line in lines)
        assert any(line.startswith("II -") for line in lines)
        assert any(line.startswith("III -") for line in lines)
        assert any(line.startswith("Art. 2º") for line in lines)
        assert any(line.startswith("§ 1º") for line in lines)
        assert any(line.startswith("§ 2º") for line in lines)
        assert any(line.startswith("CAPÍTULO II") for line in lines)
        assert any(line.startswith("Art. 3º") for line in lines)

    def test_splits_artigo_dot_ordinal_bold_italic_quotes_and_bold_incisos(self):
        dense = (
            "Decreta: Artigo 1º - Fica aberto crédito. "
            "Art. 1.º Ficam criados cargos. § 1.º O prazo é de 10 dias. "
            "deverá fixar. **Art. 104.** Compete ao Estado. "
            "*Zollernia ilicifolia.* Art. 2o Esta Resolução entra em vigor. "
            "passa a vigorar: “Art. 120 Os Auditores.” “§ 2º O prazo será de 30 dias.” "
            "Compete: I - planejar; **II** - executar; **III -** fiscalizar; e IV - avaliar."
        )
        restored = _restore_legal_line_breaks(dense)
        lines = [line.strip() for line in restored.splitlines() if line.strip()]
        for prefix in (
            "Artigo 1º",
            "Art. 1.º",
            "§ 1.º",
            "**Art. 104.**",
            "Art. 2o",
            "“Art. 120",
            "“§ 2º",
            "I -",
            "**II** -",
            "**III -**",
            "IV -",
        ):
            assert any(line.startswith(prefix) for line in lines), (
                f"Missing break before {prefix!r}"
            )

    def test_does_not_split_inline_legal_citations(self):
        text = (
            "Art. 4º Para os fins desta Lei, nos termos do art. 5º da Constituição "
            "e conforme o Art. 10 da Lei nº 6.938, observado o § 2º do art. 3º "
            "e o Parágrafo único do art. 8º, ficam mantidos os prazos."
        )
        restored = _restore_legal_line_breaks(text)
        assert restored == text


class TestIsPdfScanned:
    def test_digital_pdf_detected(self):
        pdf = make_digital_pdf("Art. 1º Ficam criadas as seguintes secretarias. " * 10)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is False
        assert confidence >= 0.7

    def test_scanned_full_page_image_detected(self):
        pdf = make_scanned_pdf(full_bleed=True)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence == 1.0

    def test_scanned_partial_coverage_image_detected(self):
        pdf = make_scanned_pdf(full_bleed=False)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence >= 0.8

    def test_1bit_ccitt_masthead_scan_detected(self):
        pdf = make_1bit_ccitt_masthead_pdf()
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence == 1.0

    def test_empty_blank_page_pdf(self):
        scanned, confidence = is_pdf_scanned(make_blank_pdf())
        assert scanned is False
        assert confidence == 1.0

    def test_zero_page_pdf(self):
        scanned, confidence = is_pdf_scanned(ZERO_PAGE_PDF)
        assert scanned is False
        assert confidence == 0.0

    def test_invalid_bytes_raises_value_error(self):
        with pytest.raises(ValueError, match="Could not open PDF"):
            is_pdf_scanned(b"not a pdf")

    @pytest.mark.parametrize(
        ("producer", "creator"),
        [
            ("Tesseract OCR 5.3", ""),
            ("ABBYY FineReader 15", ""),
            ("OmniPage Capture SDK", ""),
            ("", "Acrobat Paper Capture"),
            ("Readiris Pro", ""),
        ],
    )
    def test_ocr_metadata_keywords_detected(self, producer: str, creator: str):
        pdf = make_scanned_pdf(
            full_bleed=True, metadata={"/Producer": producer, "/Creator": creator}
        )
        assert pdf_mod._has_ocr_metadata_pypdf(pdf) is True
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence == 1.0


class TestReadabilityCalibration:
    def test_rejects_garbage_and_cmap_shift_while_accepting_clean_legal_text(self):
        assert readable_text(GARBAGE_LAYER * 4) is False
        assert readable_text(NAME_LIST * 4) is True
        # Clean Brazilian legal tokens (currency, compounds, UTM coords, codes, botanical *)
        legal = (
            "Decreto-Lei nº 200/1967 R$ 15.409,95 PA-275 QPM-1 S11D E=588.581,501m "
            "Zollernia ilicifolia* ESTADO DO ESPíRITO SANTO Art. 1º Fica instituído. "
        ) * 4
        assert readable_text(legal) is True
        # Caesar-shift / missing ToUnicode C0 control characters
        caesar = "'LiULR\x032¿FLDO\x03GR\x03'LVWULWR\x03)HGHUDO\x03" * 20
        assert readable_text(caesar) is False
        assert pdf_mod._page_has_cmap_gibberish(caesar) is True


class TestSliceAndRender:
    def test_slice_pdf_pages_preserves_df_semantics(self):
        pdf = make_numbered_multipage_pdf(5)
        assert pdf_page_count(pdf) == 5

        # 1-based subset in custom order, ignoring out-of-range page 99.
        sliced = slice_pdf_pages(pdf, [4, 2, 99, 1])
        assert pdf_page_count(sliced) == 3
        doc = pdfium.PdfDocument(sliced)
        try:
            assert "Art. 4º" in doc[0].get_textpage().get_text_bounded()
            assert "Art. 2º" in doc[1].get_textpage().get_text_bounded()
            assert "Art. 1º" in doc[2].get_textpage().get_text_bounded()
        finally:
            doc.close()

        # Empty or all out-of-range returns original bytes untouched.
        assert slice_pdf_pages(pdf, []) == pdf
        assert slice_pdf_pages(pdf, [0, -1, 10, 99]) == pdf

    def test_render_pdf_batch_returns_valid_png_at_150_dpi(self):
        pdf = make_scanned_pdf(full_bleed=False)
        pngs = render_pdf_batch(pdf, 0, 1, dpi=150)
        assert len(pngs) == 1
        assert pngs[0].startswith(b"\x89PNG\r\n\x1a\n")
        # Letter page (612x792 pt) at 150 DPI => ~1275x1650 px (not 72 DPI ~612x792).
        width, height = struct.unpack(">II", pngs[0][16:24])
        assert width >= 1200
        assert height >= 1600


class TestTablesWatermarksAndSelectiveOCR:
    async def test_ruled_table_extracted_in_place_as_gfm(self):
        pdf = make_ruled_table_pdf(
            [
                ["Ano", "Cilindrada", "CO (%)", "HC (ppm)"],
                ["2003 a 2009", "<250 cc", "6,0", "2.000"],
                ["2010 em diante", "<250 cc", "2,5", "600"],
            ]
        )
        md = await pdf_to_markdown(pdf, executor="thread")
        assert "| Ano | Cilindrada | CO (%) | HC (ppm) |" in md
        assert "| 2003 a 2009 | <250 cc | 6,0 | 2.000 |" in md
        assert "| 2010 em diante | <250 cc | 2,5 | 600 |" in md
        # In-place order: intro -> table -> outro
        assert md.find("Art. 1º") < md.find("| Ano |") < md.find("Art. 2º")

    async def test_multipage_watermark_filtered_while_unique_figure_placed_in_place(
        self,
    ):
        pdf = make_watermark_and_figure_pdf(pages=3, figure_page_0based=1)
        md = await pdf_to_markdown(pdf, executor="thread", embed_images=True)
        # 3 repeated watermarks dropped; 1 unique figure on page 2 kept!
        imgs = pdf_mod._MD_BASE64_IMAGE_RE.findall(md)
        assert len(imgs) == 1
        # Image is placed in-place between Art. 2º and Art. 99º (not dumped at EOF)
        assert (
            md.find("Art. 2º") < md.find("data:image/png;base64,") < md.find("Art. 99º")
        )

    async def test_single_page_centered_watermark_filtered(self):
        pdf = make_1page_centered_watermark_pdf()
        md = await pdf_to_markdown(pdf, executor="thread", embed_images=True)
        assert "Art. 1º" in md
        assert pdf_mod._MD_BASE64_IMAGE_RE.findall(md) == []

    async def test_1bit_ccitt_masthead_pdf_routes_to_ocr(self):
        pdf = make_1bit_ccitt_masthead_pdf()
        ocr = AsyncMock()
        ocr.pdf.return_value = "Art. 1º Lei transcrita do Diário Oficial do Piauí."
        md = await pdf_to_markdown(pdf, ocr=ocr, executor="thread")
        ocr.pdf.assert_awaited_once_with(pdf)
        assert "Piauí" in md

    async def test_selective_per_page_ocr_splices_only_image_only_pages(self):
        pdf = make_partial_ocr_pdf(image_only_pages=2)
        ocr = AsyncMock()
        ocr.pdf.return_value = (
            "Art. 2º Disposições finais transcritas das páginas 2 e 3."
        )
        md = await pdf_to_markdown(pdf, ocr=ocr, executor="thread")
        assert ocr.pdf.await_count == 1
        sliced_sent = ocr.pdf.await_args.args[0]
        # Only the 2 image-only pages (pp. 2-3) were sent to OCR, not page 1!
        assert pdf_page_count(sliced_sent) == 2
        assert "Art. 1º" in md and "Art. 2º" in md
        assert md.find("Art. 1º") < md.find("Art. 2º")


class TestPdfToMarkdownAndExecutors:
    async def test_digital_pdf_conversion_and_plain_fallback(self, monkeypatch):
        pdf = make_digital_pdf("Art. 1º " + "Texto legal para a administracao. " * 8)
        result = await pdf_to_markdown(pdf, executor="thread")
        assert "Art." in result

        monkeypatch.setattr(
            pdf_mod,
            "_digital_pdf",
            lambda *a, **kw: "![page](data:image/png;base64,AAAA)",
        )
        fallback = await pdf_to_markdown(pdf, executor="thread")
        assert "Art." in fallback

    async def test_scan_requires_ocr_and_delegates_to_ocr(self):
        pdf = make_scanned_pdf(full_bleed=True)
        with pytest.raises(ValueError, match="needs OCR"):
            await pdf_to_markdown(pdf, executor="thread")

        ocr = AsyncMock()
        ocr.pdf.return_value = "Art. 1º Texto transcrito via OCR."
        result = await pdf_to_markdown(pdf, ocr=ocr, executor="thread")
        ocr.pdf.assert_awaited_once_with(pdf)
        assert result == "Art. 1º Texto transcrito via OCR."

    @pytest.mark.parametrize(
        ("text", "ocr_called"),
        [(GARBAGE_LAYER * 4, True), (NAME_LIST * 4, False)],
        ids=["garbage", "name-list"],
    )
    async def test_unreadable_text_layer_ocr_gate(self, text: str, ocr_called: bool):
        assert readable_text(GARBAGE_LAYER * 4) is False
        assert readable_text(NAME_LIST * 4) is True

        ocr = AsyncMock()
        ocr.pdf.return_value = "Art. 1º Texto reconhecido pelo modelo. " * 5
        pdf = make_digital_pdf(text)
        result = await pdf_to_markdown(pdf, ocr=ocr, executor="thread")
        assert ocr.pdf.await_count == int(ocr_called)
        assert ("reconhecido" in result) is ocr_called

    async def test_garbage_text_layer_kept_without_ocr(self):
        pdf = make_digital_pdf(GARBAGE_LAYER * 4)
        result = await pdf_to_markdown(pdf, ocr=None, executor="thread")
        assert "DEPUTADO" in result

    async def test_auto_executor_falls_back_to_thread_on_permission_error(
        self, monkeypatch, caplog
    ):
        runner = PdfRunner(executor="auto", max_workers=2)

        def _deny_pool(*args, **kwargs):
            raise PermissionError("SemLock disabled on /dev/shm")

        monkeypatch.setattr(pdf_mod, "ProcessPoolExecutor", _deny_pool)
        pdf = make_digital_pdf("Art. 1º " + "Texto legal estadual. " * 10)
        with caplog.at_level("WARNING", logger="legisbr.core.pdf"):
            result = await runner.run(pdf_mod._pdf_text_layer, pdf, 0.1, True, True)
        assert result is not None and "Art. 1º" in result
        assert runner._process_unavailable is True
        assert "falling back to thread mode" in caplog.text

    async def test_process_executor_event_loop_stall_under_50ms_and_crash_recovery(
        self,
    ):
        pdf = make_digital_pdf(
            "Art. 1º Esta lei estabelece normas gerais de teste. " * 8,
            pages=120,
        )
        runner = PdfRunner(executor="process", max_workers=2)
        try:
            # Warm up spawn worker so startup cost isn't mixed with conversion tick test.
            warm = await runner.run(
                pdf_mod._pdf_text_layer,
                make_digital_pdf("Art. 1º " + "Texto de aquecimento. " * 6),
                0.1,
                False,
                False,
            )
            assert warm is not None

            gaps_ms: list[float] = []
            stop = False

            async def heartbeat() -> None:
                last = time.perf_counter()
                while True:
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    gaps_ms.append((now - last) * 1000.0)
                    last = now
                    if stop:
                        break

            hb = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.015)
            md = await runner.run(pdf_mod._pdf_text_layer, pdf, 0.1, False, True)
            stop = True
            await hb

            assert md is not None and "Art. 1º" in md
            assert max(gaps_ms) < 50.0, f"Event loop stalled for {max(gaps_ms):.1f} ms"

            # BrokenProcessPool recovery: crashed worker fails only its own call.
            with pytest.raises(BrokenProcessPool):
                await runner.run(_crash_worker)
            recovered = await runner.run(
                pdf_mod._pdf_text_layer,
                make_digital_pdf("Art. 1º " + "Recuperacao apos falha. " * 6),
                0.1,
                False,
                False,
            )
            assert recovered is not None and "Art. 1º" in recovered
        finally:
            runner.shutdown(wait=True)
            shutdown_pdf_pool(wait=True)
