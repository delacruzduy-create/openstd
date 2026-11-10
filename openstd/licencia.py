"""Licencia de la versión Pro: una clave de Gumroad que se activa una vez (con internet) y queda guardada.

La clave se verifica con la API pública de Gumroad (https://api.gumroad.com/v2/licenses/verify) y se guarda en
la carpeta de datos (licencia.json), así viaja en el pendrive y después funciona sin internet.
Cada clave se puede activar en hasta MAX_ACTIVACIONES pendrives o PCs. Si hay internet, cada tanto se vuelve a
consultar: una compra devuelta o con contracargo deja de ser Pro.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ID del producto en Gumroad (Product → Content → License key → "product_id"). Vacío = la activación no está disponible.
GUMROAD_PRODUCT_ID = "qso0z3Fdk4Rc2jF8OrrxJQ=="
URL_VERIFICAR = "https://api.gumroad.com/v2/licenses/verify"
MAX_ACTIVACIONES = 3
REVISAR_CADA = 30 * 86400          # cada cuánto se vuelve a consultar a Gumroad, si hay internet


class ErrorLicencia(Exception):
    """La clave no es válida (texto para mostrar)."""


class SinConexion(Exception):
    """No se pudo hablar con Gumroad (no es culpa de la clave)."""


def consultar_gumroad(clave: str, sumar_uso: bool) -> dict:
    """Devuelve la respuesta de Gumroad si la clave existe. Lanza ErrorLicencia o SinConexion."""
    if not GUMROAD_PRODUCT_ID:
        raise ErrorLicencia("La activación todavía no está disponible en esta versión")
    datos = urllib.parse.urlencode({"product_id": GUMROAD_PRODUCT_ID, "license_key": clave,
                                    "increment_uses_count": "true" if sumar_uso else "false"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(URL_VERIFICAR, data=datos), timeout=20) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:                      # Gumroad contesta 404 cuando la clave no existe para el producto
            raise ErrorLicencia("Esa clave no es válida. Copiala tal cual del mail de compra.") from None
        raise SinConexion(f"Gumroad respondió con un error ({e.code})") from None
    except (OSError, ValueError) as e:
        raise SinConexion(f"No se pudo conectar con Gumroad: {e}") from None
    if not resp.get("success"):
        raise ErrorLicencia(resp.get("message") or "Esa clave no es válida")
    return resp


def _vigente(resp: dict) -> None:
    p = resp.get("purchase") or {}
    if p.get("refunded") or p.get("chargebacked") or p.get("disputed"):
        raise ErrorLicencia("Esa compra fue devuelta o cancelada")


def ocultar_correo(c: str) -> str:
    if "@" not in (c or ""):
        return ""
    nombre, dominio = c.split("@", 1)
    return nombre[:2] + "***@" + dominio


class Licencia:
    def __init__(self, carpeta: str, consultar=consultar_gumroad):
        self.ruta = os.path.join(carpeta, "licencia.json")
        self.consultar = consultar          # los tests lo reemplazan
        self._lock = threading.Lock()

    def _leer(self) -> dict:
        try:
            with open(self.ruta, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _escribir(self, d: dict) -> None:
        os.makedirs(os.path.dirname(self.ruta), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.ruta), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.ruta)

    @property
    def es_pro(self) -> bool:
        return bool(self._leer().get("clave"))

    def estado(self) -> dict:
        d = self._leer()
        clave = d.get("clave") or ""
        return {"pro": bool(clave), "correo": ocultar_correo(d.get("correo", "")),
                "clave": ("…" + clave[-4:]) if clave else "", "disponible": bool(GUMROAD_PRODUCT_ID) or self.consultar is not consultar_gumroad}

    def activar(self, clave: str) -> str | None:
        """Verifica la clave con Gumroad y la guarda. Devuelve un texto de error o None."""
        clave = "".join(str(clave or "").split()).upper()
        if not 8 <= len(clave) <= 80:
            return "Pegá la clave de licencia que te llegó en el mail de compra"
        with self._lock:
            if self._leer().get("clave") == clave:
                return None
            try:
                resp = self.consultar(clave, True)
                _vigente(resp)
            except ErrorLicencia as e:
                return str(e)
            except SinConexion as e:
                return f"{e}. Para activar hace falta internet una sola vez."
            if (resp.get("uses") or 0) > MAX_ACTIVACIONES:
                return (f"Esta clave ya se activó en {MAX_ACTIVACIONES} pendrives o PCs, que es el máximo. "
                        "Si cambiaste de pendrive, escribinos respondiendo el mail de la compra.")
            self._escribir({"clave": clave, "correo": (resp.get("purchase") or {}).get("email", ""),
                            "activada": time.time(), "revisada": time.time()})
        return None

    def desactivar(self) -> None:
        with self._lock:
            try:
                os.remove(self.ruta)
            except FileNotFoundError:
                pass

    def revisar(self) -> None:
        """Si toca, vuelve a consultar a Gumroad sin sumar un uso. Sin internet no cambia nada."""
        d = self._leer()
        if not d.get("clave") or time.time() - (d.get("revisada") or 0) < REVISAR_CADA:
            return
        if not GUMROAD_PRODUCT_ID and self.consultar is consultar_gumroad:
            return                              # sin producto configurado no se puede revisar: no se toca nada
        try:
            _vigente(self.consultar(d["clave"], False))
        except ErrorLicencia:
            self.desactivar()
            return
        except SinConexion:
            return
        with self._lock:
            d["revisada"] = time.time()
            self._escribir(d)
