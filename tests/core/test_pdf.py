"""Unit tests for the permissive ``pdf_oxide`` + ``pypdf`` PDF engine (zero ``fitz``)."""

from __future__ import annotations

import asyncio
import io
import os
import struct
import time
import zlib
from concurrent.futures.process import BrokenProcessPool
from unittest.mock import AsyncMock

import pdf_oxide
import pypdf
import pytest

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


def _minimal_png(width: int = 595, height: int = 842) -> bytes:
    """Build a valid solid-color PNG without external imaging libraries."""
    raw = b"".join(b"\x00" + b"\xff" * (width * 3) for _ in range(height))
    compressed = zlib.compress(raw)

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        body = ctype + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def _make_digital_pdf(
    text: str = "Art. 1º Este é um decreto digital com texto suficiente para validação.",
    *,
    pages: int = 1,
    metadata: dict[str, str] | None = None,
) -> bytes:
    """Create a digital PDF with selectable text using only ``pdf_oxide`` + ``pypdf``."""
    base_bytes = pdf_oxide.Pdf.from_text(text).to_bytes()
    if pages == 1 and not metadata:
        return base_bytes
    reader = pypdf.PdfReader(io.BytesIO(base_bytes))
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_page(reader.pages[0])
    if metadata:
        writer.add_metadata(metadata)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_numbered_multipage_pdf(num_pages: int) -> bytes:
    """Build a multi-page PDF where each page has distinct text (Page 1..N)."""
    writer = pypdf.PdfWriter()
    for idx in range(1, num_pages + 1):
        page_bytes = pdf_oxide.Pdf.from_text(
            f"Art. {idx}º Disposicao da pagina {idx} do diario oficial. " * 6
        ).to_bytes()
        reader = pypdf.PdfReader(io.BytesIO(page_bytes))
        writer.add_page(reader.pages[0])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_scanned_pdf(
    *,
    full_bleed: bool = True,
    metadata: dict[str, str] | None = None,
) -> bytes:
    """Create a scanned image-only PDF without ``fitz``."""
    png = _minimal_png(595, 842)
    raw = pdf_oxide.Pdf.from_image_bytes(png).to_bytes()
    if not full_bleed and not metadata:
        return raw
    reader = pypdf.PdfReader(io.BytesIO(raw))
    writer = pypdf.PdfWriter()
    page = writer.add_page(reader.pages[0])
    if full_bleed:
        # Tightly crop to the placed image rectangle so coverage == 1.0 (> 0.8).
        doc = pdf_oxide.PdfDocument.from_bytes(raw)
        imgs = doc.page_images(0)
        if imgs:
            img = imgs[0]
            x0, y0 = float(img["x"]), float(img["y"])
            x1, y1 = x0 + float(img["width"]), y0 + float(img["height"])
            page.mediabox.lower_left = (x0, y0)
            page.mediabox.upper_right = (x1, y1)
    if metadata:
        writer.add_metadata(metadata)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_blank_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


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
        pdf = _make_digital_pdf("Art. 1º Ficam criadas as seguintes secretarias. " * 10)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is False
        assert confidence >= 0.7

    def test_scanned_full_page_image_detected(self):
        pdf = _make_scanned_pdf(full_bleed=True)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence == 1.0

    def test_scanned_partial_coverage_image_detected(self):
        pdf = _make_scanned_pdf(full_bleed=False)
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence >= 0.8

    def test_empty_blank_page_pdf(self):
        scanned, confidence = is_pdf_scanned(_make_blank_pdf())
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
        pdf = _make_scanned_pdf(
            full_bleed=True, metadata={"/Producer": producer, "/Creator": creator}
        )
        assert pdf_mod._has_ocr_metadata_pypdf(pdf) is True
        scanned, confidence = is_pdf_scanned(pdf)
        assert scanned is True
        assert confidence == 1.0


class TestSliceAndRender:
    def test_slice_pdf_pages_preserves_df_semantics(self):
        pdf = _make_numbered_multipage_pdf(5)
        assert pdf_page_count(pdf) == 5

        # 1-based subset in custom order, ignoring out-of-range page 99.
        sliced = slice_pdf_pages(pdf, [4, 2, 99, 1])
        assert pdf_page_count(sliced) == 3
        doc = pdf_oxide.PdfDocument.from_bytes(sliced)
        assert "Art. 4º" in doc.extract_text(0)
        assert "Art. 2º" in doc.extract_text(1)
        assert "Art. 1º" in doc.extract_text(2)

        # Empty or all out-of-range returns original bytes untouched.
        assert slice_pdf_pages(pdf, []) == pdf
        assert slice_pdf_pages(pdf, [0, -1, 10, 99]) == pdf

    def test_render_pdf_batch_returns_valid_png_at_150_dpi(self):
        pdf = _make_scanned_pdf(full_bleed=False)
        pngs = render_pdf_batch(pdf, 0, 1, dpi=150)
        assert len(pngs) == 1
        assert pngs[0].startswith(b"\x89PNG\r\n\x1a\n")
        # Letter page (612x792 pt) at 150 DPI => ~1275x1650 px (not 72 DPI ~612x792).
        width, height = struct.unpack(">II", pngs[0][16:24])
        assert width >= 1200
        assert height >= 1600


class TestPdfToMarkdownAndExecutors:
    async def test_digital_pdf_conversion_and_plain_fallback(self, monkeypatch):
        pdf = _make_digital_pdf("Art. 1º " + "Texto legal para a administracao. " * 8)
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
        pdf = _make_scanned_pdf(full_bleed=True)
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
        pdf = _make_digital_pdf(text)
        result = await pdf_to_markdown(pdf, ocr=ocr, executor="thread")
        assert ocr.pdf.await_count == int(ocr_called)
        assert ("reconhecido" in result) is ocr_called

    async def test_garbage_text_layer_kept_without_ocr(self):
        pdf = _make_digital_pdf(GARBAGE_LAYER * 4)
        result = await pdf_to_markdown(pdf, ocr=None, executor="thread")
        assert "DEPUTADO" in result

    async def test_auto_executor_falls_back_to_thread_on_permission_error(
        self, monkeypatch, caplog
    ):
        runner = PdfRunner(executor="auto", max_workers=2)

        def _deny_pool(*args, **kwargs):
            raise PermissionError("SemLock disabled on /dev/shm")

        monkeypatch.setattr(pdf_mod, "ProcessPoolExecutor", _deny_pool)
        pdf = _make_digital_pdf("Art. 1º " + "Texto legal estadual. " * 10)
        with caplog.at_level("WARNING", logger="legisbr.core.pdf"):
            result = await runner.run(pdf_mod._pdf_text_layer, pdf, 0.1, True, True)
        assert result is not None and "Art. 1º" in result
        assert runner._process_unavailable is True
        assert "falling back to thread mode" in caplog.text

    async def test_process_executor_event_loop_stall_under_50ms_and_crash_recovery(
        self,
    ):
        pdf = _make_digital_pdf(
            "Art. 1º Esta lei estabelece normas gerais de teste. " * 8,
            pages=120,
        )
        runner = PdfRunner(executor="process", max_workers=2)
        try:
            # Warm up spawn worker so startup cost isn't mixed with conversion tick test.
            warm = await runner.run(
                pdf_mod._pdf_text_layer,
                _make_digital_pdf("Art. 1º " + "Texto de aquecimento. " * 6),
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
                _make_digital_pdf("Art. 1º " + "Recuperacao apos falha. " * 6),
                0.1,
                False,
                False,
            )
            assert recovered is not None and "Art. 1º" in recovered
        finally:
            runner.shutdown(wait=True)
            shutdown_pdf_pool(wait=True)
