"""Punto de entrada.

    python -m openstd                  abre el panel en el navegador
    python -m openstd info             muestra los datos del equipo (JSON)
    python -m openstd diagnosticar     corre el diagnóstico en la consola (ver --help)
    python -m openstd probar-correo    manda un correo de prueba con la configuración guardada
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time

from . import NOMBRE, __version__
from .util import ES_WINDOWS


def _consola_utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def cmd_panel(args) -> int:
    from . import plataforma
    from .config import actual
    from .diagnostico import Diagnostico
    from .web.servidor import servir

    cfg = actual()
    diag = Diagnostico(cfg)
    diag.cargar_equipo()
    parar = threading.Event()
    diag.cola.iniciar(parar)
    srv = servir(diag, args.puerto or cfg.get("puerto"))
    url = f"http://localhost:{srv.server_port}/"
    print("=" * 64)
    print(f" {NOMBRE} {__version__} · diagnóstico de PC")
    print("=" * 64)
    if not plataforma.actual().es_admin():
        print(" ATENCIÓN: sin permisos de " + ("administrador" if ES_WINDOWS else "root (sudo)")
              + ": la lectura de discos no va a poder hacerse.")
    print(f" Panel: {url}")
    print(f" Datos y reportes: {cfg.carpeta}")
    print(" No cierres esta ventana mientras uses el programa (Ctrl+C para salir).")
    print("=" * 64, flush=True)
    if cfg.get("abrir_navegador") and not args.sin_navegador and not os.environ.get("OPENSTD_SIN_NAVEGADOR"):
        abrir_navegador(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nCerrando...")
    finally:
        parar.set()
        srv.shutdown()
    return 0


def abrir_navegador(url: str) -> None:
    if ES_WINDOWS:
        try:
            # explorer.exe abre el navegador como el usuario normal aunque este programa corra como administrador
            subprocess.Popen(["explorer.exe", url])
            return
        except OSError:
            pass
    elif os.environ.get("SUDO_USER") and os.geteuid() == 0:
        try:
            # con sudo, el navegador se abre como el usuario que lo pidió (los navegadores no corren como root)
            subprocess.Popen(["sudo", "-u", os.environ["SUDO_USER"], "xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)


def cmd_info(args) -> int:
    from . import plataforma
    e = plataforma.actual().equipo()
    print(json.dumps(e, ensure_ascii=False, indent=2))
    return 0


def cmd_diagnosticar(args) -> int:
    from .config import actual
    from .diagnostico import Diagnostico

    cfg = actual()
    diag = Diagnostico(cfg)
    print("Leyendo los datos del equipo...", flush=True)
    diag.cargar_equipo(esperar=True)
    if not diag.equipo:
        print(diag.equipo_error or "No se pudieron leer los datos del equipo")
        return 2
    pedidas = {p.strip() for p in args.pruebas.split(",") if p.strip()}
    desconocidas = pedidas - {"sistema", "bateria", "discos", "cpu", "ram", "gpu"}
    if desconocidas:
        print("Pruebas desconocidas: " + ", ".join(sorted(desconocidas)))
        return 2
    pr = {"sistema": "sistema" in pedidas, "bateria": "bateria" in pedidas,
          "discos": [d["id"] for d in diag.equipo["discos"] if not d["es_usb"]] if "discos" in pedidas else [],
          "disco_modo": "completo" if args.disco_completo else "rapido",
          "cpu": args.minutos_cpu if "cpu" in pedidas else 0, "ram": args.minutos_ram if "ram" in pedidas else 0,
          "gpu": args.minutos_gpu if "gpu" in pedidas else 0}
    err = diag.iniciar({"orden": args.orden, "cliente": args.cliente, "enviar": not args.sin_correo, "pruebas": pr})
    if err:
        print(err)
        return 2
    ultimo = ""
    try:
        while diag.estado.get("corriendo"):
            fase = diag.progreso.fase
            if fase != ultimo:
                print(f"  {fase}", flush=True)
                ultimo = fase
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCancelando...")
        diag.cancelar()
    diag.esperar()
    est = diag.estado
    if args.json:
        print(json.dumps({k: est.get(k) for k in ("id", "veredicto", "conclusion", "archivo", "correo", "resultados", "error")},
                         ensure_ascii=False, indent=2))
    else:
        from .resultado import Resultado
        print()
        for r in est.get("resultados") or []:
            print(Resultado.de_dict(r).a_texto())
            print()
        print(f"RESULTADO: {est.get('veredicto')}")
        print(est.get("conclusion") or "")
        if est.get("archivo"):
            print("PDF: " + os.path.join(cfg.carpeta_reportes, est["archivo"]))
        if est.get("correo"):
            print("Correo: " + est["correo"]["texto"])
        if est.get("error"):
            print("Error: " + est["error"])
    return 1 if est.get("veredicto") == "CON FALLAS" else 0


def cmd_probar_correo(args) -> int:
    from . import correo
    from .config import actual
    try:
        correo.probar(actual())
    except correo.ErrorCorreo as e:
        print(f"No salió: {e}")
        return 1
    print("Listo: el correo de prueba salió.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _consola_utf8()
    ap = argparse.ArgumentParser(prog="openstd", description=f"{NOMBRE}: diagnóstico de hardware y sistema para Windows y Linux")
    ap.add_argument("--version", action="version", version=f"{NOMBRE} {__version__}")
    ap.add_argument("--puerto", type=int, help="puerto del panel (por defecto 8765)")
    ap.add_argument("--sin-navegador", action="store_true", help="no abrir el navegador")
    sub = ap.add_subparsers(dest="comando")
    sub.add_parser("panel", help="abre el panel (lo mismo que sin comando)")
    sub.add_parser("info", help="muestra los datos del equipo")
    d = sub.add_parser("diagnosticar", help="corre el diagnóstico en la consola")
    d.add_argument("--pruebas", default="sistema,bateria,discos,cpu,ram",
                   help="lista separada por comas: sistema,bateria,discos,cpu,ram,gpu (por defecto todas menos gpu)")
    d.add_argument("--minutos-cpu", type=float, default=5)
    d.add_argument("--minutos-ram", type=float, default=5)
    d.add_argument("--minutos-gpu", type=float, default=5)
    d.add_argument("--disco-completo", action="store_true", help="leer los discos enteros (tarda horas)")
    d.add_argument("--orden", default="")
    d.add_argument("--cliente", default="")
    d.add_argument("--sin-correo", action="store_true", help="no mandar el reporte por correo")
    d.add_argument("--json", action="store_true", help="salida en JSON")
    sub.add_parser("probar-correo", help="manda un correo de prueba")
    args = ap.parse_args(argv)
    if args.comando in (None, "panel"):
        return cmd_panel(args)
    return {"info": cmd_info, "diagnosticar": cmd_diagnosticar, "probar-correo": cmd_probar_correo}[args.comando](args)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
