"""Envío de reportes por correo (SMTP), con la cuenta que se configure en Ajustes.

- Seguridad: STARTTLS (puerto 587), SSL directo (465) o ninguna (servidores internos).
- Si el envío falla (sin internet, servidor caído), el correo queda en datos/cola_correo y se reintenta solo.
- Los errores se explican en palabras simples (contraseña rechazada, puerto bloqueado, etc.).
"""
from __future__ import annotations

import json
import os
import smtplib
import socket
import ssl
import threading
import time
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Config, destinatarios

PRESETS = {
    "gmail": {"smtp_servidor": "smtp.gmail.com", "smtp_puerto": 587, "smtp_seguridad": "starttls"},
    "outlook": {"smtp_servidor": "smtp.office365.com", "smtp_puerto": 587, "smtp_seguridad": "starttls"},
    "yahoo": {"smtp_servidor": "smtp.mail.yahoo.com", "smtp_puerto": 465, "smtp_seguridad": "ssl"},
    "zoho": {"smtp_servidor": "smtp.zoho.com", "smtp_puerto": 587, "smtp_seguridad": "starttls"},
}
AYUDA_CLAVE = {
    "gmail.com": "Gmail pide una 'contraseña de aplicación' de 16 letras (myaccount.google.com > Seguridad > "
                 "Contraseñas de aplicaciones). La contraseña normal de la cuenta no sirve.",
    "office365.com": "Microsoft 365 / Outlook pide una contraseña de aplicación o que el administrador habilite SMTP AUTH.",
    "yahoo.com": "Yahoo pide una 'contraseña de aplicación' (Seguridad de la cuenta > Generar contraseña de aplicación).",
}


class ErrorCorreo(Exception):
    pass


def contexto_ssl() -> ssl.SSLContext:
    """Los certificados del sistema. Si existe certifi (lo trae pip en muchas instalaciones), se suman sus raíces:
    un Windows recién instalado todavía no tiene todas y el correo falla con 'certificate verify failed'."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    return ctx


def _ayuda(servidor: str) -> str:
    for dominio, texto in AYUDA_CLAVE.items():
        if dominio in (servidor or ""):
            return " " + texto
    return ""


def armar(cfg: Config, asunto: str, cuerpo: str, adjuntos: list[str] = (), extra: str = "") -> EmailMessage:
    v = cfg.cargar()
    para = destinatarios(v["destinatarios"], extra)
    if not para:
        raise ErrorCorreo("No hay ningún destinatario configurado (Ajustes > Correo)")
    remitente = v["remitente_correo"] or v["smtp_usuario"]
    if "@" not in (remitente or ""):
        raise ErrorCorreo("Falta el correo del remitente (Ajustes > Correo)")
    msg = EmailMessage()
    msg["From"] = formataddr((v["remitente_nombre"] or v["taller_nombre"] or "OpenSTD Test", remitente))
    msg["To"] = ", ".join(para)
    msg["Subject"] = asunto
    msg["Message-ID"] = make_msgid(domain=remitente.split("@")[-1])
    msg.set_content(cuerpo)
    for a in adjuntos:
        if not a:
            continue
        if not os.path.exists(a):
            raise ErrorCorreo(f"No se encuentra el archivo adjunto {os.path.basename(a)}")
        with open(a, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(a))
    return msg


def enviar(cfg: Config, msg: EmailMessage, timeout: float = 40) -> None:
    """Manda el mensaje. Lanza ErrorCorreo con una explicación clara si falla."""
    v = cfg.cargar()
    host, puerto, seg = v["smtp_servidor"], int(v["smtp_puerto"]), v["smtp_seguridad"]
    usuario, clave = v["smtp_usuario"], v["smtp_clave"]
    if not host:
        raise ErrorCorreo("Falta configurar el servidor de correo (Ajustes > Correo)")
    try:
        if seg == "ssl":
            s = smtplib.SMTP_SSL(host, puerto, timeout=timeout, context=contexto_ssl())
        else:
            s = smtplib.SMTP(host, puerto, timeout=timeout)
        with s:
            s.ehlo()
            if seg == "starttls":
                s.starttls(context=contexto_ssl())
                s.ehlo()
            if usuario and clave:
                s.login(usuario, clave)
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise ErrorCorreo(f"El servidor rechazó el usuario o la contraseña ({e.smtp_code}).{_ayuda(host)}") from e
    except smtplib.SMTPNotSupportedError as e:
        raise ErrorCorreo("El servidor no acepta ese tipo de conexión: probá con otra seguridad (STARTTLS o SSL) o con otro puerto") from e
    except smtplib.SMTPRecipientsRefused as e:
        raise ErrorCorreo("El servidor rechazó la dirección de destino: revisá los destinatarios") from e
    except smtplib.SMTPSenderRefused as e:
        raise ErrorCorreo("El servidor rechazó el remitente: tiene que ser la misma cuenta del usuario SMTP") from e
    except ssl.SSLError as e:
        raise ErrorCorreo(f"Error de conexión segura ({str(e)[:80]}). Revisá que la fecha y hora de la PC estén bien "
                          "y que la seguridad coincida con el puerto (587 = STARTTLS, 465 = SSL)") from e
    except socket.gaierror as e:
        raise ErrorCorreo(f"No se encontró el servidor '{host}': revisá el nombre o la conexión a internet") from e
    except (TimeoutError, socket.timeout) as e:
        raise ErrorCorreo(f"El servidor no respondió en el puerto {puerto}: puede estar bloqueado por la red o el antivirus") from e
    except ConnectionRefusedError as e:
        raise ErrorCorreo(f"El servidor rechazó la conexión en el puerto {puerto}: revisá el puerto") from e
    except smtplib.SMTPServerDisconnected as e:
        raise ErrorCorreo("El servidor cortó la conexión: revisá que la seguridad coincida con el puerto (587 = STARTTLS, 465 = SSL)") from e
    except (smtplib.SMTPException, OSError) as e:
        raise ErrorCorreo(f"No se pudo enviar: {e}") from e


def probar(cfg: Config) -> None:
    """Manda un correo de prueba a los destinatarios configurados."""
    v = cfg.cargar()
    msg = armar(cfg, "Prueba de correo de OpenSTD Test",
                "Si te llegó este correo, la configuración está bien y los reportes van a llegar a esta dirección.\n\n"
                f"Enviado desde {socket.gethostname()} el {time.strftime('%d/%m/%Y %H:%M')}.\n"
                + (f"\n{v['taller_nombre']}" if v["taller_nombre"] else ""))
    enviar(cfg, msg)


# ============================================================================ cola de reintentos
class Cola:
    """Correos que no se pudieron mandar. Se guardan como archivos para que sobrevivan a un reinicio."""

    MAX_DIAS = 7

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.ultimo_error = ""

    @property
    def carpeta(self) -> str:
        return self.cfg.carpeta_cola

    def agregar(self, asunto: str, cuerpo: str, adjunto: str = "", extra: str = "") -> str:
        os.makedirs(self.carpeta, exist_ok=True)
        for p in self.pendientes():
            if adjunto and p.get("adjunto") == adjunto:
                return p["id"]                     # ese reporte ya está esperando
        id_ = time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6]
        with open(os.path.join(self.carpeta, id_ + ".json"), "w", encoding="utf-8") as f:
            json.dump({"id": id_, "creado": time.time(), "asunto": asunto, "cuerpo": cuerpo, "adjunto": adjunto,
                       "extra": extra, "intentos": 0, "error": ""}, f, ensure_ascii=False)
        return id_

    def pendientes(self) -> list[dict]:
        r = []
        try:
            nombres = sorted(os.listdir(self.carpeta))
        except OSError:
            return r
        for n in nombres:
            if n.endswith(".json"):
                try:
                    with open(os.path.join(self.carpeta, n), encoding="utf-8") as f:
                        r.append(json.load(f))
                except (OSError, ValueError):
                    continue
        return r

    def quitar(self, id_: str) -> None:
        try:
            os.remove(os.path.join(self.carpeta, id_ + ".json"))
        except OSError:
            pass

    def procesar(self) -> int:
        """Intenta mandar lo pendiente. Devuelve cuántos salieron."""
        if not self._lock.acquire(blocking=False):
            return 0
        enviados = 0
        try:
            for p in self.pendientes():
                if time.time() - p.get("creado", 0) > self.MAX_DIAS * 86400:
                    self.quitar(p["id"])
                    continue
                demora = ""
                if time.time() - p["creado"] > 180:
                    demora = (f"\n\n(Este correo es del {time.strftime('%d/%m %H:%M', time.localtime(p['creado']))} y salió "
                              "con demora porque antes no se pudo enviar.)")
                try:
                    enviar(self.cfg, armar(self.cfg, p["asunto"], p["cuerpo"] + demora, [p["adjunto"]] if p.get("adjunto") else [],
                                           p.get("extra", "")))
                except ErrorCorreo as e:
                    self.ultimo_error = str(e)
                    p["intentos"] = p.get("intentos", 0) + 1
                    p["error"] = str(e)
                    try:
                        with open(os.path.join(self.carpeta, p["id"] + ".json"), "w", encoding="utf-8") as f:
                            json.dump(p, f, ensure_ascii=False)
                    except OSError:
                        pass
                    break                          # si uno falla, los demás seguramente también
                self.quitar(p["id"])
                enviados += 1
                self.ultimo_error = ""
        finally:
            self._lock.release()
        return enviados

    def iniciar(self, parar: threading.Event, cada: float = 300) -> None:
        def bucle():
            while not parar.wait(5):
                if self.pendientes() and self.cfg.correo_listo():
                    self.procesar()
                parar.wait(cada)
        threading.Thread(target=bucle, daemon=True, name="cola-correo").start()
