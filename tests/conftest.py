"""Piezas compartidas por los tests: configuración en una carpeta temporal, una plataforma simulada
(sin tocar el hardware real) y un servidor SMTP falso para probar el correo sin mandar nada de verdad."""
from __future__ import annotations

import base64
import os
import socket
import socketserver
import threading
import time

import pytest

from openstd.config import Config
from openstd.plataforma.base import Plataforma, eventos_vacios
from openstd.pruebas import Contexto
from openstd.resultado import Progreso
from openstd.sensores import Muestreador


def equipo_ejemplo(ruta_disco: str = "", tam_disco: int = 0) -> dict:
    return {
        "fabricante": "Fabricante SA", "modelo": "Modelo X1", "serie": "ABC123", "placa": "Placa Z", "bios": "1.0",
        "nombre_pc": "PC-PRUEBA", "sistema": "Sistema de prueba", "es_portatil": True,
        "cpu": {"nombre": "CPU de prueba", "nucleos": 2, "hilos": 2, "mhz": 3000},
        "ram": {"instalada": 8 * 1024 ** 3, "visible": 8 * 1024 ** 3, "modulos": []},
        "video": [{"nombre": "GPU de prueba", "driver": "1.2.3"}],
        "discos": [{"id": "0", "ruta": ruta_disco, "smart_ruta": "/dev/sda", "modelo": "Disco de prueba", "serie": "D1",
                    "tam": tam_disco, "tipo": "SSD", "bus": "SATA", "es_usb": False, "es_sistema": True, "sector": 512,
                    "salud_so": None},
                   {"id": "1", "ruta": "", "smart_ruta": "/dev/sdb", "modelo": "Pendrive", "serie": "", "tam": 16e9,
                    "tipo": "SSD", "bus": "USB", "es_usb": True, "es_sistema": False, "sector": 512, "salud_so": None}],
        "baterias": 1,
    }


class PlataformaFalsa(Plataforma):
    """Devuelve lo que se le cargue. No lee nada del equipo real."""

    nombre = "Falsa"

    def __init__(self, equipo=None, eventos=None, baterias=None, admin=True, gpu_errores=None, ops_gpu=None):
        self._equipo = equipo or equipo_ejemplo()
        self._eventos = eventos if eventos is not None else eventos_vacios(True, fuente="prueba")
        self._baterias = baterias if baterias is not None else [
            {"nombre": "BAT0", "fabricante": "ACME", "modelo": "B1", "quimica": "Li-ion", "diseno": 50_000_000,
             "actual": 45_000_000, "unidad": "µWh", "ciclos": 120, "estado": "Discharging", "carga": 80}]
        self._admin = admin
        self._gpu_errores = gpu_errores or []
        self._ops_gpu = ops_gpu or []
        self.llamadas: list[str] = []

    def equipo(self):
        self.llamadas.append("equipo")
        return self._equipo

    def es_admin(self):
        return self._admin

    def smartctl(self, carpetas):
        return None

    def baterias(self):
        return self._baterias

    def eventos(self, desde):
        self.llamadas.append(f"eventos:{int(desde)}")
        return self._eventos

    def volumenes(self):
        return [{"nombre": "C:\\", "total": 500e9, "libre": 200e9, "sistema": True}]

    def _gpu_herramientas(self, carpetas):
        return [dict(o) for o in self._ops_gpu]

    def gpu_errores(self, desde, marca=None):
        return list(self._gpu_errores)

    def leer_sensores(self):
        return {"cpu_temp": 55.0, "mhz": 3000.0, "fuente_temp": "sensor de prueba"}


@pytest.fixture
def cfg(tmp_path):
    return Config(str(tmp_path / "datos"))


@pytest.fixture
def plat():
    return PlataformaFalsa()


@pytest.fixture
def ctx(cfg, plat):
    return Contexto(equipo=plat.equipo(), config=cfg, plat=plat, muestreador=Muestreador(intervalo=0.2, plat=plat), progreso=Progreso())


# ============================================================================ servidor SMTP falso
class _ManejadorSMTP(socketserver.StreamRequestHandler):
    def _decir(self, texto):
        self.wfile.write((texto + "\r\n").encode())

    def handle(self):
        srv = self.server
        self._decir("220 smtp-falso listo")
        datos, en_datos, autenticado = [], False, False
        while True:
            linea = self.rfile.readline()
            if not linea:
                return
            if en_datos:
                if linea in (b".\r\n", b".\n"):
                    en_datos = False
                    srv.recibidos.append({"de": srv._de, "para": list(srv._para), "datos": b"".join(datos), "autenticado": autenticado})
                    datos = []
                    self._decir("250 OK")
                else:
                    datos.append(linea[1:] if linea.startswith(b"..") else linea)
                continue
            cmd = linea.decode("utf-8", "replace").strip()
            u = cmd.upper()
            if u.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-smtp-falso\r\n250-AUTH PLAIN\r\n250 8BITMIME\r\n")
            elif u.startswith("AUTH PLAIN"):
                partes = cmd.split()
                cred = partes[2] if len(partes) > 2 else ""
                _, usuario, clave = base64.b64decode(cred).decode().split("\0")
                if (usuario, clave) == (srv.usuario, srv.clave):
                    autenticado = True
                    self._decir("235 Autenticado")
                else:
                    self._decir("535 5.7.8 Usuario o clave incorrectos")
            elif u.startswith("MAIL FROM"):
                if srv.usuario and not autenticado:
                    self._decir("530 Hace falta autenticarse")
                    continue
                srv._de, srv._para = cmd.split(":", 1)[1].strip(" <>"), []
                self._decir("250 OK")
            elif u.startswith("RCPT TO"):
                srv._para.append(cmd.split(":", 1)[1].strip(" <>"))
                self._decir("250 OK")
            elif u == "DATA":
                en_datos = True
                self._decir("354 Mandá los datos")
            elif u == "QUIT":
                self._decir("221 Chau")
                return
            elif u in ("RSET", "NOOP"):
                self._decir("250 OK")
            else:
                self._decir("502 No implementado")


class ServidorSMTP(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, usuario="tecnico@ejemplo.com", clave="clave-de-aplicacion"):
        super().__init__(("127.0.0.1", 0), _ManejadorSMTP)
        self.usuario, self.clave = usuario, clave
        self.recibidos: list[dict] = []
        self._de, self._para = "", []

    @property
    def puerto(self):
        return self.server_address[1]

    def esperar(self, n=1, segundos=5):
        fin = time.time() + segundos
        while len(self.recibidos) < n and time.time() < fin:
            time.sleep(0.05)
        return self.recibidos


@pytest.fixture
def smtp():
    srv = ServidorSMTP()
    hilo = threading.Thread(target=srv.serve_forever, daemon=True)
    hilo.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def cfg_correo(cfg, smtp):
    errores = cfg.guardar({"smtp_servidor": "127.0.0.1", "smtp_puerto": smtp.puerto, "smtp_seguridad": "ninguna",
                           "smtp_usuario": smtp.usuario, "smtp_clave": smtp.clave, "destinatarios": "reportes@ejemplo.com",
                           "taller_nombre": "Taller de Prueba"})
    assert errores == []
    return cfg


def puerto_cerrado() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


os.environ.setdefault("OPENSTD_SIN_NAVEGADOR", "1")
