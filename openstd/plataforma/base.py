"""Lo que cada sistema operativo tiene que saber hacer. Todo devuelve datos simples (dicts y listas)
con el mismo formato en Windows y en Linux, así las pruebas y su evaluación son una sola.

Formato de equipo():
    {"fabricante", "modelo", "serie", "placa", "bios", "nombre_pc", "sistema", "es_portatil": bool,
     "cpu": {"nombre", "nucleos", "hilos", "mhz"},
     "ram": {"instalada": bytes, "visible": bytes, "modulos": [{"ranura", "tam", "tipo", "velocidad", "fabricante", "parte"}]},
     "video": [{"nombre", "driver"}],
     "discos": [{"id", "ruta", "smart_ruta", "modelo", "serie", "tam", "tipo", "bus", "es_usb", "es_sistema", "sector",
                 "salud_so": {...} o None}],
     "baterias": int}

Formato de eventos(desde) (para el chequeo del sistema):
    {"disponible": bool, "motivo": str, "fuente": str,
     "pantallazos": [{"t", "codigo", "texto"}], "apagados": [{"t", "texto"}],
     "hardware": [{"t", "texto", "grave": bool}], "disco": [{"t", "texto", "grave": bool}],
     "video": [{"t", "texto"}], "memoria": [{"t", "texto"}], "drivers": [{"nombre", "codigo"}]}
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shlex

from ..util import ES_WINDOWS, es_admin


def eventos_vacios(disponible: bool = True, motivo: str = "", fuente: str = "") -> dict:
    return {"disponible": disponible, "motivo": motivo, "fuente": fuente, "pantallazos": [], "apagados": [],
            "hardware": [], "disco": [], "video": [], "memoria": [], "drivers": []}


class LectorDisco:
    """Lectura cruda de un disco (o de un archivo, para las pruebas). Solo lectura."""

    sin_cache = False

    def __init__(self, ruta: str):
        self._f = open(ruta, "rb", buffering=0)

    def leer(self, offset: int, buf) -> int:
        self._f.seek(offset)
        return self._f.readinto(buf) or 0

    def cerrar(self) -> None:
        self._f.close()


class Plataforma:
    nombre = "base"

    # ------------------------------------------------------------------ equipo
    def equipo(self) -> dict:
        raise NotImplementedError

    def es_admin(self) -> bool:
        return es_admin()

    @staticmethod
    def identificacion(e: dict) -> str:
        texto = " ".join(x for x in (e.get("fabricante"), e.get("modelo")) if x) or e.get("placa") or e.get("nombre_pc") or "Equipo"
        if e.get("serie"):
            texto += f" (S/N {e['serie']})"
        return texto

    @staticmethod
    def clave_equipo(e: dict) -> str:
        """Identifica a la PC entre sesiones (para la fecha de referencia de errores)."""
        base = e.get("serie") or "|".join(str(e.get(k) or "") for k in ("fabricante", "modelo", "placa", "nombre_pc"))
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ disco
    def abrir_disco(self, disco: dict) -> LectorDisco:
        return LectorDisco(disco["ruta"])

    def smartctl(self, carpetas_herramientas: list[str]) -> str | None:
        raise NotImplementedError

    # ------------------------------------------------------------------ batería
    def baterias(self) -> list[dict]:
        """[{"nombre", "fabricante", "modelo", "quimica", "diseno", "actual", "unidad", "ciclos", "estado", "carga"}]"""
        return []

    # ------------------------------------------------------------------ sistema
    def eventos(self, desde: float) -> dict:
        return eventos_vacios(False, "No disponible en este sistema")

    def volumenes(self) -> list[dict]:
        """[{"nombre", "total", "libre", "sistema": bool}]"""
        import psutil
        r = []
        sistema = os.environ.get("SystemDrive", "C:") + "\\" if ES_WINDOWS else "/"
        for p in psutil.disk_partitions(all=False):
            if not ES_WINDOWS and (p.mountpoint.startswith(("/snap", "/boot/efi")) or p.fstype in ("squashfs", "iso9660")):
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            if u.total < 1e9:
                continue
            r.append({"nombre": p.mountpoint, "total": u.total, "libre": u.free,
                      "sistema": p.mountpoint.rstrip("\\/").lower() == sistema.rstrip("\\/").lower() or p.mountpoint == sistema})
        return r

    # ------------------------------------------------------------------ GPU
    def gpu_opciones(self, carpetas: list[str], comando_propio: str = "") -> list[dict]:
        """Formas de cargar la GPU: [{"id", "nombre", "cmd": [..], "cwd", "entorno": {}}]."""
        ops = self._gpu_herramientas(carpetas)
        if comando_propio.strip():
            try:
                cmd = shlex.split(comando_propio, posix=not ES_WINDOWS)
            except ValueError:
                cmd = []
            if cmd:
                cmd = [c.strip('"') for c in cmd] if ES_WINDOWS else cmd
                ops.insert(0, {"nombre": f"Comando propio ({os.path.basename(cmd[0])})", "cmd": cmd, "cwd": None, "entorno": {}})
        for i, o in enumerate(ops):
            o["id"] = i
        return ops

    def _gpu_herramientas(self, carpetas: list[str]) -> list[dict]:
        return []

    def gpu_preparar(self) -> dict:
        """Antes de la prueba de GPU: {"problema": str|None, "aviso": str|None, "entorno": {}, "lineas": []}."""
        return {"problema": None, "aviso": None, "entorno": {}, "lineas": []}

    def gpu_errores(self, desde: float, marca=None) -> list[str]:
        """Errores del driver de video desde ese momento."""
        return []

    def gpu_marca(self):
        return None

    def preferir_gpu_dedicada(self, exe: str) -> None:
        pass

    # ------------------------------------------------------------------ sensores
    def iniciar_sensores(self) -> None:
        pass

    def detener_sensores(self) -> None:
        pass

    def leer_sensores(self) -> dict:
        """{"cpu_temp", "gpu_temp", "gpu_uso", "mhz", "fuente_temp"} (lo que no haya, None)."""
        return {}

    def mantener_despierto(self, si: bool) -> None:
        pass


def buscar(carpetas: list[str], *patrones: str, profundidad: int = 3) -> list[str]:
    """Busca archivos en las carpetas de herramientas (sin bajar más de `profundidad` niveles)."""
    encontrados = []
    for raiz in carpetas:
        if not os.path.isdir(raiz):
            continue
        for nivel in range(profundidad + 1):
            for patron in patrones:
                comodin = os.path.join(raiz, *(["*"] * nivel), patron)
                for c in sorted(glob.glob(comodin)):
                    if c not in encontrados and os.path.isfile(c):
                        encontrados.append(c)
    return encontrados


def numero_hex(texto: str) -> int | None:
    """'0x0000007e (0xffff...)' -> 0x7e."""
    m = re.search(r"0x([0-9a-fA-F]+)", texto or "")
    if m:
        return int(m.group(1), 16)
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None
