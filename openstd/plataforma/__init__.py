"""Elige la implementación según el sistema operativo."""
from __future__ import annotations

from ..util import ES_LINUX, ES_WINDOWS
from .base import Plataforma

_actual: Plataforma | None = None


def actual() -> Plataforma:
    global _actual
    if _actual is None:
        if ES_WINDOWS:
            from .windows import Windows
            _actual = Windows()
        elif ES_LINUX:
            from .linux import Linux
            _actual = Linux()
        else:
            raise RuntimeError("Sistema operativo no soportado (solo Windows y Linux)")
    return _actual


def usar(p: Plataforma) -> None:
    """Para las pruebas: reemplaza la plataforma por una simulada."""
    global _actual
    _actual = p
