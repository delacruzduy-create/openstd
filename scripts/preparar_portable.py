"""Agrega al Python portable de Windows las librerías que usa OpenSTD Test, bajándolas de PyPI (el repositorio oficial)
y verificando el SHA-256 de cada archivo. Lo ejecuta preparar_portable.ps1 con el Python portable recién bajado.
"""
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.environ.get("OPENSTD_PY_DIR") or os.path.join(RAIZ, "python")
SITE = os.path.join(PY, "Lib", "site-packages")
PYPI = "https://pypi.org"
TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"

# (paquete, tipo de archivo): "puro" sirve en cualquier sistema, "windows" es compilado para Windows 64 bits
PAQUETES = [("reportlab", "puro"), ("charset-normalizer", "puro"), ("certifi", "puro"), ("pillow", "windows"), ("psutil", "windows"),
            # prueba de placa de video (wgpu) y su ventana (glfw); rendercanvas y numpy no hacen falta para esta prueba
            ("wgpu", "windows"), ("cffi", "windows"), ("pycparser", "puro"), ("glfw", "windows")]


def _json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def _sirve(archivo, tipo):
    if not archivo.endswith(".whl"):
        return False
    if tipo == "puro":
        return archivo.endswith(("py3-none-any.whl", "py2.py3-none-any.whl"))
    return archivo.endswith("win_amd64.whl") and (f"-{TAG}-" in archivo or "-abi3-" in archivo or "-py3-none-" in archivo or "py3-none-win_amd64" in archivo)


def _version(v):
    return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split("."))


def elegir(nombre, tipo):
    """La versión estable más nueva que tenga un archivo para este Python de Windows."""
    info = _json(f"{PYPI}/pypi/{nombre}/json")
    for v in sorted(info.get("releases", {}), key=_version, reverse=True):
        if any(c in v for c in "abrc") or "dev" in v:
            continue
        for u in info["releases"][v]:
            if _sirve(u["filename"], tipo) and not u.get("yanked"):
                return v, u
    raise RuntimeError(f"no encontré {nombre} para este Python ({TAG}, Windows 64 bits)")


def bajar(u):
    with urllib.request.urlopen(u["url"], timeout=300) as r:
        datos = r.read()
    esperado = (u.get("digests") or {}).get("sha256")
    if not esperado or hashlib.sha256(datos).hexdigest() != esperado:
        raise RuntimeError(f"{u['filename']} llegó dañado (no coincide la verificación SHA-256)")
    return datos


def configurar_pth():
    """El Python portable solo carga lo que dice su archivo ._pth: se agregan las librerías y la carpeta del programa."""
    pth = glob.glob(os.path.join(PY, "python3*._pth"))
    if not pth:
        raise RuntimeError("no encontré el archivo python3XX._pth (¿Python portable incompleto?)")
    with open(pth[0], encoding="utf-8") as f:
        zipname = next((l.strip() for l in f if l.strip().endswith(".zip")), f"python{TAG[2:]}.zip")
    with open(pth[0], "w", encoding="utf-8") as f:
        f.write(f"{zipname}\n.\nLib\\site-packages\n..\nimport site\n")


def main():
    os.makedirs(SITE, exist_ok=True)
    for nombre, tipo in PAQUETES:
        v, u = elegir(nombre, tipo)
        print(f"   {nombre} {v} ({u['size'] / 1e6:.1f} MB)...", flush=True)
        zipfile.ZipFile(io.BytesIO(bajar(u))).extractall(SITE)
    configurar_pth()
    exe = os.path.join(PY, "python.exe")
    p = subprocess.run([exe, "-c", "import reportlab, PIL, psutil, wgpu, glfw, openstd; print('ok')"], capture_output=True, text=True, cwd=RAIZ)
    if p.returncode:
        print(p.stdout + p.stderr)
        raise RuntimeError("las librerías no cargan")
    print("   Comprobado: las librerías cargan bien.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR: {e}")
        sys.exit(1)
