"""Resultado de cada prueba y el canal de progreso que comparten las pruebas y la interfaz."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

PASA = "PASA"
FALLA = "FALLA"
NO_EVALUADO = "NO EVALUADO"
VEREDICTOS = (PASA, FALLA, NO_EVALUADO)


@dataclass
class Resultado:
    """Lo que devuelve cada prueba.

    Regla de oro: si la prueba no se pudo hacer (falta una herramienta, faltan permisos, un error del
    propio programa), el veredicto es NO EVALUADO, nunca FALLA. FALLA es solo para el hardware o el
    sistema que muestran un problema real.
    """

    codigo: str                     # "CPU", "RAM", "DISCO_sda", "GPU", "BATERIA_0", "SISTEMA"
    titulo: str                     # "Procesador", "Memoria RAM", ...
    veredicto: str = NO_EVALUADO
    motivo: str = ""
    lineas: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    acciones: list[str] = field(default_factory=list)   # qué hacer, en palabras simples
    grafico: dict | None = None                          # datos opcionales para un gráfico del PDF

    def __post_init__(self):
        if self.veredicto not in VEREDICTOS:
            raise ValueError(f"veredicto inválido: {self.veredicto}")

    def poner(self, veredicto: str, motivo: str) -> Resultado:
        if veredicto not in VEREDICTOS:
            raise ValueError(f"veredicto inválido: {veredicto}")
        self.veredicto, self.motivo = veredicto, motivo
        return self

    def a_dict(self) -> dict:
        return {"codigo": self.codigo, "titulo": self.titulo, "veredicto": self.veredicto, "motivo": self.motivo,
                "lineas": list(self.lineas), "advertencias": list(self.advertencias), "acciones": list(self.acciones),
                "grafico": self.grafico}

    @classmethod
    def de_dict(cls, d: dict) -> Resultado:
        return cls(codigo=d["codigo"], titulo=d["titulo"], veredicto=d.get("veredicto", NO_EVALUADO),
                   motivo=d.get("motivo", ""), lineas=list(d.get("lineas") or []),
                   advertencias=list(d.get("advertencias") or []), acciones=list(d.get("acciones") or []),
                   grafico=d.get("grafico"))

    def a_texto(self) -> str:
        partes = [f"== {self.titulo} =="]
        partes += ["  " + l for l in self.lineas]
        partes += ["  [ADVERTENCIA] " + a for a in self.advertencias]
        partes.append(f"  VEREDICTO: {self.veredicto}" + (f" - {self.motivo}" if self.motivo else ""))
        return "\n".join(partes)


def veredicto_general(resultados: list[Resultado]) -> str:
    if not resultados:
        return "SIN PRUEBAS"
    if any(r.veredicto == FALLA for r in resultados):
        return "CON FALLAS"
    if any(r.veredicto == NO_EVALUADO for r in resultados):
        return "SIN FALLAS (CON PRUEBAS NO EVALUADAS)"
    return "SIN FALLAS"


def peor(veredictos: list[str]) -> str:
    """El veredicto que manda en un grupo: FALLA > NO EVALUADO > PASA."""
    if not veredictos:
        return NO_EVALUADO
    if FALLA in veredictos:
        return FALLA
    if NO_EVALUADO in veredictos:
        return NO_EVALUADO
    return PASA


class Cancelado(Exception):
    """El técnico canceló el diagnóstico."""


class Progreso:
    """Canal entre una prueba (que corre en un hilo) y la interfaz."""

    def __init__(self):
        self._lock = threading.Lock()
        self.fase = ""
        self.porcentaje = 0.0
        self._log: list[str] = []
        self.cancelar = threading.Event()

    def set_fase(self, texto: str) -> None:
        with self._lock:
            self.fase = texto
            self._log.append(time.strftime("[%H:%M:%S] ") + texto)
            del self._log[:-500]

    def set_porcentaje(self, p: float) -> None:
        with self._lock:
            self.porcentaje = max(0.0, min(100.0, float(p)))

    def log(self, texto: str) -> None:
        with self._lock:
            self._log.append(time.strftime("[%H:%M:%S] ") + texto)
            del self._log[:-500]

    def ultimas(self, n: int = 50) -> list[str]:
        with self._lock:
            return self._log[-n:]

    @property
    def cancelado(self) -> bool:
        return self.cancelar.is_set()
