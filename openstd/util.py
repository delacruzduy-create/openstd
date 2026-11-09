"""Funciones chicas que usan todas las partes del programa."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ES_WINDOWS = sys.platform == "win32"
ES_LINUX = sys.platform.startswith("linux")
SIN_VENTANA = 0x08000000 if ES_WINDOWS else 0          # CREATE_NO_WINDOW: que no aparezcan consolas


def hay(comando: str) -> bool:
    return shutil.which(comando) is not None


def correr(cmd: list[str], timeout: float = 60, entorno: dict | None = None, cwd: str | None = None) -> tuple[int, str]:
    """Corre un programa de consola y devuelve (código, salida+errores). Nunca lanza excepción.

    En Linux fuerza el idioma inglés (LC_ALL=C) para poder interpretar la salida. En Windows decodifica
    con la página de códigos OEM, que es la que usan los programas de consola.
    """
    env = dict(os.environ)
    if not ES_WINDOWS:
        env.update(LC_ALL="C", LANG="C")
    if entorno:
        env.update(entorno)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=cwd, creationflags=SIN_VENTANA)
    except FileNotFoundError:
        return -1, f"no está instalado: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "tiempo de espera agotado"
    except OSError as e:
        return -3, str(e)
    cod = "oem" if ES_WINDOWS else "utf-8"
    return p.returncode, (p.stdout or b"").decode(cod, "replace") + (p.stderr or b"").decode(cod, "replace")


def leer(ruta: str, por_defecto: str | None = None) -> str | None:
    """Lee un archivo de texto chico (por ejemplo de /sys)."""
    try:
        with open(ruta, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return por_defecto


def leer_int(ruta: str, por_defecto: int | None = None) -> int | None:
    try:
        return int(leer(ruta) or "")
    except ValueError:
        return por_defecto


def tam(b: float, decimal: bool = True) -> str:
    """Tamaño legible. decimal=True para discos (1 GB = 1000³), False para RAM (1 GB = 1024³)."""
    base = 1000.0 if decimal else 1024.0
    b = float(b or 0)
    if b >= base ** 4:
        return f"{b / base ** 4:.1f} TB".replace(".", ",")
    if b >= base ** 3:
        g = b / base ** 3
        return (f"{g:.0f} GB" if g >= 100 else f"{g:.1f} GB").replace(".", ",")
    return f"{b / base ** 2:.0f} MB"


def duracion(seg: float) -> str:
    seg = int(max(0, seg))
    if seg >= 3600:
        return f"{seg // 3600} h {seg % 3600 // 60:02d} min"
    if seg >= 60:
        return f"{seg // 60} min {seg % 60:02d} s"
    return f"{seg} s"


def plural(n: int, singular: str, plural_: str | None = None) -> str:
    """'1 error', '2 errores'."""
    return f"{n} {singular if n == 1 else (plural_ or singular + 's')}"


def es_admin() -> bool:
    """Administrador en Windows, root en Linux."""
    if ES_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0
