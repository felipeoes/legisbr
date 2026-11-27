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
    reached_two = asyncio.Event()
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
        try:
            if llm_active >= 2:
                reached_two.set()
            else:
                await asyncio.wait_for(reached_two.wait(), timeout=5.0)
        finally:
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


async def test_ocr_images_sniffs_mime_types_and_transcodes_bmp_and_tiff() -> None:
    from PIL import Image
    from pydantic_ai.messages import BinaryContent

    def encode(fmt: str) -> bytes:
        img = Image.new("RGB", (16, 16), (120, 80, 40))
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    samples = [
        ("PNG", "image/png", encode("PNG")),
        ("JPEG", "image/jpeg", encode("JPEG")),
        ("GIF", "image/gif", encode("GIF")),
        ("WEBP", "image/webp", encode("WEBP")),
        ("BMP", "image/png", encode("BMP")),
        ("TIFF", "image/png", encode("TIFF")),
    ]
    seen_parts: list[BinaryContent] = []

    async def respond(messages, info):
        for msg in messages:
            for part in getattr(msg, "parts", ()):
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, BinaryContent):
                            seen_parts.append(item)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text_markdown": "ok"})]
        )

    ocr = OCR("test", batch_size=10, concurrency=1)
    with ocr.agent.override(model=FunctionModel(respond)):
        result = await ocr.images([raw for _, _, raw in samples])

    assert result == "ok"
    assert [p.media_type for p in seen_parts] == [exp for _, exp, _ in samples]
    # BMP and TIFF were re-encoded to valid PNG bytes.
    assert seen_parts[4].data.startswith(b"\x89PNG\r\n\x1a\n")
    assert seen_parts[5].data.startswith(b"\x89PNG\r\n\x1a\n")

    # Non-image payload raises OCRError.
    with ocr.agent.override(model=FunctionModel(respond)):
        with pytest.raises(OCRError):
            await ocr.images([b"not an image payload"])


def _pdf_with_pages(n: int) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(n):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def test_ocr_default_model_settings_anthropic_caching_and_overrides(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    # 1. Generic/non-Anthropic models default to thinking=False with no Anthropic cache key.
    generic = OCR("test")
    assert generic.model_settings == {"thinking": False}
    assert generic.agent.model_settings == {"thinking": False}

    openai_only = OCR("openai:gpt-4.1-mini")
    assert openai_only.model_settings == {"thinking": False}
    assert "anthropic_cache_instructions" not in openai_only.model_settings

    # 2. Pure Anthropic model(s) auto-enable anthropic_cache_instructions=True.
    anthropic_single = OCR("anthropic:claude-haiku-4-5")
    assert anthropic_single.model_settings == {
        "thinking": False,
        "anthropic_cache_instructions": True,
    }
    anthropic_fallback = OCR("anthropic:claude-haiku-4-5,anthropic:claude-sonnet-4-6")
    assert anthropic_fallback.model_settings == {
        "thinking": False,
        "anthropic_cache_instructions": True,
    }

    # 3. Mixed fallback (Anthropic + OpenAI) must NOT enable Anthropic-only cache setting.
    mixed = OCR("anthropic:claude-haiku-4-5,openai:gpt-4.1-mini")
    assert mixed.model_settings == {"thinking": False}
    assert "anthropic_cache_instructions" not in mixed.model_settings

    # 4. Caller model_settings overrides take precedence over defaults.
    custom = OCR(
        "anthropic:claude-haiku-4-5",
        model_settings={
            "thinking": True,
            "anthropic_cache_instructions": False,
            "temperature": 0.0,
        },
    )
    assert custom.model_settings == {
        "thinking": True,
        "anthropic_cache_instructions": False,
        "temperature": 0.0,
    }

    # 5. Model settings are forwarded through Agent -> model.prepare_request() & agent.run().
    from pydantic_ai.models import ModelRequestParameters

    prepared_settings, prepared_params = anthropic_single.agent.model.prepare_request(
        anthropic_single.agent.model_settings, ModelRequestParameters()
    )
    assert prepared_params.thinking is False
    assert prepared_settings == {"anthropic_cache_instructions": True}

    seen_settings: list[dict | None] = []

    async def respond(messages, info):
        seen_settings.append(dict(info.model_settings) if info.model_settings else None)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"text_markdown": "ok"})]
        )

    with custom.agent.override(model=FunctionModel(respond)):
        assert await custom.pdf(_pdf_with_pages(1)) == "ok"
    assert seen_settings == [
        {"anthropic_cache_instructions": False, "temperature": 0.0}
    ]


async def test_ocr_pdf_sliding_window_does_not_block_on_slow_straggler(
    monkeypatch,
) -> None:
    ocr = OCR("test", batch_size=1, concurrency=2)
    monkeypatch.setattr(
        ocr,
        "_render_batch",
        lambda data, start, end: [
            ocr._binary_content(data=f"batch-{start}".encode(), media_type="image/png")
        ],
    )
    release_batch_0 = asyncio.Event()
    later_batches_done = asyncio.Event()
    completed: list[int] = []
    active = peak = 0

    async def respond(messages, info):
        nonlocal active, peak
        batch_idx = -1
        for msg in messages:
            for part in getattr(msg, "parts", ()):
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    for item in content:
                        if hasattr(item, "data") and item.data.startswith(b"batch-"):
                            batch_idx = int(item.data.decode().split("-")[1])
        active += 1
        peak = max(peak, active)
        try:
            if batch_idx == 0:
                await asyncio.wait_for(release_batch_0.wait(), timeout=2.0)
            else:
                await asyncio.sleep(0.005)
                completed.append(batch_idx)
                # Batches 1, 2, AND 3 must all finish while batch 0 is still blocked!
                if set(completed) == {1, 2, 3}:
                    later_batches_done.set()
                    release_batch_0.set()
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name, {"text_markdown": f"p{batch_idx}"}
                    )
                ]
            )
        finally:
            active -= 1

    with ocr.agent.override(model=FunctionModel(respond)):
        result = await asyncio.wait_for(ocr.pdf(_pdf_with_pages(4)), timeout=3.0)

    assert later_batches_done.is_set()
    assert peak == 2
    assert result == "p0\n\np1\n\np2\n\np3"


async def test_ocr_pdf_sliding_window_all_or_nothing_cancels_in_flight_batches(
    monkeypatch,
) -> None:
    ocr = OCR("test", batch_size=1, concurrency=2)
    monkeypatch.setattr(
        ocr,
        "_render_batch",
        lambda data, start, end: [
            ocr._binary_content(data=f"batch-{start}".encode(), media_type="image/png")
        ],
    )
    batch_0_started = asyncio.Event()
    batch_0_cancelled = asyncio.Event()
    started_batches: list[int] = []

    async def respond(messages, info):
        batch_idx = -1
        for msg in messages:
            for part in getattr(msg, "parts", ()):
                content = getattr(part, "content", None)
                if isinstance(content, list):
                    for item in content:
                        if hasattr(item, "data") and item.data.startswith(b"batch-"):
                            batch_idx = int(item.data.decode().split("-")[1])
        started_batches.append(batch_idx)
        if batch_idx == 0:
            batch_0_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                batch_0_cancelled.set()
                raise
        if batch_idx == 1:
            await asyncio.wait_for(batch_0_started.wait(), timeout=2.0)
            raise ValueError("batch 1 exploded")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name, {"text_markdown": f"p{batch_idx}"}
                )
            ]
        )

    before_tasks = set(asyncio.all_tasks())
    with ocr.agent.override(model=FunctionModel(respond)):
        with pytest.raises(OCRError) as exc_info:
            await asyncio.wait_for(ocr.pdf(_pdf_with_pages(5)), timeout=3.0)

    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "batch 1 exploded" in str(exc_info.value.__cause__)
    assert batch_0_cancelled.is_set()
    assert not (set(asyncio.all_tasks()) - before_tasks)
