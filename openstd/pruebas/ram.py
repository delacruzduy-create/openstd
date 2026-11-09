"""Prueba de memoria RAM: ocupa la memoria libre con patrones (aleatorios que cambian de lugar en cada
pasada y fijos 00/FF/55/AA) y la vuelve a leer. Cualquier diferencia es FALLA.

Desde el sistema en uso no se puede probar el 100 % de la RAM (una parte la ocupa el propio sistema):
el reporte dice qué porcentaje se cubrió. Para probarla entera, usar MemTest86+ desde un pendrive.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue
import random
import time

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from ..util import duracion, plural, tam
from . import Contexto, cierre_por_hardware

MB = 1024 * 1024
BLOQUE_MB = 16          # cada bloque de memoria pedida al sistema


def patron(refs: list[bytes], k: int, pase: int) -> bytes:
    if pase % 3 == 2:
        return refs[16 + (pase // 3) % 4]          # 00, FF, 55, AA
    return refs[(k * 7 + pase) % 16]               # aleatorios, cambian de lugar en cada pasada


def comparar(leido, esperado, nombre: str) -> str | None:
    """Compara 1 MB con lo que se escribió. Devuelve la descripción del error o None."""
    if leido == esperado:
        return None
    pos = next(i for i in range(len(leido)) if leido[i] != esperado[i])
    distintos = sum(1 for a, b in zip(leido, esperado, strict=True) if a != b)
    bits = bin(leido[pos] ^ esperado[pos]).count("1")
    return (f"bloque {nombre}, byte {pos}: esperado {esperado[pos]:02X}, leído {leido[pos]:02X} "
            f"({plural(bits, 'bit')} distinto{'s' if bits > 1 else ''}; {plural(distintos, 'byte')} mal en ese MB)")


def _trabajador(mb: int, fin: float, semilla: int, cola, idx: int, corromper: bool = False) -> None:
    try:
        _patrones(mb, fin, semilla, cola, idx, corromper)
    except MemoryError:
        cola.put(("sinmem", idx))
    except BaseException as e:  # noqa: BLE001
        cola.put(("err", idx, f"{type(e).__name__}: {e}"[:200]))


def _patrones(mb: int, fin: float, semilla: int, cola, idx: int, corromper: bool) -> None:
    bloques = []
    try:
        for _ in range(max(1, mb // BLOQUE_MB)):
            bloques.append(bytearray(BLOQUE_MB * MB))
    except MemoryError:
        del bloques[-2:]                            # se deja aire para el resto del proceso
    if not bloques:
        raise MemoryError
    rnd = random.Random(semilla)
    refs = [rnd.randbytes(MB) for _ in range(16)] + [bytes([p]) * MB for p in (0x00, 0xFF, 0x55, 0xAA)]
    total = len(bloques) * BLOQUE_MB * MB
    cola.put(("mem", idx, total))
    pase, procesado, errores = 0, 0, []
    while len(errores) < 30:
        for b, blk in enumerate(bloques):
            vista = memoryview(blk)
            for j in range(BLOQUE_MB):
                vista[j * MB:(j + 1) * MB] = patron(refs, b * BLOQUE_MB + j, pase)
        if corromper and pase == 0:
            bloques[0][12345] ^= 0x10              # para probar que la verificación detecta un bit dado vuelta
        for b, blk in enumerate(bloques):
            vista = memoryview(blk)
            for j in range(BLOQUE_MB):
                e = comparar(vista[j * MB:(j + 1) * MB], patron(refs, b * BLOQUE_MB + j, pase), f"{idx}.{b}.{j}")
                if e:
                    errores.append(e)
        procesado += 2 * total
        pase += 1
        cola.put(("p", idx, pase, procesado))
        if time.time() >= fin:
            break
    cola.put(("fin", idx, pase, procesado, errores, total))


def ejecutar(ctx: Contexto, segundos: float, mb_max: int | None = None, _corromper: bool = False) -> Resultado:
    r = Resultado("RAM", "Memoria RAM")
    p = ctx.progreso
    try:
        import psutil
    except ImportError:
        return r.poner(NO_EVALUADO, "Falta la librería psutil")
    seg = max(1.0, float(segundos))
    vm = psutil.virtual_memory()
    instalada = (ctx.equipo.get("ram") or {}).get("instalada") or vm.total
    reserva = max(int(ctx.config.get("ram_reserva_mb")) * MB, int(0.08 * vm.total))
    disponible = vm.available - reserva
    if mb_max:
        disponible = min(disponible, mb_max * MB)
    if disponible < 64 * MB:
        r.lineas.append(f"Instalada: {tam(instalada, False)} · libre: {tam(vm.available, False)}")
        return r.poner(NO_EVALUADO, "No hay memoria libre suficiente para la prueba (cerrar programas y repetir)")
    trab = max(1, min(4, (os.cpu_count() or 2) // 2, int(disponible // (256 * MB)) or 1))
    mb_cada = max(BLOQUE_MB, int(disponible / MB / trab) // BLOQUE_MB * BLOQUE_MB)
    p.set_fase(f"RAM: preparando {tam(mb_cada * trab * MB, False)} en {plural(trab, 'proceso')}...")
    mpctx = mp.get_context("spawn")
    cola = mpctx.Queue()
    t0 = time.time()
    fin = t0 + seg
    procesos = [mpctx.Process(target=_trabajador, args=(mb_cada, fin, 99 + i, cola, i, _corromper and i == 0), daemon=True)
                for i in range(trab)]
    try:
        for proc in procesos:
            proc.start()
    except OSError as e:
        return r.poner(NO_EVALUADO, f"No se pudieron abrir los procesos de prueba: {e}")
    memoria, pases, procesado, errores, terminados, no_arrancaron, del_programa = {}, {}, {}, [], set(), set(), []
    cancelado = False
    while len(terminados) < trab:
        try:
            m = cola.get(timeout=0.5)
            if m[0] == "sinmem":
                del_programa.append("el sistema se quedó sin memoria para la prueba (cerrar programas y repetir)")
                terminados.add(m[1])
            elif m[0] == "err":
                del_programa.append(m[2])
                terminados.add(m[1])
            elif m[0] == "mem":
                memoria[m[1]] = m[2]
                p.set_fase(f"RAM: probando {tam(sum(memoria.values()), False)} durante {duracion(seg)}")
            elif m[0] == "p":
                pases[m[1]], procesado[m[1]] = m[2], m[3]
            else:
                pases[m[1]], procesado[m[1]] = m[2], m[3]
                errores += m[4]
                memoria[m[1]] = m[5]
                terminados.add(m[1])
        except queue.Empty:
            pass
        p.set_porcentaje(100 * min(1, (time.time() - t0) / seg))
        if p.cancelado:
            cancelado = True
            break
        if time.time() > fin + 180:
            break
        for i, proc in enumerate(procesos):
            if i in terminados or proc.is_alive() or proc.exitcode in (0, None):
                continue
            terminados.add(i)
            hw = cierre_por_hardware(proc.exitcode)
            if hw and i in memoria:
                errores.append(f"el proceso de prueba {i} se cayó en plena prueba ({hw})")
            elif i in memoria:
                del_programa.append(f"un proceso de prueba se cerró (código {proc.exitcode}): ¿falta de memoria o antivirus?")
            else:
                no_arrancaron.add(i)
    for proc in procesos:
        if proc.is_alive():
            proc.terminate()
    for proc in procesos:
        proc.join(timeout=5)
    corrido = min(time.time() - t0, seg)
    probado = sum(memoria.values())
    cobertura = 100.0 * probado / instalada if instalada else 0
    r.lineas.append(f"RAM instalada: {tam(instalada, False)} · visible para el sistema: {tam(vm.total, False)}")
    r.lineas.append(f"Memoria probada: {tam(probado, False)} = {cobertura:.0f} % de la RAM instalada "
                    "(el resto lo usan el sistema y los programas abiertos)")
    r.lineas.append(f"Duración: {duracion(corrido)} · pasadas completas: {min(pases.values()) if pases else 0} "
                    "(patrones aleatorios que cambian de lugar + 00/FF/55/AA)")
    r.lineas.append(f"Datos escritos y verificados: {sum(procesado.values()) / 1024 ** 3:.1f} GB · ".replace(".", ",")
                    + plural(len(errores), "error", "errores"))
    r.lineas += ["  " + e for e in errores[:6]]
    if cobertura < 50 and not mb_max:
        r.advertencias.append("Se probó menos de la mitad de la RAM: para probarla entera usar MemTest86+ desde un pendrive")
    if any(e.startswith("bloque") for e in errores):
        r.poner(FALLA, "Se encontraron errores de memoria")
        r.acciones.append("Probar los módulos de a uno para encontrar el dañado y reemplazarlo")
    elif errores:
        r.poner(FALLA, "Un proceso de prueba se cayó en plena prueba (error del procesador o la memoria)")
        r.acciones.append("Probar los módulos de a uno y revisar que no haya overclock/XMP inestable")
    elif cancelado:
        r.poner(NO_EVALUADO, "Cancelado")
    elif del_programa:
        r.poner(NO_EVALUADO, "Se interrumpió por un problema del programa, no de la RAM: " + del_programa[0])
    elif no_arrancaron and not memoria:
        r.poner(NO_EVALUADO, "Los procesos de prueba no pudieron arrancar (¿antivirus?)")
    elif len(terminados) < trab:
        r.poner(NO_EVALUADO, "La prueba no terminó a tiempo (el sistema estuvo usando el disco como memoria)")
    else:
        r.poner(PASA, f"Sin errores en {duracion(corrido)} sobre el {cobertura:.0f} % de la RAM instalada")
    return r
