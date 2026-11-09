"""Muestreo de sensores mientras corre el diagnóstico (para el gráfico en vivo y el reporte):
uso de CPU y RAM (psutil), frecuencia, temperaturas de CPU y GPU y uso de GPU (lo que la plataforma pueda leer).
"""
from __future__ import annotations

import threading
import time

from . import plataforma


class Muestreador:
    def __init__(self, intervalo: float = 2.0, plat=None):
        self.intervalo = intervalo
        self._plat = plat
        self._lock = threading.Lock()
        self._muestras: list[dict] = []
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self.fuente_temp = ""

    @property
    def plat(self):
        return self._plat or plataforma.actual()

    def iniciar(self) -> Muestreador:
        if self._hilo and self._hilo.is_alive():
            return self
        self._parar.clear()
        with self._lock:
            self._muestras = []
        try:
            self.plat.iniciar_sensores()
        except Exception:  # noqa: BLE001 - sin sensores el diagnóstico sigue igual
            pass
        self._hilo = threading.Thread(target=self._bucle, daemon=True, name="muestreador")
        self._hilo.start()
        return self

    def detener(self) -> None:
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout=10)
        try:
            self.plat.detener_sensores()
        except Exception:  # noqa: BLE001
            pass

    def _bucle(self) -> None:
        try:
            import psutil
            psutil.cpu_percent(None)
        except ImportError:
            psutil = None
        while not self._parar.wait(self.intervalo):
            m = {"t": time.time()}
            if psutil:
                m["cpu"] = psutil.cpu_percent(None)
                m["ram"] = psutil.virtual_memory().percent
            try:
                s = self.plat.leer_sensores() or {}
            except Exception:  # noqa: BLE001
                s = {}
            for k in ("cpu_temp", "gpu_temp", "gpu_uso", "mhz"):
                if s.get(k) is not None:
                    m[k] = float(s[k])
            if s.get("fuente_temp"):
                self.fuente_temp = s["fuente_temp"]
            with self._lock:
                self._muestras.append(m)
                del self._muestras[:-5000]

    def marca(self) -> float:
        return time.time()

    def desde(self, t: float) -> list[dict]:
        with self._lock:
            return [m for m in self._muestras if m["t"] >= t]

    def agregar(self, m: dict) -> None:
        """Para las pruebas: agrega una muestra a mano."""
        with self._lock:
            self._muestras.append(m)

    def estadisticas(self, desde: float) -> dict:
        return estadisticas(self.desde(desde), self.fuente_temp)


def estadisticas(muestras: list[dict], fuente_temp: str = "") -> dict:
    def st(k):
        v = [m[k] for m in muestras if m.get(k) is not None]
        return {"min": min(v), "max": max(v), "prom": sum(v) / len(v)} if v else None
    return {"cpu_temp": st("cpu_temp"), "gpu_temp": st("gpu_temp"), "gpu_uso": st("gpu_uso"), "uso_cpu": st("cpu"),
            "mhz": [m["mhz"] for m in muestras if m.get("mhz")], "fuente_temp": fuente_temp}


muestreador = Muestreador()
