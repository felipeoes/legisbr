from __future__ import annotations

import asyncio
import io

import pypdf
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from legisbr.core.ocr import OCR, OCRError, OCROutput


def pdf_bytes():
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.parametrize("pdf_input", [False, True])
@pytest.mark.parametrize("failed_batch", [0, 1, 2])
async def test_any_failed_batch_rejects_entire_pdf(pdf_input, failed_batch):
    ocr = OCR("test", batch_size=1, concurrency=1, pdf_input=pdf_input)
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        current = calls
        calls += 1
        if current == failed_batch:
            raise ValueError("batch failed")
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text_markdown": "page"})]
        )

    with ocr.agent.override(model=FunctionModel(respond)):
        with pytest.raises(OCRError):
            await ocr.pdf(pdf_bytes())


async def test_shared_concurrency_and_usage():
    ocr = OCR("test", batch_size=1, concurrency=2)
    active = peak = 0
    reached_two = asyncio.Event()

    async def respond(messages, info):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active >= 2:
            reached_two.set()
        else:
            try:
                await asyncio.wait_for(reached_two.wait(), timeout=2.0)
            except TimeoutError:
                pass
        await asyncio.sleep(0.01)
        active -= 1
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text_markdown": "page"})]
        )

    with ocr.agent.override(model=FunctionModel(respond)):
        texts = await asyncio.gather(*(ocr.pdf(pdf_bytes()) for _ in range(3)))
    assert texts == ["page\n\npage\n\npage"] * 3
    assert peak == 2
    assert sum(usage.requests for usage in ocr.usage.values()) == 9


async def test_successful_blank_batch_is_not_a_failure():
    ocr = OCR("test", batch_size=1, concurrency=1)
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"text_markdown": "" if calls == 2 else "page"},
                )
            ]
        )

    with ocr.agent.override(model=FunctionModel(respond)):
        assert await ocr.pdf(pdf_bytes()) == "page\n\n\n\npage"
    assert calls == 3


async def test_live_usage_cost_can_be_saved_to_sqlite() -> None:
    import json
    from decimal import Decimal

    from pydantic_ai.usage import RunUsage

    from legisbr.core.store import Store

    ocr = OCR("test")
    ocr._scoped_usage["live"]["claude-haiku-5-5"] = RunUsage(
        input_tokens=42, output_tokens=10, requests=1, cost=Decimal("0.000123")
    )
    async with Store(":memory:") as store:
        run_id = await store.start_run("OCR_TEST")
        await store.finish_run(run_id, 1, 0, ocr.usage_for("live"))
        saved = json.loads(
            store.connection.execute("SELECT llm_usage FROM runs").fetchone()[0]
        )
    assert saved["claude-haiku-5-5"]["input_tokens"] == 42
    assert saved["claude-haiku-5-5"]["cost"] == "0.000123"


@pytest.mark.parametrize(
    "payload", [{}, {"text_markdown": None}, {"text_markdown": 42}]
)
def test_ocr_output_requires_a_string(payload: dict) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OCROutput.model_validate(payload)


def test_ocr_missing_pydantic_ai_raises_install_hint(monkeypatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pydantic_ai", None)
    with pytest.raises(
        ImportError, match=r'uv add "legisbr\[ocr\]".*pip install "legisbr\[ocr\]"'
    ) as exc_info:
        OCR("openai:gpt-4.1-mini")
    assert "ocr-" not in str(exc_info.value)


async def test_pdf_batch_rendering_is_serialized_while_llm_calls_stay_concurrent(
    monkeypatch,
) -> None:
    import threading
    import time

    ocr = OCR("test", batch_size=1, concurrency=3)
    render_lock = threading.Lock()
    render_active = render_peak = 0
    llm_active = llm_peak = 0
    real_render = ocr._render_batch

    def tracked_render(data: bytes, start: int, end: int):
        nonlocal render_active, render_peak
        with render_lock:
            render_active += 1
            render_peak = max(render_peak, render_active)
        time.sleep(0.01)
        result = real_render(data, start, end)
        with render_lock:
            render_active -= 1
        return result

    monkeypatch.setattr(ocr, "_render_batch", tracked_render)

    async def respond(messages, info):
        nonlocal llm_active, llm_peak
        llm_active += 1
        llm_peak = max(llm_peak, llm_active)
        await asyncio.sleep(0.05)
        llm_active -= 1
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text_markdown": "page"})]
        )

    with ocr.agent.override(model=FunctionModel(respond)):
        text = await ocr.pdf(pdf_bytes())

    assert text == "page\n\npage\n\npage"
    assert render_peak == 1
    assert llm_peak >= 2


def test_ocr_dpi_validation_and_batch_rendering() -> None:
    import struct

    with pytest.raises(ValueError, match="positive"):
        OCR("test", dpi=0)
    ocr_low = OCR("test", dpi=100)
    parts = ocr_low._render_batch(pdf_bytes(), 0, 1)
    assert len(parts) == 1
    assert parts[0].data.startswith(b"\x89PNG\r\n\x1a\n")
    width, _ = struct.unpack(">II", parts[0].data[16:24])
    assert 800 <= width <= 900
