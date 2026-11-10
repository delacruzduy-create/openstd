"""Controles del repositorio: que no se cuelen datos privados ni marcas de terceros."""
import os
import re

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSIONES = (".py", ".html", ".md", ".toml", ".yml", ".yaml", ".bat", ".sh", ".ps1", ".txt", ".cfg", ".json")
SALTEAR = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "datos", "build", "dist", "venta"}  # venta: textos y zip de la versión Pro (no va a git)


def _archivos():
    for raiz, dirs, archivos in os.walk(RAIZ):
        dirs[:] = [d for d in dirs if d not in SALTEAR and not d.endswith(".egg-info")]
        for a in archivos:
            if a.endswith(EXTENSIONES) and a != "test_repositorio.py":
                yield os.path.join(raiz, a)


def test_sin_marcas_ni_contrasenas():
    prohibidos = [re.compile(p, re.I) for p in (r"pc\s?compu", r"pccompu\.com", r"[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}\"")]
    encontrados = []
    for ruta in _archivos():
        with open(ruta, encoding="utf-8", errors="replace") as f:
            texto = f.read()
        for p in prohibidos:
            if p.search(texto):
                encontrados.append(f"{os.path.relpath(ruta, RAIZ)}: {p.pattern}")
    assert encontrados == []


def test_bat_sin_parentesis_en_echo():
    """Un paréntesis en un 'echo' dentro de un bloque if (...) rompe el .bat y la ventana se cierra sola."""
    malos = []
    for nombre in os.listdir(RAIZ):
        if nombre.lower().endswith(".bat"):
            with open(os.path.join(RAIZ, nombre), encoding="utf-8") as f:
                for n, linea in enumerate(f, 1):
                    l = linea.strip().lower()
                    if l.startswith("echo") and ("(" in l or ")" in l):
                        malos.append(f"{nombre}:{n}")
                    if l.endswith("(") and l.startswith("if"):
                        malos.append(f"{nombre}:{n} (bloque if: usar goto)")
    assert malos == []
