#!/usr/bin/env python3
"""Helper per i test lenti (download modelli, A/B embedding, rete).

Uso:
    from test_slow import slow, RUN_SLOW

    class TestDownload(unittest.TestCase):
        @slow("scarica ~2GB di embedding")
        def test_scarica_modello(self): ...

Il test viene SKIPPATO salvo SYNC_VIDEO_RUN_SLOW=1 (job nightly CI).
In locale: SYNC_VIDEO_RUN_SLOW=1 python -m unittest test_...
"""

from __future__ import annotations

import functools
import os
import unittest
from collections.abc import Callable
from typing import TypeVar

RUN_SLOW = os.environ.get("SYNC_VIDEO_RUN_SLOW", "") == "1"

_F = TypeVar("_F", bound=Callable[..., object])


def slow(motivo: str = "") -> Callable[[_F], _F]:
    """Marca un test come lento: skip salvo SYNC_VIDEO_RUN_SLOW=1."""

    def decoratore(fn: _F) -> _F:
        messaggio = f"lento ({motivo})" if motivo else "lento (SYNC_VIDEO_RUN_SLOW=1 per eseguirlo)"

        @functools.wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> object:
            if not RUN_SLOW:
                raise unittest.SkipTest(messaggio)
            return fn(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decoratore
