"""Configuración: valores de fábrica + lo que se cambia desde el panel (datos/config.json).

Dónde se guardan los datos (config, reportes, historial):
  1. la carpeta que diga la variable OPENSTD_DATOS;
  2. modo portable: la carpeta "datos" junto al programa, si existe (los lanzadores la crean,
     así todo viaja en el pendrive);
  3. si no, la carpeta del usuario: %LOCALAPPDATA%\\OpenSTD en Windows, ~/.local/share/openstd en Linux.

La configuración de fábrica NO trae ningún correo ni contraseña: se cargan desde Ajustes.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading

from .util import ES_WINDOWS

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # carpeta que contiene el paquete

SEGURIDADES = ("starttls", "ssl", "ninguna")
MODOS_DISCO = ("rapido", "completo")

POR_DEFECTO: dict = {
    # Datos del taller o empresa (aparecen en el PDF y en los correos)
    "taller_nombre": "",
    "taller_contacto": "",          # teléfono, web, dirección: una línea al pie del reporte
    "logo": "",                     # ruta a una imagen PNG/JPG (opcional)

    # Correo (SMTP). Gmail y Microsoft 365 piden una "contraseña de aplicación"
    "smtp_servidor": "",
    "smtp_puerto": 587,
    "smtp_seguridad": "starttls",   # starttls (587), ssl (465) o ninguna
    "smtp_usuario": "",
    "smtp_clave": "",
    "remitente_nombre": "",
    "remitente_correo": "",         # vacío = el mismo usuario SMTP
    "destinatarios": "",            # separados por coma
    "enviar_al_terminar": True,

    # Pruebas
    "minutos_cpu": 10,
    "minutos_ram": 15,
    "minutos_gpu": 10,
    "ram_reserva_mb": 1024,         # memoria que se le deja al sistema durante la prueba de RAM
    "disco_modo": "rapido",         # rapido: 256 zonas repartidas · completo: todo el disco
    "gpu_vram_mb": 512,             # memoria de video que verifica la prueba de GPU propia
    "gpu_ventana": True,            # mostrar la prueba de GPU propia en una ventana con una escena 3D
    "gpu_comando": "",              # comando externo para cargar la GPU (opcional; la prueba propia no lo necesita)
    "dias_eventos": 30,             # cuántos días hacia atrás mira el chequeo del sistema

    # Umbrales
    "temp_cpu_alerta": 90,
    "temp_gpu_alerta": 88,
    "bateria_alerta": 80,           # % de salud: debajo es advertencia
    "bateria_falla": 50,            # % de salud: debajo es FALLA

    # Panel
    "puerto": 8765,
    "abrir_navegador": True,

    # Fecha desde la que se cuentan los errores del sistema, por equipo: {clave: {"desde": ts, "motivo": ""}}
    "referencias": {},
}

LIMITES = {
    "smtp_puerto": (1, 65535), "minutos_cpu": (0.5, 600), "minutos_ram": (0.5, 600), "minutos_gpu": (0.5, 600),
    "ram_reserva_mb": (256, 65536), "gpu_vram_mb": (64, 16384), "dias_eventos": (1, 365), "temp_cpu_alerta": (50, 110), "temp_gpu_alerta": (50, 110),
    "bateria_alerta": (1, 100), "bateria_falla": (1, 100), "puerto": (1024, 65535),
}
SECRETOS = ("smtp_clave",)


def carpeta_datos() -> str:
    if os.environ.get("OPENSTD_DATOS"):
        return os.environ["OPENSTD_DATOS"]
    portable = os.path.join(RAIZ, "datos")
    if os.path.isdir(portable) and _se_puede_escribir(portable):
        return portable
    if ES_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OpenSTD")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "openstd")


def _se_puede_escribir(d: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=d):
            pass
        return True
    except OSError:
        return False


class Config:
    def __init__(self, carpeta: str | None = None):
        self.carpeta = carpeta or carpeta_datos()
        self._lock = threading.RLock()
        self._valores: dict | None = None

    # ------------------------------------------------------------------ rutas
    @property
    def ruta(self) -> str:
        return os.path.join(self.carpeta, "config.json")

    @property
    def carpeta_reportes(self) -> str:
        return os.path.join(self.carpeta, "reportes")

    @property
    def carpeta_historial(self) -> str:
        return os.path.join(self.carpeta, "historial")

    @property
    def carpeta_cola(self) -> str:
        return os.path.join(self.carpeta, "cola_correo")

    @property
    def carpetas_herramientas(self) -> list[str]:
        return [os.path.join(RAIZ, "herramientas"), os.path.join(self.carpeta, "herramientas")]

    # ------------------------------------------------------------------ lectura
    def cargar(self) -> dict:
        with self._lock:
            if self._valores is None:
                v = copy.deepcopy(POR_DEFECTO)
                try:
                    with open(self.ruta, encoding="utf-8") as f:
                        guardado = json.load(f)
                    if isinstance(guardado, dict):
                        for k, val in guardado.items():
                            if k in POR_DEFECTO:
                                v[k] = val
                except (OSError, ValueError):
                    pass
                self._valores = v
            return copy.deepcopy(self._valores)

    def get(self, clave: str):
        return self.cargar().get(clave, copy.deepcopy(POR_DEFECTO.get(clave)))

    def __getitem__(self, clave: str):
        return self.get(clave)

    def publico(self) -> dict:
        """La configuración para mostrar en el panel: las contraseñas no salen nunca."""
        v = self.cargar()
        for s in SECRETOS:
            v[s + "_configurada"] = bool(v.pop(s, ""))
        v.pop("referencias", None)
        v["carpeta_datos"] = self.carpeta
        return v

    def correo_listo(self) -> bool:
        v = self.cargar()
        return bool(v["smtp_servidor"] and destinatarios(v["destinatarios"]))

    # ------------------------------------------------------------------ escritura
    def validar(self, cambios: dict) -> tuple[dict, list[str]]:
        """Devuelve (cambios limpios, errores). Las claves desconocidas se ignoran."""
        limpios, errores = {}, []
        for k, v in (cambios or {}).items():
            if k not in POR_DEFECTO or k == "referencias":
                continue
            base = POR_DEFECTO[k]
            if k in SECRETOS:
                if v is None:
                    limpios[k] = ""            # borrar la contraseña
                elif isinstance(v, str) and v != "":
                    limpios[k] = v.strip() if k != "smtp_clave" else v
                continue                        # "" = dejar la que estaba
            if isinstance(base, bool):
                if not isinstance(v, bool):
                    errores.append(f"{k}: tiene que ser sí o no")
                    continue
                limpios[k] = v
            elif isinstance(base, (int, float)):
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    errores.append(f"{k}: tiene que ser un número")
                    continue
                lo, hi = LIMITES.get(k, (float("-inf"), float("inf")))
                if not lo <= n <= hi:
                    errores.append(f"{k}: tiene que estar entre {lo:g} y {hi:g}")
                    continue
                limpios[k] = int(n) if isinstance(base, int) and float(n).is_integer() else n
            else:
                if not isinstance(v, str):
                    errores.append(f"{k}: tiene que ser texto")
                    continue
                v = v.strip()
                if len(v) > 2000:
                    errores.append(f"{k}: demasiado largo")
                    continue
                limpios[k] = v
        if "smtp_seguridad" in limpios and limpios["smtp_seguridad"] not in SEGURIDADES:
            errores.append("smtp_seguridad: tiene que ser starttls, ssl o ninguna")
            limpios.pop("smtp_seguridad")
        if "disco_modo" in limpios and limpios["disco_modo"] not in MODOS_DISCO:
            errores.append("disco_modo: tiene que ser rapido o completo")
            limpios.pop("disco_modo")
        if "destinatarios" in limpios:
            malos = [d for d in _partir(limpios["destinatarios"]) if not correo_valido(d)]
            if malos:
                errores.append("destinatarios: dirección no válida: " + ", ".join(malos))
                limpios.pop("destinatarios")
        for k in ("smtp_usuario", "remitente_correo"):
            if limpios.get(k) and "@" in limpios[k] and not correo_valido(limpios[k]):
                errores.append(f"{k}: dirección no válida")
                limpios.pop(k)
        return limpios, errores

    def guardar(self, cambios: dict) -> list[str]:
        """Valida y guarda. Devuelve la lista de errores (vacía si todo bien); lo válido se guarda igual."""
        limpios, errores = self.validar(cambios)
        if limpios:
            with self._lock:
                v = self.cargar()
                v.update(limpios)
                self._escribir(v)
        return errores

    def poner_referencia(self, clave: str, desde: float, motivo: str = "") -> None:
        with self._lock:
            v = self.cargar()
            refs = dict(v.get("referencias") or {})
            refs[clave] = {"desde": float(desde), "motivo": motivo[:200]}
            v["referencias"] = refs
            self._escribir(v)

    def referencia(self, clave: str) -> dict | None:
        return (self.get("referencias") or {}).get(clave)

    def _escribir(self, v: dict) -> None:
        os.makedirs(self.carpeta, exist_ok=True)
        tmp = self.ruta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(v, f, indent=2, ensure_ascii=False)
        if not ES_WINDOWS:
            os.chmod(tmp, 0o600)            # tiene la contraseña del correo: solo la lee el dueño
        os.replace(tmp, self.ruta)
        self._valores = copy.deepcopy(v)


def _partir(texto: str) -> list[str]:
    return [d.strip() for d in (texto or "").replace(";", ",").split(",") if d.strip()]


def correo_valido(d: str) -> bool:
    if d.count("@") != 1 or any(c.isspace() for c in d) or any(c in d for c in "<>,;\"'"):
        return False
    usuario, dominio = d.split("@")
    return bool(usuario) and "." in dominio and not dominio.startswith(".") and not dominio.endswith(".")


def destinatarios(texto: str, extra: str = "") -> list[str]:
    """Lista de direcciones válidas, sin repetir (sin distinguir mayúsculas)."""
    vistos, lista = set(), []
    for d in _partir(texto) + _partir(extra):
        if correo_valido(d) and d.lower() not in vistos:
            vistos.add(d.lower())
            lista.append(d)
    return lista


_actual: Config | None = None


def actual() -> Config:
    global _actual
    if _actual is None:
        _actual = Config()
    return _actual


def usar(c: Config) -> None:
    """Para las pruebas: reemplaza la configuración global."""
    global _actual
    _actual = c
