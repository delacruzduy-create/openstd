"""Prueba de placa de video.

Principal: la prueba propia de OpenSTD Test (openstd/gpu/estres.py), que verifica la memoria de video y los
cálculos de la GPU por cada API que tenga el driver (Vulkan, Direct3D 12, OpenGL, Metal), sin programas externos.

Opcional: una herramienta externa (FurMark, glmark2 o un comando propio) durante N minutos, revisando si el
driver de video se colgó o reinició. Ahí los artefactos en pantalla se verifican a ojo.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from ..util import ES_WINDOWS, duracion, plural
from . import Contexto


def correr_herramienta(op: dict, seg: float, progreso, entorno_extra: dict | None = None) -> tuple[bool, int | None, bool]:
    """Corre la herramienta `seg` segundos. Devuelve (se cerró sola antes de tiempo, código, cancelado)."""
    env = dict(os.environ)
    env.update(op.get("entorno") or {})
    env.update(entorno_extra or {})
    t0 = time.time()
    proc = subprocess.Popen(op["cmd"], cwd=op.get("cwd") or None, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    salio, codigo, cancelado = False, None, False
    try:
        while time.time() - t0 < seg:
            time.sleep(0.5)
            progreso.set_porcentaje(100 * (time.time() - t0) / seg)
            if proc.poll() is not None:
                salio, codigo = True, proc.returncode
                break
            if progreso.cancelado:
                cancelado = True
                break
    finally:
        if proc.poll() is None:
            try:
                if not ES_WINDOWS:
                    proc.send_signal(signal.SIGINT)
                    proc.wait(5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    pass
    return salio, codigo, cancelado


def ejecutar_externa(ctx: Contexto, segundos: float, opcion: dict | None) -> Resultado:
    r = Resultado("GPU", "Placa de video")
    p = ctx.progreso
    for v in ctx.equipo.get("video") or []:
        r.lineas.append(f"Adaptador: {v.get('nombre')}" + (f" · driver {v['driver']}" if v.get("driver") else " · sin driver"))
    prep = ctx.plat.gpu_preparar()
    r.lineas += prep.get("lineas") or []
    if prep.get("problema"):
        r.acciones.append("Instalar el driver de la placa de video y repetir la prueba")
        return r.poner(NO_EVALUADO, prep["problema"])
    if prep.get("aviso"):
        r.advertencias.append(prep["aviso"])
    if not opcion:
        return r.poner(NO_EVALUADO, "No hay herramienta para cargar la GPU: copiá FurMark en la carpeta 'herramientas', "
                                    "instalá glmark2 (Linux) o configurá un comando propio en Ajustes")
    seg = max(5.0, float(segundos))
    p.set_fase(f"Placa de video: {opcion['nombre']} durante {duracion(seg)} (mirá la pantalla por si aparecen manchas o rayas)")
    ctx.plat.preferir_gpu_dedicada(opcion["cmd"][0])
    marca_gpu = ctx.plat.gpu_marca()
    marca = ctx.muestreador.marca()
    t0 = time.time()
    try:
        salio, codigo, cancelado = correr_herramienta(opcion, seg, p, prep.get("entorno"))
    except OSError as e:
        return r.poner(NO_EVALUADO, f"No se pudo abrir {os.path.basename(opcion['cmd'][0])}: {e}")
    corrido = time.time() - t0
    est = ctx.muestreador.estadisticas(marca)
    errores = ctx.plat.gpu_errores(t0, marca_gpu)

    r.lineas.append(f"Carga: {opcion['nombre']} · {duracion(min(corrido, seg))} de {duracion(seg)}")
    if est["gpu_uso"]:
        r.lineas.append(f"Uso de la GPU: promedio {est['gpu_uso']['prom']:.0f} %")
    if est["gpu_temp"]:
        r.lineas.append(f"Temperatura: promedio {est['gpu_temp']['prom']:.0f} °C · máxima {est['gpu_temp']['max']:.0f} °C")
        if est["gpu_temp"]["max"] >= ctx.config.get("temp_gpu_alerta"):
            r.advertencias.append(f"Llegó a {est['gpu_temp']['max']:.0f} °C: limpiar la placa y revisar ventiladores y pasta térmica")
            r.acciones.append("Limpiar la placa de video y cambiar la pasta térmica")
    else:
        r.lineas.append("Temperatura: el driver no la informa")
    r.lineas.append(f"Cuelgues o reinicios del driver de video durante la prueba: {len(errores)}")
    r.lineas += ["  " + e[:160] for e in errores[:5]]
    r.lineas.append("Las manchas, rayas o parpadeos en pantalla se verifican a ojo durante la prueba.")

    if errores:
        r.poner(FALLA, f"El driver de video se colgó o reinició durante la carga ({plural(len(errores), 'vez', 'veces')})")
        r.acciones.append("Reinstalar el driver de video desde cero y repetir; si sigue fallando, la placa tiene un problema")
    elif cancelado:
        r.poner(NO_EVALUADO, "Cancelado")
    elif salio:
        r.poner(NO_EVALUADO, f"La herramienta se cerró a los {duracion(corrido)} (código {codigo}). "
                             "Puede ser un cierre manual, parámetros que no reconoce o una falla del driver: repetir la prueba")
    elif est["gpu_uso"] and est["gpu_uso"]["prom"] < 20:
        r.poner(NO_EVALUADO, "La herramienta casi no usó la GPU (¿quedó en su menú o usó la placa integrada?)")
    else:
        r.poner(PASA, "Completó la carga sin cierres ni cuelgues del driver")
    return r


# ============================================================================ prueba propia (wgpu)
RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOMBRES_API = {"Vulkan": "Vulkan", "D3D12": "DirectX 12", "OpenGL": "OpenGL", "Metal": "Metal", "D3D11": "DirectX 11"}
PALABRAS_CUELGUE = ("lost", "perdid", "hung", "tdr", "removed", "reset", "timeout", "dejó de responder")
SIN_VENTANA = 0x08000000 if ES_WINDOWS else 0


def _worker(args: list[str]) -> list[str]:
    return [sys.executable, "-m", "openstd.gpu.estres"] + args


def listar_adaptadores(timeout: float = 90) -> tuple[list[dict], str]:
    """Placas que ve wgpu, por API. Devuelve (lista, motivo si no se pudo)."""
    try:
        p = subprocess.run(_worker(["--listar"]), capture_output=True, timeout=timeout, cwd=RAIZ, creationflags=SIN_VENTANA)
    except subprocess.TimeoutExpired:
        return [], "el driver de video no respondió al listar las placas"
    except OSError as e:
        return [], str(e)
    for linea in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            d = json.loads(linea)
        except ValueError:
            continue
        if d.get("tipo") == "adaptadores":
            vistos, r = set(), []
            for i, a in enumerate(d["lista"]):
                clave = (a["api"], a["nombre"])
                if clave in vistos:
                    continue
                vistos.add(clave)
                a["indice"] = sum(1 for x in d["lista"][:i] if x["api"] == a["api"])
                r.append(a)
            return r, ""
        if d.get("tipo") == "error":
            return [], d.get("texto", "")
    return [], "no se pudieron listar las placas de video"


def opciones_propias(adaptadores: list[dict]) -> list[dict]:
    hw = [a for a in adaptadores if not a.get("software")]
    if not hw:
        return []
    apis = sorted({a["api"] for a in hw})
    ops = [{"id": "todas", "nombre": "OpenSTD: todas las APIs (" + ", ".join(NOMBRES_API.get(x, x) for x in apis) + ")",
            "propia": True, "adaptadores": hw}]
    if len(apis) > 1:
        for api in apis:
            ops.append({"id": f"api:{api}", "nombre": f"OpenSTD: solo {NOMBRES_API.get(api, api)}", "propia": True,
                        "adaptadores": [a for a in hw if a["api"] == api]})
    return ops


def _correr_worker(args: list[str], seg: float, progreso, base_pct: float, peso_pct: float) -> tuple[list[dict], int | None, bool]:
    """Corre la prueba en otro proceso y junta sus mensajes. Devuelve (mensajes, código de salida, cancelado)."""
    proc = subprocess.Popen(_worker(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=RAIZ, creationflags=SIN_VENTANA)
    msgs: list[dict] = []

    def leer():
        for linea in proc.stdout:
            try:
                msgs.append(json.loads(linea.decode("utf-8", "replace")))
            except ValueError:
                continue
    hilo = threading.Thread(target=leer, daemon=True)
    hilo.start()
    t0, cancelado, colgado = time.time(), False, False
    limite = seg + 120                                  # calibración + margen: si se pasa, la GPU quedó colgada
    while proc.poll() is None:
        time.sleep(0.3)
        progreso.set_porcentaje(base_pct + peso_pct * min(1, (time.time() - t0) / max(seg, 1)))
        if msgs and msgs[-1].get("tipo") == "pase":
            m = msgs[-1]
            progreso.fase = (f"Placa de video: pasada {m['pases']} · {m['bytes_vram'] // 2 ** 20} MB de VRAM · "
                             f"errores: {m['errores_vram'] + m['errores_calculo'] + m['errores_cpu']}")
        if progreso.cancelado:
            cancelado = True
        elif time.time() - t0 > limite:
            colgado = True
        if cancelado or colgado:
            proc.kill()
            break
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        pass
    hilo.join(5)
    if colgado:
        msgs.append({"tipo": "error", "fase": "carga", "texto": "la placa dejó de responder (no terminó a tiempo)"})
    return msgs, proc.returncode, cancelado


def evaluar_propia(titulo_api: str, msgs: list[dict], codigo: int | None, cancelado: bool, errores_driver: list[str]) -> Resultado:
    """Función pura: convierte los mensajes del proceso de prueba en un Resultado."""
    r = Resultado("GPU_" + "".join(c if c.isalnum() else "_" for c in titulo_api.upper()), f"Placa de video ({titulo_api})")
    ini = next((m for m in msgs if m.get("tipo") == "inicio"), None)
    fin = next((m for m in msgs if m.get("tipo") == "fin"), None)
    err = next((m for m in msgs if m.get("tipo") == "error"), None)
    aviso = next((m for m in msgs if m.get("tipo") == "aviso"), None)
    cerrada = bool(fin and fin.get("ventana_cerrada"))
    ult = fin or next((m for m in reversed(msgs) if m.get("tipo") == "pase"), None)
    if ini:
        r.lineas.append(f"Placa: {ini.get('nombre')}" + (f" · driver {ini['driver']}" if ini.get("driver") else ""))
    if ult:
        r.lineas.append(f"Memoria de video verificada: {ult['bytes_vram'] // 2 ** 20} MB · {plural(ult['pases'], 'pasada')} "
                        "(patrón distinto en cada pasada)")
        r.lineas.append(f"Cálculos: {ult['despachos']} lotes en miles de hilos, repetidos y comparados · "
                        f"{ult['muestras_cpu']} resultados recalculados en el procesador")
        r.lineas.append(f"Errores: memoria de video {ult['errores_vram']} · cálculo {ult['errores_calculo']} · "
                        f"contra el procesador {ult['errores_cpu']}")
    if ult and ult.get("fps"):
        r.lineas.append(f"Escena 3D en ventana: {ult['fps']:.0f} FPS promedio a {ult.get('resolucion', '?')} (mientras se verificaba)")
    if aviso:
        r.lineas.append(aviso.get("texto", "")[:200])
    if errores_driver:
        r.lineas.append(f"Cuelgues o reinicios del driver de video: {len(errores_driver)}")
        r.lineas += ["  " + e[:160] for e in errores_driver[:4]]
    if err:
        r.lineas.append(f"Mensaje: {err.get('texto', '')[:200]}")

    if ult and ult["errores_vram"]:
        r.poner(FALLA, f"{plural(ult['errores_vram'], 'error', 'errores')} en la memoria de video (VRAM)")
        r.acciones.append("La memoria de la placa de video tiene fallas: revisar temperatura y, si sigue, reemplazar la placa")
    elif ult and (ult["errores_calculo"] or ult["errores_cpu"]):
        r.poner(FALLA, "La placa dio resultados distintos para el mismo cálculo: GPU inestable")
        r.acciones.append("Sacar overclock de la placa, revisar temperatura y fuente; si sigue, la placa tiene una falla")
    elif errores_driver or (err and err.get("fase") == "carga" and any(p in err.get("texto", "").lower() for p in PALABRAS_CUELGUE)):
        r.poner(FALLA, "El driver de video dejó de responder durante la carga")
        r.acciones.append("Reinstalar el driver de video desde cero y repetir; si sigue fallando, la placa tiene un problema")
    elif cancelado:
        r.poner(NO_EVALUADO, "Cancelado")
    elif cerrada:
        r.poner(NO_EVALUADO, f"Se cerró la ventana de la prueba antes de terminar ({plural(fin['pases'], 'pasada')} sin errores)")
    elif err and err.get("fase") == "inicio":
        r.poner(NO_EVALUADO, f"No se pudo usar {titulo_api}: {err.get('texto', '')[:160]}")
    elif err or not fin:
        r.poner(NO_EVALUADO, "La prueba se interrumpió por un error del programa o del driver"
                + (f" (código {codigo})" if codigo not in (None, 0) else "") + ": conviene repetirla")
    else:
        r.poner(PASA, f"Sin errores en la memoria de video ni en los cálculos ({plural(fin['pases'], 'pasada')})")
    return r


def ejecutar_propia(ctx: Contexto, segundos: float, opcion: dict) -> list[Resultado]:
    p = ctx.progreso
    adaptadores = opcion.get("adaptadores") or []
    mb = int(ctx.config.get("gpu_vram_mb"))
    extra = ["--ventana"] if ctx.config.get("gpu_ventana") else []
    resultados = []
    cada = max(5.0, float(segundos) / max(1, len(adaptadores)))
    varias_placas = len({x["nombre"] for x in adaptadores}) > 1
    for n, a in enumerate(adaptadores):
        if p.cancelado:
            break
        api = NOMBRES_API.get(a["api"], a["api"])
        titulo = f"{api}, {a['nombre']}" if varias_placas else api
        p.set_fase(f"Placa de video por {titulo}: preparando...")
        marca = ctx.muestreador.marca()
        marca_gpu = ctx.plat.gpu_marca()
        t0 = time.time()
        msgs, codigo, cancelado = _correr_worker(["--api", a["api"], "--indice", str(a.get("indice", 0)), "--segundos", str(cada),
                                                  "--mb", str(mb)] + extra, cada, p, 100 * n / len(adaptadores), 100 / len(adaptadores))
        r = evaluar_propia(titulo, msgs, codigo, cancelado, ctx.plat.gpu_errores(t0, marca_gpu))
        est = ctx.muestreador.estadisticas(marca)
        if est["gpu_uso"]:
            r.lineas.append(f"Uso de la GPU: promedio {est['gpu_uso']['prom']:.0f} %")
        if est["gpu_temp"]:
            r.lineas.append(f"Temperatura: promedio {est['gpu_temp']['prom']:.0f} °C · máxima {est['gpu_temp']['max']:.0f} °C")
            if est["gpu_temp"]["max"] >= ctx.config.get("temp_gpu_alerta"):
                r.advertencias.append(f"Llegó a {est['gpu_temp']['max']:.0f} °C: limpiar la placa y revisar ventiladores y pasta térmica")
                r.acciones.append("Limpiar la placa de video y cambiar la pasta térmica")
        resultados.append(r)
    return resultados


def ejecutar(ctx: Contexto, segundos: float, opcion: dict | None) -> list[Resultado]:
    """La prueba propia si la opción es propia; si no, la herramienta externa."""
    if opcion and opcion.get("propia"):
        return ejecutar_propia(ctx, segundos, opcion)
    r = ejecutar_externa(ctx, segundos, opcion)
    if not opcion:
        r.motivo = "No se encontró ninguna placa de video que se pueda probar (¿falta el driver?) ni una herramienta externa"
    return [r]
