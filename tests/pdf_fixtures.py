"""Pure-Python permissive PDF test fixture builders (``pypdf`` + ``PIL``)."""

from __future__ import annotations

import io
import textwrap
import zlib

from PIL import Image
import pypdf
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)


def _escape_pdf_text(text: str) -> bytes:
    """Encode *text* to WinAnsi (cp1252) and escape PDF literal-string delimiters."""
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _make_helvetica_font() -> DictionaryObject:
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }
    )


def _add_text_page(
    writer: pypdf.PdfWriter,
    text: str,
    *,
    width: float = 595,
    height: float = 842,
    render_mode: int = 0,
    font_size: int = 10,
    start_x: float = 50,
    start_y: float = 780,
    wrap_width: int = 85,
) -> pypdf.PageObject:
    page = writer.add_blank_page(width=width, height=height)
    font_ref = writer._add_object(_make_helvetica_font())
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    lines: list[str] = []
    for para in text.splitlines() or [""]:
        wrapped = textwrap.wrap(para, width=wrap_width) if para.strip() else [""]
        lines.extend(wrapped)
    ops = [
        b"BT",
        f"/F1 {font_size} Tf".encode("ascii"),
        f"{render_mode} Tr".encode("ascii"),
        f"{start_x} {start_y} Td".encode("ascii"),
        f"{font_size + 4} TL".encode("ascii"),
    ]
    for idx, line in enumerate(lines[:52]):
        escaped = _escape_pdf_text(line)
        ops.append(
            (b"(" + escaped + b") Tj") if idx == 0 else (b"T* (" + escaped + b") Tj")
        )
    ops.append(b"ET")
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(ops))
    page[NameObject("/Contents")] = writer._add_object(stream)
    return page


def make_digital_pdf(
    text: str = "Art. 1º Este é um decreto digital com texto suficiente para validação.",
    *,
    pages: int = 1,
    metadata: dict[str, str] | None = None,
    render_mode: int = 0,
) -> bytes:
    """Create a digital PDF with selectable text using only ``pypdf``."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        _add_text_page(writer, text, render_mode=render_mode)
    if metadata:
        writer.add_metadata(metadata)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_numbered_multipage_pdf(num_pages: int) -> bytes:
    """Build a multi-page PDF where each page has distinct text (Art. 1º..Nº)."""
    writer = pypdf.PdfWriter()
    for idx in range(1, num_pages + 1):
        _add_text_page(
            writer,
            f"Art. {idx}º Disposição da página {idx} do diário oficial. " * 6,
        )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_blank_pdf() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_rgb_image_xobject(
    writer: pypdf.PdfWriter,
    px_w: int,
    px_h: int,
    color: tuple[int, int, int] = (200, 210, 220),
) -> pypdf.generic.IndirectObject:
    raw = bytes(color) * (px_w * px_h)
    compressed = zlib.compress(raw)
    img_stream = DecodedStreamObject()
    img_stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(px_w),
            NameObject("/Height"): NumberObject(px_h),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        }
    )
    img_stream._data = compressed
    return writer._add_object(img_stream)


def _make_1bit_image_xobject(
    writer: pypdf.PdfWriter, px_w: int, px_h: int
) -> pypdf.generic.IndirectObject:
    row_bytes = (px_w + 7) // 8
    raw = b"\xaa" * (row_bytes * px_h)
    compressed = zlib.compress(raw)
    img_stream = DecodedStreamObject()
    img_stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(px_w),
            NameObject("/Height"): NumberObject(px_h),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(1),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        }
    )
    img_stream._data = compressed
    return writer._add_object(img_stream)


def make_scanned_pdf(
    *,
    full_bleed: bool = True,
    metadata: dict[str, str] | None = None,
) -> bytes:
    """Create a scanned image-only PDF using ``PIL`` + ``pypdf``."""
    img = Image.new("RGB", (612, 792), (245, 245, 245))
    raw_buf = io.BytesIO()
    img.save(raw_buf, format="PDF", resolution=72.0)
    raw = raw_buf.getvalue()
    if full_bleed and not metadata:
        return raw

    writer = pypdf.PdfWriter()
    if full_bleed:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        writer.add_page(reader.pages[0])
    else:
        # Partial coverage (~35% of page) with zero text -> scanned=True, conf>=0.8.
        page = writer.add_blank_page(width=612, height=792)
        im_ref = _make_rgb_image_xobject(writer, 200, 260)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): im_ref})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"q 350 0 0 480 120 150 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
    if metadata:
        writer.add_metadata(metadata)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_1bit_ccitt_masthead_pdf(
    masthead_text: str = "Diário Oficial do Estado do Piauí - Teresina(PI) - Edição 123 - OF. 940",
    *,
    pages: int = 1,
) -> bytes:
    """Simulate a Piauí Diário Oficial PDF: small digital masthead + 2 un-OCR'd 1-bit scan columns (~65% area)."""
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        font_ref = writer._add_object(_make_helvetica_font())
        im_left = _make_1bit_image_xobject(writer, 240, 620)
        im_right = _make_1bit_image_xobject(writer, 240, 620)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/ImL"): im_left, NameObject("/ImR"): im_right}
                ),
            }
        )
        ops = [
            b"BT /F1 10 Tf 50 805 Td (" + _escape_pdf_text(masthead_text) + b") Tj ET",
            b"q 245 0 0 660 40 60 cm /ImL Do Q",
            b"q 245 0 0 660 310 60 cm /ImR Do Q",
        ]
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(ops))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_partial_ocr_pdf(
    page1_text: str = "Art. 1º Emenda Constitucional que altera dispositivos da Constituição Estadual. "
    * 4,
    *,
    image_only_pages: int = 2,
) -> bytes:
    """Build a multi-page PDF with text on page 1 and full-page image-only pages 2..N (like RR id=1/19)."""
    writer = pypdf.PdfWriter()
    _add_text_page(writer, page1_text)
    for _ in range(image_only_pages):
        page = writer.add_blank_page(width=595, height=842)
        im_ref = _make_rgb_image_xobject(writer, 200, 280)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): im_ref})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"q 595 0 0 842 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_ruled_table_pdf(
    rows: list[list[str]],
    *,
    intro_text: str = "Art. 1º Ficam fixados os limites constantes da tabela abaixo:",
    outro_text: str = "Art. 2º Esta norma entra em vigor na data de sua publicação.",
) -> bytes:
    """Build a single-page digital PDF with intro prose, a ruled vector table, and outro prose."""
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font_ref = writer._add_object(_make_helvetica_font())
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )

    ncols = len(rows[0])
    nrows = len(rows)
    x_left = 50.0
    col_w = 480.0 / ncols
    y_top = 700.0
    row_h = 24.0
    y_bot = y_top - nrows * row_h

    ops: list[bytes] = [
        b"BT /F1 11 Tf 50 760 Td (" + _escape_pdf_text(intro_text) + b") Tj ET",
        b"0.8 w",
    ]
    # Draw horizontal rules
    for r in range(nrows + 1):
        y = y_top - r * row_h
        ops.append(f"{x_left} {y} m {x_left + ncols * col_w} {y} l S".encode("ascii"))
    # Draw vertical rules
    for c in range(ncols + 1):
        x = x_left + c * col_w
        ops.append(f"{x} {y_top} m {x} {y_bot} l S".encode("ascii"))
    # Draw cell text
    for r, row in enumerate(rows):
        cy = y_top - r * row_h - 16.0
        for c, cell in enumerate(row):
            cx = x_left + c * col_w + 6.0
            ops.append(
                b"BT /F1 10 Tf "
                + f"{cx} {cy} Td (".encode("ascii")
                + _escape_pdf_text(cell)
                + b") Tj ET"
            )
    ops.append(
        b"BT /F1 11 Tf "
        + f"50 {y_bot - 40} Td (".encode("ascii")
        + _escape_pdf_text(outro_text)
        + b") Tj ET"
    )
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(ops))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_watermark_and_figure_pdf(
    *,
    pages: int = 3,
    figure_page_0based: int = 1,
) -> bytes:
    """Multi-page PDF with an identical repeated watermark (ratio ~0.19) + 1 unique substantive figure."""
    writer = pypdf.PdfWriter()
    wm_ref = _make_rgb_image_xobject(writer, 120, 160, color=(235, 235, 235))
    fig_ref = _make_rgb_image_xobject(writer, 260, 180, color=(40, 110, 180))
    for i in range(pages):
        page = writer.add_blank_page(width=595, height=842)
        font_ref = writer._add_object(_make_helvetica_font())
        xobjs = {NameObject("/ImWM"): wm_ref}
        ops = [
            b"q 255 0 0 366 170 230 cm /ImWM Do Q",
            b"BT /F1 11 Tf 50 760 Td ("
            + _escape_pdf_text(
                f"Art. {i + 1}º Disposição normativa da página {i + 1} sobre administração pública estadual."
            )
            + b") Tj ET",
        ]
        if i == figure_page_0based:
            xobjs[NameObject("/ImFig")] = fig_ref
            # Placed cleanly between Art. N (y=760) and closing text (y=120)
            ops.append(b"q 360 0 0 220 110 380 cm /ImFig Do Q")
            ops.append(
                b"BT /F1 11 Tf 50 120 Td ("
                + _escape_pdf_text(
                    "Art. 99º Disposição final posicionada após o diagrama técnico anexado."
                )
                + b") Tj ET"
            )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
                NameObject("/XObject"): DictionaryObject(xobjs),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"\n".join(ops))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_1page_centered_watermark_pdf() -> bytes:
    """Single-page digital PDF with a centered background watermark behind 20+ lines of visible text."""
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font_ref = writer._add_object(_make_helvetica_font())
    wm_ref = _make_rgb_image_xobject(writer, 120, 160, color=(235, 235, 235))
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
            NameObject("/XObject"): DictionaryObject({NameObject("/ImWM"): wm_ref}),
        }
    )
    ops = [b"q 260 0 0 380 165 230 cm /ImWM Do Q"]
    for idx in range(25):
        y = 650 - idx * 18
        ops.append(
            b"BT /F1 10 Tf "
            + f"80 {y} Td (".encode("ascii")
            + _escape_pdf_text(
                f"Art. {idx + 1}º Fica estabelecida a norma estadual de gestão orçamentária."
            )
            + b") Tj ET"
        )
    stream = DecodedStreamObject()
    stream.set_data(b"\n".join(ops))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
