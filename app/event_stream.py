"""In-process SSE broadcaster for the live event tail.

Per-subscriber queues so every connected client gets every event.
Publishers may run on APScheduler threads or webhook BackgroundTasks —
use call_soon_threadsafe against each subscriber's event loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger("para_scope.event_stream")

_QUEUE_MAX = 256


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop


_subscribers: list[_Subscriber] = []
_lock = threading.Lock()


def _remove_subscriber(sub: _Subscriber) -> None:
    with _lock:
        try:
            _subscribers.remove(sub)
        except ValueError:
            pass


def publish(event_id: int) -> None:
    """Notify all live subscribers that ``event_id`` is ready to render."""
    with _lock:
        subs = list(_subscribers)
    for sub in subs:
        try:
            sub.loop.call_soon_threadsafe(_enqueue, sub, event_id)
        except RuntimeError:
            _remove_subscriber(sub)


def _enqueue(sub: _Subscriber, event_id: int) -> None:
    try:
        sub.queue.put_nowait(event_id)
    except asyncio.QueueFull:
        # Drop oldest then retry once so a slow client doesn't block others.
        try:
            sub.queue.get_nowait()
            sub.queue.put_nowait(event_id)
        except Exception:
            _remove_subscriber(sub)
    except Exception:
        _remove_subscriber(sub)


async def subscribe() -> asyncio.Queue:
    """Register a new subscriber queue on the current event loop."""
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    sub = _Subscriber(queue=q, loop=asyncio.get_running_loop())
    with _lock:
        _subscribers.append(sub)
    return q


async def unsubscribe(queue: asyncio.Queue) -> None:
    with _lock:
        for sub in list(_subscribers):
            if sub.queue is queue:
                _subscribers.remove(sub)
                break
