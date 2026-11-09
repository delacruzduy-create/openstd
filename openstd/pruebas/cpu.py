"""Prueba de procesador: todos los hilos al 100 % con cálculos cuyo resultado se verifica.

Cada hilo calcula una y otra vez SHA-256, compresión, aritmética de enteros grandes y coma flotante,
y compara con el resultado de referencia. Un procesador inestable (temperatura, tensión, overclock)
da resultados distintos o se cuelga: eso es FALLA. Si los procesos de prueba no pueden arrancar
(por ejemplo, un antivirus), es NO EVALUADO.
"""
from __future__ import annotations

import math
import multiprocessing as mp
import os
import queue
import random
import time

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from ..util import duracion, plural
from . import Contexto, cierre_por_hardware


def _calculo_flotante() -> float:
    x = 0.0
    for i in range(1, 4000):
        x += math.sin(i * 0.001) * math.sqrt(i) / (1.0 + math.log(i))
    return x


def _trabajador(fin: float, cola, idx: int, fallar: bool = False) -> None:
    """Corre en un proceso aparte, uno por hilo. Un error del propio programa se informa aparte
    para no confundirlo con una falla del procesador."""
    try:
        _calcular(fin, cola, idx, fallar)
    except BaseException as e:  # noqa: BLE001
        cola.put(("err", idx, f"{type(e).__name__}: {e}"[:200]))


def _calcular(fin: float, cola, idx: int, fallar: bool) -> None:
    import hashlib
    import zlib
    rnd = random.Random(1234 + idx)
    alfabeto = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,;:-_()"
    datos = bytes(rnd.choice(alfabeto) for _ in range(1 << 18))
    ref_sha = hashlib.sha256(datos).digest()
    ref_z = zlib.compress(datos, 6)
    modulo = (1 << 2203) - 1
    base = rnd.getrandbits(2000) | 1
    exponente = (1 << 2048) + 12345 + idx
    ref_pow = pow(base, exponente, modulo)
    ref_f = _calculo_flotante() + (1.0 if fallar else 0.0)   # fallar: para probar que se detectan los errores
    ciclos, errores = 0, []
    while time.time() < fin and len(errores) < 20:
        if hashlib.sha256(datos).digest() != ref_sha:
            errores.append("hash SHA-256 distinto")
        z = zlib.compress(datos, 6)
        if z != ref_z or zlib.decompress(z) != datos:
            errores.append("compresión distinta")
        if pow(base, exponente, modulo) != ref_pow:
            errores.append("aritmética de enteros grandes distinta")
        if _calculo_flotante() != ref_f:
            errores.append("cálculo de coma flotante distinto")
        ciclos += 1
        if ciclos % 5 == 0:
            cola.put(("p", idx, ciclos))
    cola.put(("fin", idx, ciclos, errores))


def ejecutar(ctx: Contexto, segundos: float, hilos: int | None = None, _fallar: bool = False) -> Resultado:
    r = Resultado("CPU", "Procesador")
    p = ctx.progreso
    hilos = hilos or os.cpu_count() or 1
    seg = max(1.0, float(segundos))
    nombre = (ctx.equipo.get("cpu") or {}).get("nombre") or "?"
    p.set_fase(f"Procesador: {hilos} hilos al 100 % con verificación de resultados durante {duracion(seg)}")
    marca = ctx.muestreador.marca()
    mpctx = mp.get_context("spawn")
    cola = mpctx.Queue()
    t0 = time.time()
    fin = t0 + seg
    procesos = [mpctx.Process(target=_trabajador, args=(fin, cola, i, _fallar), daemon=True) for i in range(hilos)]
    try:
        for proc in procesos:
            proc.start()
    except OSError as e:
        r.poner(NO_EVALUADO, f"No se pudieron abrir los procesos de prueba: {e}")
        return r
    ciclos, errores, terminados, no_arrancaron, del_programa = {}, {}, set(), set(), []
    cancelado = False
    while len(terminados) < hilos:
        try:
            m = cola.get(timeout=0.5)
            if m[0] == "p":
                ciclos[m[1]] = m[2]
            elif m[0] == "err":
                del_programa.append(m[2])
                terminados.add(m[1])
            else:
                ciclos[m[1]] = m[2]
                errores[m[1]] = m[3]
                terminados.add(m[1])
        except queue.Empty:
            pass
        p.set_porcentaje(100 * min(1, (time.time() - t0) / seg))
        if p.cancelado:
            cancelado = True
            break
        if time.time() > fin + 90:                 # algún proceso no contestó
            break
        for i, proc in enumerate(procesos):
            if i in terminados or proc.is_alive() or proc.exitcode in (0, None):
                continue
            terminados.add(i)
            hw = cierre_por_hardware(proc.exitcode)
            if hw and ciclos.get(i) is not None:
                errores[i] = [f"un proceso de cálculo se cayó en plena carga ({hw})"]
            elif ciclos.get(i) is not None:
                del_programa.append(f"un proceso de cálculo se cerró (código {proc.exitcode}): ¿lo cerró el antivirus?")
            else:
                no_arrancaron.add(i)
    for proc in procesos:
        if proc.is_alive():
            proc.terminate()
    for proc in procesos:
        proc.join(timeout=5)
    corrido = min(time.time() - t0, seg)
    est = ctx.muestreador.estadisticas(marca)
    total_err = [e for lst in errores.values() for e in lst]

    r.lineas.append(f"Procesador: {nombre}")
    r.lineas.append(f"Hilos en carga: {hilos} · duración: {duracion(corrido)} de {duracion(seg)}")
    r.lineas.append("Carga: cálculos verificados en todos los hilos (SHA-256, compresión, enteros grandes y coma flotante)")
    r.lineas.append(f"Ciclos completados: {sum(ciclos.values()):,}".replace(",", ".") + f" · {plural(len(total_err), 'error', 'errores')}")
    r.lineas += ["  " + e for e in total_err[:5]]
    if est["uso_cpu"]:
        r.lineas.append(f"Uso medido del procesador: {est['uso_cpu']['prom']:.0f} %")
    alerta = ctx.config.get("temp_cpu_alerta")
    if est["cpu_temp"]:
        r.lineas.append(f"Temperatura: promedio {est['cpu_temp']['prom']:.0f} °C · máxima {est['cpu_temp']['max']:.0f} °C"
                        + (f" ({est['fuente_temp']})" if est["fuente_temp"] else ""))
        if est["cpu_temp"]["max"] >= alerta:
            r.advertencias.append(f"Llegó a {est['cpu_temp']['max']:.0f} °C: revisar pasta térmica, disipador y ventiladores")
            r.acciones.append("Limpiar el disipador y cambiar la pasta térmica del procesador")
    else:
        r.lineas.append("Temperatura: el sistema no la informa en este equipo")
    mhz = est["mhz"]
    if len(mhz) >= 20:
        v = max(3, len(mhz) // 5)
        inicio, final = sum(mhz[2:2 + v]) / v, sum(mhz[-v:]) / v
        r.lineas.append(f"Frecuencia bajo carga: al inicio {inicio:.0f} MHz · al final {final:.0f} MHz")
        if inicio and (1 - final / inicio) * 100 > 25:
            r.advertencias.append(f"La frecuencia bajó {(1 - final / inicio) * 100:.0f} % durante la carga: "
                                  "refrigeración insuficiente o plan de energía en ahorro")

    if total_err:
        r.poner(FALLA, "Errores de cálculo bajo carga: procesador, memoria, fuente o refrigeración inestables")
        r.acciones.append("Sacar overclock/XMP, revisar temperaturas y fuente, y repetir la prueba")
    elif cancelado:
        r.poner(NO_EVALUADO, "Cancelado")
    elif no_arrancaron:
        r.poner(NO_EVALUADO, f"{len(no_arrancaron)} de {hilos} procesos de prueba no pudieron arrancar (¿antivirus?)")
    elif del_programa:
        r.poner(NO_EVALUADO, "Se interrumpió por un problema del programa, no del procesador: " + del_programa[0])
    elif len(terminados) < hilos:
        r.poner(FALLA, "Un proceso de cálculo dejó de responder durante la carga (cuelgue)")
        r.acciones.append("Revisar temperaturas y fuente, y repetir la prueba")
    else:
        r.poner(PASA, "Sin errores de cálculo ni cuelgues durante la carga")
    return r
