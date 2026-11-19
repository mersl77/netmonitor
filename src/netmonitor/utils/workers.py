"""Malý pool pro blokující OS dotazy, které nesmějí zadržet konec procesu.

ThreadPoolExecutor registruje workery pro bezpodmínečný join při konci Pythonu,
i pokud je shutdown(wait=False). Zde jsou vlákna daemon, bez atexit registrace.
Rozběhnutou práci nelze násilně přerušit; volající musí chránit její prostředky.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from typing import Callable, TypeVar

T = TypeVar("T")


class DaemonPool:
    """Omezený počet workerů; submit přijímá funkci bez argumentů jako Controller."""

    def __init__(self, max_workers: int, thread_name_prefix: str):
        self._max_workers = max(1, max_workers)
        self._prefix = thread_name_prefix
        self._queue: queue.Queue[tuple[Future, Callable] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Pool je ukončen")
            self._queue.put((future, fn))
            if len(self._threads) < self._max_workers:
                worker = threading.Thread(target=self._work, daemon=True,
                                          name=f"{self._prefix}_{len(self._threads)}")
                self._threads.append(worker)
                worker.start()
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn = item
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = fn()
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
                        del result
            finally:
                # Nezadržovat callbacky a jejich Qt objekty v nečinném workeru.
                del item, future, fn

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False,
                 timeout: float | None = None) -> None:
        """Timeout omezuje celkový join, nikoli každý worker zvlášť."""
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        cancelled = []
        with self._lock:
            first_shutdown = not self._shutdown
            self._shutdown = True
            if cancel_futures:
                # Vyřazený pool už může mít shutdown(wait=False). Při stop()
                # zrušíme i jeho zbývající frontu a obnovíme ukončovací značky.
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        cancelled.append(item[0])
            if first_shutdown or cancel_futures:
                for _ in self._threads:
                    self._queue.put(None)
        # cancel() může spustit callback volající zpět do poolu – mimo zámek.
        for future in cancelled:
            future.cancel()
        if wait:
            for worker in self._threads:
                worker.join(None if deadline is None else max(0, deadline - time.monotonic()))
