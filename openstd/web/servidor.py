"""Panel web local (solo en esta PC: escucha en 127.0.0.1, no abre el firewall).

Protecciones: solo acepta el encabezado Host de esta máquina (evita ataques de 'DNS rebinding') y los POST
tienen que traer 'X-OpenSTD: 1' con JSON (una página de internet no puede mandar eso sin permiso del navegador).
"""
from __future__ import annotations

import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__, correo
from ..diagnostico import Diagnostico

AQUI = os.path.dirname(os.path.abspath(__file__))
MAX_CUERPO = 4 * 1024 * 1024


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, direccion, diag: Diagnostico):
        super().__init__(direccion, Manejador)
        self.diag = diag


class Manejador(BaseHTTPRequestHandler):
    server: Servidor
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):        # sin ruido en la consola
        pass

    # ------------------------------------------------------------------ respuestas
    def _enviar(self, codigo: int, cuerpo: bytes, tipo: str, extra: dict | None = None) -> None:
        if codigo in (403, 413) and self.command == "POST":
            self.close_connection = True       # el cuerpo del pedido rechazado no se leyó: se cierra la conexión
        self.send_response(codigo)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def _json(self, codigo: int, datos) -> None:
        self._enviar(codigo, json.dumps(datos, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _host_valido(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        puerto = self.server.server_address[1]
        return host in (f"localhost:{puerto}", f"127.0.0.1:{puerto}", f"[::1]:{puerto}")

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        if not self._host_valido():
            return self._json(403, {"error": "Acceso no permitido"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        d = self.server.diag
        if u.path in ("/", "/index.html"):
            with open(os.path.join(AQUI, "panel.html"), "rb") as f:
                html = f.read().replace(b"{{VERSION}}", __version__.encode())
            return self._enviar(200, html, "text/html; charset=utf-8",
                                {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})
        if u.path in ("/logo.png", "/icono.png"):
            with open(os.path.join(AQUI, u.path[1:]), "rb") as f:
                return self._enviar(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})
        if u.path == "/api/opciones":
            return self._json(200, d.opciones())
        if u.path == "/api/estado":
            try:
                desde = float(q.get("desde") or 0)
            except ValueError:
                desde = 0
            return self._json(200, d.estado_publico(desde))
        if u.path == "/api/historial":
            return self._json(200, d.historial.lista())
        if u.path == "/api/config":
            return self._json(200, d.cfg.publico() | {"presets": correo.PRESETS})
        if u.path == "/api/correo/cola":
            return self._json(200, {"pendientes": len(d.cola.pendientes()), "error": d.cola.ultimo_error})
        if u.path.startswith("/pdf/"):
            reg = d.historial.leer(u.path[5:])
            ruta = d.historial.ruta_pdf(reg) if reg else ""
            if not ruta or not os.path.isfile(ruta):
                return self._json(404, {"error": "No existe ese PDF"})
            with open(ruta, "rb") as f:
                datos = f.read()
            return self._enviar(200, datos, "application/pdf",
                                {"Content-Disposition": f'inline; filename="{os.path.basename(ruta)}"'})
        return self._json(404, {"error": "No encontrado"})

    do_HEAD = do_GET

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_valido():
            return self._json(403, {"error": "Acceso no permitido"})
        if self.headers.get("X-OpenSTD") != "1" or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(403, {"error": "Solicitud no permitida"})
        try:
            largo = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            largo = -1
        if not 0 <= largo <= MAX_CUERPO:
            return self._json(413, {"error": "Solicitud demasiado grande"})
        try:
            datos = json.loads(self.rfile.read(largo) or b"{}")
            if not isinstance(datos, dict):
                raise ValueError
        except ValueError:
            return self._json(400, {"error": "JSON inválido"})
        d = self.server.diag
        ruta = urlparse(self.path).path
        if ruta == "/api/iniciar":
            err = d.iniciar(datos)
            return self._json(400, {"error": err}) if err else self._json(200, {"ok": True})
        if ruta == "/api/cancelar":
            return self._json(200, {"ok": True}) if d.cancelar() else self._json(400, {"error": "No hay ningún diagnóstico corriendo"})
        if ruta == "/api/enviar":
            r = d.reenviar(str(datos.get("id") or ""), str(datos.get("extra") or ""))
            return self._json(200 if r["estado"] == "enviado" else 400, r if r["estado"] == "enviado" else {"error": r["texto"]})
        if ruta == "/api/contar_desde_hoy":
            err = d.contar_desde_hoy()
            return self._json(400, {"error": err}) if err else self._json(200, {"ok": True})
        if ruta == "/api/releer":
            d.cargar_equipo(fresco=True)
            return self._json(200, {"ok": True})
        if ruta == "/api/config":
            errores = d.cfg.guardar(datos)
            if "gpu_comando" in datos:
                d.cargar_equipo(fresco=True)
            return self._json(400 if errores else 200, {"errores": errores, "config": d.cfg.publico()})
        if ruta == "/api/correo/probar":
            try:
                correo.probar(d.cfg)
            except correo.ErrorCorreo as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"ok": True})
        if ruta == "/api/correo/reintentar":
            n = d.cola.procesar()
            return self._json(200, {"enviados": n, "pendientes": len(d.cola.pendientes()), "error": d.cola.ultimo_error})
        if ruta == "/api/logo":
            return self._logo(datos)
        return self._json(404, {"error": "No encontrado"})

    def _logo(self, datos: dict):
        d = self.server.diag
        if datos.get("borrar"):
            d.cfg.guardar({"logo": ""})
            return self._json(200, {"ok": True})
        try:
            contenido = base64.b64decode(str(datos.get("datos") or ""), validate=True)
        except ValueError:
            return self._json(400, {"error": "Archivo inválido"})
        if contenido.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif contenido.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        else:
            return self._json(400, {"error": "El logo tiene que ser una imagen PNG o JPG"})
        if len(contenido) > 2 * 1024 * 1024:
            return self._json(400, {"error": "El logo no puede pesar más de 2 MB"})
        os.makedirs(d.cfg.carpeta, exist_ok=True)
        ruta = os.path.join(d.cfg.carpeta, "logo" + ext)
        with open(ruta, "wb") as f:
            f.write(contenido)
        d.cfg.guardar({"logo": ruta})
        return self._json(200, {"ok": True})


def servir(diag: Diagnostico, puerto: int = 8765, intentos: int = 10) -> Servidor:
    """Abre el panel en el primer puerto libre desde `puerto`. Corre en un hilo aparte."""
    ultimo = None
    for p in range(puerto, puerto + intentos):
        try:
            srv = Servidor(("127.0.0.1", p), diag)
            break
        except OSError as e:
            ultimo = e
    else:
        raise OSError(f"No hay ningún puerto libre entre {puerto} y {puerto + intentos - 1}: {ultimo}")
    threading.Thread(target=srv.serve_forever, daemon=True, name="panel").start()
    return srv
