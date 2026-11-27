"""Private stdlib-only sync bridge (lazy event-loop thread + bounded DocumentStream)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import weakref
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Generic, Self, TypeVar

if TYPE_CHECKING:
    from legisbr.core.document import Document

_T = TypeVar("_T")


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    to_cancel = asyncio.all_tasks(loop)
    if not to_cancel:
        return
    for task in to_cancel:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*to_cancel, return_exceptions=True))


class _LoopThread:
    """Lazy daemon thread running a private ``asyncio`` event loop.

    Never creates a thread or loop at import or construction time; starts on the first
    ``submit()`` / ``start_future()`` call. Safe from both plain sync threads and threads
    that already have an active event loop (e.g. Jupyter / IPython / FastAPI).
    """

    def __init__(self, *, name: str = "legisbr-sync") -> None:
        self._name = name
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._loop is not None and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None and not self._closed:
            return loop
        with self._lock:
            if self._closed:
                raise RuntimeError("Client is closed")
            if self._loop is not None:
                return self._loop

            ready = threading.Event()
            holder: list[asyncio.AbstractEventLoop] = []

            def _run() -> None:
                ev_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(ev_loop)
                holder.append(ev_loop)
                ready.set()
                try:
                    ev_loop.run_forever()
                finally:
                    try:
                        _cancel_all_tasks(ev_loop)
                        ev_loop.run_until_complete(ev_loop.shutdown_asyncgens())
                        ev_loop.run_until_complete(ev_loop.shutdown_default_executor())
                    finally:
                        asyncio.set_event_loop(None)
                        ev_loop.close()

            thread = threading.Thread(target=_run, name=self._name, daemon=True)
            thread.start()
            ready.wait()
            self._loop = holder[0]
            self._thread = thread
            return self._loop

    def start_future(
        self, coro: Coroutine[Any, Any, _T]
    ) -> concurrent.futures.Future[_T]:
        """Schedule *coro* on the private loop and return its concurrent Future."""
        try:
            loop = self._ensure_started()
        except BaseException:
            coro.close()
            raise
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop)
        except BaseException:
            coro.close()
            raise

    def submit(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run *coro* on the private loop and return its result synchronously."""
        future = self.start_future(coro)
        try:
            return future.result()
        except KeyboardInterrupt:
            future.cancel()
            try:
                future.result(timeout=1.0)
            except BaseException:
                pass
            raise

    def close(self, timeout: float = 5.0) -> None:
        """Stop the private loop, cancel remaining tasks, and join the thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None

        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)


class _StreamDone:
    __slots__ = ()


_STREAM_DONE = _StreamDone()


class _StreamError:
    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _StreamState(Generic[_T]):
    """Finalizer-safe shared state for bridging an async stream to a sync iterator."""

    def __init__(
        self,
        *,
        maxsize: int,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self.maxsize = max(1, int(maxsize))
        self.cond = threading.Condition()
        self.buf: deque[Any] = deque()
        self.closed = False
        self.producer_waiting = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.space_event: asyncio.Event | None = None
        self.future: concurrent.futures.Future[None] | None = None
        self._on_close = on_close

    def start(
        self,
        loop_thread: _LoopThread,
        async_cm_factory: Callable[[], AbstractAsyncContextManager[AsyncIterator[_T]]],
        *,
        before_enter: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        async def _produce() -> None:
            ev_loop = asyncio.get_running_loop()
            space_event = asyncio.Event()
            space_event.set()
            with self.cond:
                if self.closed:
                    self.buf.append(_STREAM_DONE)
                    self.cond.notify_all()
                    return
                self.loop = ev_loop
                self.space_event = space_event

            try:
                if before_enter is not None:
                    await before_enter()
                async with async_cm_factory() as items:
                    async for item in items:
                        while True:
                            with self.cond:
                                if self.closed:
                                    return
                                if len(self.buf) < self.maxsize:
                                    self.buf.append(item)
                                    self.cond.notify()
                                    break
                                space_event.clear()
                                self.producer_waiting = True
                            await space_event.wait()
                with self.cond:
                    self.buf.append(_STREAM_DONE)
                    self.cond.notify_all()
            except asyncio.CancelledError:
                with self.cond:
                    self.buf.append(_STREAM_DONE)
                    self.cond.notify_all()
                raise
            except BaseException as exc:
                with self.cond:
                    self.buf.append(_StreamError(exc))
                    self.cond.notify_all()

        self.future = loop_thread.start_future(_produce())

    def close(self) -> None:
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.producer_waiting = False
            self.cond.notify_all()
            ev_loop = self.loop
            space_event = self.space_event

        if ev_loop is not None and space_event is not None:
            try:
                ev_loop.call_soon_threadsafe(space_event.set)
            except RuntimeError:
                pass

        future = self.future
        if future is not None:
            if not future.done():
                future.cancel()
            try:
                future.result(timeout=5.0)
            except BaseException:
                pass

        on_close = self._on_close
        if on_close is not None:
            self._on_close = None
            on_close()


class _BridgeStream(Iterator[_T]):
    """Bounded synchronous iterator over an async context-managed async iterator."""

    def __init__(
        self,
        loop_thread: _LoopThread,
        async_cm_factory: Callable[[], AbstractAsyncContextManager[AsyncIterator[_T]]],
        *,
        maxsize: int = 16,
        before_enter: Callable[[], Coroutine[Any, Any, None]] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._state: _StreamState[_T] = _StreamState(maxsize=maxsize, on_close=on_close)
        self._exhausted = False
        self._finalizer = weakref.finalize(self, self._state.close)
        try:
            self._state.start(loop_thread, async_cm_factory, before_enter=before_enter)
        except BaseException:
            self.close()
            raise

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> _T:
        if self._exhausted:
            raise StopIteration
        state = self._state
        try:
            with state.cond:
                while not state.buf:
                    if state.closed:
                        self._exhausted = True
                        raise StopIteration
                    state.cond.wait()
                item = state.buf.popleft()
                if state.producer_waiting:
                    state.producer_waiting = False
                    ev_loop = state.loop
                    space_event = state.space_event
                    if ev_loop is not None and space_event is not None:
                        try:
                            ev_loop.call_soon_threadsafe(space_event.set)
                        except RuntimeError:
                            pass
        except KeyboardInterrupt:
            self._exhausted = True
            self.close()
            raise

        if isinstance(item, _StreamDone):
            self._exhausted = True
            self.close()
            raise StopIteration
        if isinstance(item, _StreamError):
            self._exhausted = True
            self.close()
            raise item.exc
        return item

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Deterministically cancel the background producer and release resources."""
        self._exhausted = True
        self._finalizer()


class DocumentStream(_BridgeStream["Document"]):
    """Bounded synchronous stream of validated ``Document`` objects with clean teardown.

    Supports both direct iteration (``for doc in stream:``) and context-manager usage
    (``with client.scrape(...) as stream:``). Early ``break`` or ``close()`` cancels
    the background scraper cleanly and releases any owned temporary client.
    """

    if TYPE_CHECKING:

        def __next__(self) -> Document: ...
