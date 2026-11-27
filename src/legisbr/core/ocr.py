"""Shared PydanticAI OCR with bounded rendering and all-or-nothing batches."""

from __future__ import annotations

import asyncio
import io
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from legisbr.core.convert import clean_md_tag
from legisbr.core.images import _detect_image_mime
from legisbr.core.pdf import pdf_page_count, render_pdf_batch, slice_pdf_pages

if TYPE_CHECKING:
    from pydantic_ai import BinaryContent
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.usage import RunUsage

__all__ = [
    "DEFAULT_PROMPT",
    "OCR",
    "OCRError",
    "OCROutput",
    "clean_md_tag",
    "usage_scope",
]

usage_scope: ContextVar[str] = ContextVar("ocr_usage_scope", default="global")

_INSTALL_HINT = (
    'Install OCR support with: uv add "legisbr[ocr]" (or: pip install "legisbr[ocr]")'
)


class OCRError(Exception):
    pass


class OCROutput(BaseModel):
    text_markdown: str


DEFAULT_PROMPT = """Você é um especialista de extração e formatação de textos jurídicos. O documento fornecido é uma norma jurídica. Extraia todo o conteúdo principal e formate-o em Markdown, seguindo rigorosamente estas regras:

*   **Fidelidade Absoluta (CRÍTICO):** Transcreva o texto exata e literalmente como aparece no documento. Não altere nenhuma palavra, não corrija gramática e não modifique a pontuação. Preservar a exatidão legal é essencial. Não introduza nenhuma palavra ou frase que não esteja presente no documento original.
*   **Estrutura Legal:** Respeite rigorosamente a numeração e a hierarquia legislativa: títulos, capítulos, seções, artigos (Art.), parágrafos (§), incisos (algarismos romanos: I, II, III) e alíneas (letras: a, b, c).
*   **Formatação Markdown:**
    * Use títulos Markdown (`##` ou `###`) para títulos, capítulos e seções.
    * Aplique **negrito** ou *itálico* exatamente onde o texto original estiver em destaque.
    * Caso haja tabelas, preserve a formatação tabular usando a sintaxe de tabelas do Markdown.
    * Se houver uma *ementa* (o bloco de texto que resume a norma, geralmente recuado à direita no topo), formate-a como citação (usando `>` antes do bloco).
*   **Continuidade:** O texto pode ser continuação de uma página anterior ou terminar de forma abrupta. Extraia desde a primeira palavra válida até a última, mesmo que comece ou termine no meio de uma frase.
*   **Limpeza e Exclusões (ATENÇÃO):** Ignore cabeçalhos (headers), rodapés (footers), números de página, datas de impressão ou marcas d'água. **Exclua obrigatoriamente qualquer nota editorial ou aviso legal que inicie com "Este texto não substitui..." ou "Esse texto não substitui..." ou outras notas e observações similares, independentemente de onde apareçam na página.**

Nota: o documento recebido pode estar em branco. Nesse caso, retorne text_markdown como uma string vazia ("").

Retorne o conteúdo extraído no campo text_markdown. Não inclua a tag ```markdown, saudações, introduções ou explicações adicionais nesse campo."""


class OCR:
    def __init__(
        self,
        models: str,
        prompt: str = DEFAULT_PROMPT,
        batch_size: int = 5,
        concurrency: int = 8,
        pdf_input: bool = False,
        dpi: int = 150,
        model_settings: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            from pydantic_ai import Agent, BinaryContent
            from pydantic_ai.exceptions import ModelHTTPError
            from pydantic_ai.models.fallback import FallbackModel
            from pydantic_ai.usage import RunUsage
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc

        names = [name.strip() for name in models.split(",") if name.strip()]
        if not names or batch_size < 1 or concurrency < 1 or dpi < 1:
            raise ValueError("OCR needs models and positive batch_size/concurrency/dpi")
        self._binary_content = BinaryContent
        self._model_http_error = ModelHTTPError
        effective_settings: dict[str, Any] = {"thinking": False}
        if all(name.startswith("anthropic:") for name in names):
            effective_settings["anthropic_cache_instructions"] = True
        if model_settings is not None:
            effective_settings.update(model_settings)
        self.model_settings: dict[str, Any] = effective_settings
        model = FallbackModel(*names) if len(names) > 1 else names[0]
        self.agent = Agent(
            model,
            instructions=prompt,
            output_type=OCROutput,
            model_settings=cast("ModelSettings", effective_settings),
        )
        self.sem = asyncio.Semaphore(concurrency)
        # Bound local PDF batch rendering to 1 concurrent open document to prevent
        # multi-GB RSS spikes on large scanned PDFs while keeping LLM calls concurrent.
        self._render_sem = asyncio.Semaphore(1)
        self.batch_size = batch_size
        self.concurrency = concurrency
        self.pdf_input = pdf_input
        self.dpi = dpi
        self.usage: dict[str, RunUsage] = defaultdict(RunUsage)
        self._scoped_usage: dict[str, dict[str, RunUsage]] = defaultdict(
            lambda: defaultdict(RunUsage)
        )

    async def _run(self, parts: Sequence[BinaryContent]) -> str:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            retry=retry_if_exception_type((self._model_http_error, TimeoutError)),
            reraise=True,
        ):
            with attempt:
                result = await self.agent.run(["Transcreva o documento.", *parts])
                model = result.response.model_name or "unknown"
                usage = result.usage
                self.usage[model].incr(usage)
                self._scoped_usage[usage_scope.get()][model].incr(usage)
                return clean_md_tag(result.output.text_markdown)
        raise AssertionError("Retry loop did not execute")

    def usage_for(self, scope: str) -> dict[str, Any]:
        return {
            model: to_jsonable_python(usage)
            for model, usage in self._scoped_usage.get(scope, {}).items()
        }

    @staticmethod
    def _transcode_to_png(data: bytes) -> bytes:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    async def _prepare_image_part(self, raw: bytes) -> BinaryContent:
        mime = _detect_image_mime(raw)
        if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            return self._binary_content(data=raw, media_type=mime)
        if mime in {"image/bmp", "image/tiff"}:
            png_bytes = await asyncio.to_thread(self._transcode_to_png, raw)
            return self._binary_content(data=png_bytes, media_type="image/png")
        raise ValueError(
            f"Unsupported or unrecognized image format: {mime or 'unknown'}"
        )

    async def images(self, pngs: Sequence[bytes]) -> str:
        texts = []
        try:
            for offset in range(0, len(pngs), self.batch_size):
                batch_raw = pngs[offset : offset + self.batch_size]
                parts = [await self._prepare_image_part(raw) for raw in batch_raw]
                async with self.sem:
                    texts.append(await self._run(parts))
        except Exception as exc:
            raise OCRError("Image OCR failed") from exc
        return "\n\n".join(texts)

    def _render_batch(self, data: bytes, start: int, end: int) -> list[BinaryContent]:
        if self.pdf_input:
            chunk_bytes = slice_pdf_pages(data, list(range(start + 1, end + 1)))
            return [
                self._binary_content(data=chunk_bytes, media_type="application/pdf")
            ]
        return [
            self._binary_content(data=png, media_type="image/png")
            for png in render_pdf_batch(data, start, end, dpi=self.dpi)
        ]

    async def pdf(self, data: bytes) -> str:
        try:
            count = await asyncio.to_thread(pdf_page_count, data)

            async def batch(start: int) -> str:
                async with self.sem:
                    async with self._render_sem:
                        parts = await asyncio.to_thread(
                            self._render_batch,
                            data,
                            start,
                            min(start + self.batch_size, count),
                        )
                    try:
                        return await self._run(parts)
                    finally:
                        parts.clear()

            starts = range(0, count, self.batch_size)
            tasks: list[asyncio.Task[str]] = []
            async with asyncio.TaskGroup() as group:
                for start in starts:
                    tasks.append(group.create_task(batch(start)))
            return "\n\n".join(task.result() for task in tasks)
        except ExceptionGroup as exc:
            cause = exc.exceptions[0] if len(exc.exceptions) == 1 else exc
            raise OCRError(
                "PDF OCR failed; no partial document was returned"
            ) from cause
        except Exception as exc:
            raise OCRError("PDF OCR failed; no partial document was returned") from exc
