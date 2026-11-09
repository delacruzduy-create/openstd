"""Chequeo del sistema operativo: lo que hace que "la PC falla" aunque las pruebas de hardware den bien.

Mira los registros desde la fecha de referencia (por defecto, los últimos N días):
  - pantallazos azules (Windows) o cuelgues del kernel (Linux);
  - apagados sin cerrar el sistema (cortes de luz, fuente, temperatura, botón);
  - errores de hardware informados por el procesador (WHEA / MCE / EDAC / PCIe);
  - errores de disco y del sistema de archivos;
  - cuelgues del driver de video;
  - dispositivos con el driver con error, falta de memoria y espacio libre.

Fecha de referencia: después de arreglar algo (cambiar la RAM, reinstalar un driver), con "Contar desde hoy"
los errores viejos quedan como historial y ya no dan FALLA.
"""
from __future__ import annotations

import time

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from ..util import plural, tam
from . import Contexto

DIA = 86400

# Códigos de pantallazo azul más comunes: (nombre, causa probable)
BUGCHECKS = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL", "un driver"), 0x19: ("BAD_POOL_HEADER", "un driver o la RAM"),
    0x1A: ("MEMORY_MANAGEMENT", "la RAM o un driver"), 0x1E: ("KMODE_EXCEPTION_NOT_HANDLED", "un driver"),
    0x24: ("NTFS_FILE_SYSTEM", "el disco"), 0x3B: ("SYSTEM_SERVICE_EXCEPTION", "un driver"),
    0x4E: ("PFN_LIST_CORRUPT", "la RAM"), 0x50: ("PAGE_FAULT_IN_NONPAGED_AREA", "la RAM o un driver"),
    0x7A: ("KERNEL_DATA_INPAGE_ERROR", "el disco o su cable"), 0x77: ("KERNEL_STACK_INPAGE_ERROR", "el disco"),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "un driver"), 0x7F: ("UNEXPECTED_KERNEL_MODE_TRAP", "el hardware o un driver"),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", "un driver (suspensión o energía)"), 0xA0: ("INTERNAL_POWER_ERROR", "la administración de energía"),
    0xC2: ("BAD_POOL_CALLER", "un driver"), 0xD1: ("DRIVER_IRQL_NOT_LESS_OR_EQUAL", "un driver (muchas veces el de red o Wi-Fi)"),
    0xEF: ("CRITICAL_PROCESS_DIED", "archivos de Windows dañados o el disco"), 0xF4: ("CRITICAL_OBJECT_TERMINATION", "el disco"),
    0xFC: ("ATTEMPTED_EXECUTE_OF_NOEXECUTE_MEMORY", "un driver o la RAM"), 0x101: ("CLOCK_WATCHDOG_TIMEOUT", "el procesador o el BIOS"),
    0x109: ("CRITICAL_STRUCTURE_CORRUPTION", "la RAM o un driver"), 0x116: ("VIDEO_TDR_FAILURE", "la placa de video o su driver"),
    0x117: ("VIDEO_TDR_TIMEOUT_DETECTED", "la placa de video o su driver"), 0x119: ("VIDEO_SCHEDULER_INTERNAL_ERROR", "el driver de video"),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "el hardware: procesador, RAM, fuente o temperatura"),
    0x133: ("DPC_WATCHDOG_VIOLATION", "un driver o el firmware del SSD"), 0x139: ("KERNEL_SECURITY_CHECK_FAILURE", "un driver o la RAM"),
    0x13A: ("KERNEL_MODE_HEAP_CORRUPTION", "un driver"), 0x154: ("UNEXPECTED_STORE_EXCEPTION", "el disco"),
    0x1A8: ("SYSTEM_SCAN_AT_RAISED_IRQL_CAUGHT_IMPROPER_DRIVER_UNLOAD", "un driver"),
}


def _fecha(t: float) -> str:
    return time.strftime("%d/%m/%Y %H:%M", time.localtime(t))


def evaluar(ev: dict, volumenes: list[dict], desde: float, motivo_ref: str = "", ahora: float | None = None) -> Resultado:
    """Evalúa los eventos ya normalizados (ver plataforma/base.py). Función pura: no lee nada del sistema."""
    r = Resultado("SISTEMA", "Sistema operativo")
    ahora = ahora or time.time()
    dias = max(1, round((ahora - desde) / DIA))
    if not ev.get("disponible"):
        r.lineas.append(ev.get("motivo") or "No se pudieron leer los registros del sistema")
        _espacio(r, volumenes)
        return r.poner(NO_EVALUADO, ev.get("motivo") or "No se pudieron leer los registros del sistema")
    r.lineas.append(f"Período revisado: desde el {_fecha(desde)} ({plural(dias, 'día')})"
                    + (f" · contando desde: {motivo_ref}" if motivo_ref else "") + (f" · fuente: {ev['fuente']}" if ev.get("fuente") else ""))
    fallas = []

    pant = ev.get("pantallazos") or []
    r.lineas.append(f"Pantallazos azules / cuelgues del sistema: {len(pant)}")
    if pant:
        causas = {}
        for x in pant:
            nombre, causa = BUGCHECKS.get(x.get("codigo"), (None, None))
            clave = f"0x{x['codigo']:X} {nombre}" if nombre else (f"0x{x['codigo']:X}" if x.get("codigo") else x.get("texto") or "cuelgue")
            causas.setdefault(clave, [0, causa, 0])
            causas[clave][0] += 1
            causas[clave][2] = max(causas[clave][2], x.get("t") or 0)
        for clave, (n, causa, ult) in sorted(causas.items(), key=lambda kv: -kv[1][0]):
            r.lineas.append(f"  {clave}: {plural(n, 'vez', 'veces')} (último {_fecha(ult)})" + (f" · causa probable: {causa}" if causa else ""))
        fallas.append(f"{plural(len(pant), 'pantallazo azul', 'pantallazos azules')} o cuelgues del sistema")
        r.acciones.append("Pantallazos: actualizar o reinstalar el driver indicado, probar la RAM y el disco; "
                          "cuando esté resuelto, tocar 'Contar desde hoy'")

    apag = ev.get("apagados") or []
    r.lineas.append(f"Apagados sin cerrar el sistema: {len(apag)}")
    if len(apag) >= 3:
        fallas.append(f"{len(apag)} apagados inesperados (fuente, temperatura, corte de luz o botón)")
        r.acciones.append("Apagados repetidos: revisar la fuente, la temperatura y la instalación eléctrica")
    elif apag:
        r.advertencias.append(f"{plural(len(apag), 'apagado inesperado', 'apagados inesperados')}: puede ser un corte de luz o "
                              "que lo apagaron con el botón; si se repite, revisar fuente y temperatura")

    hw = ev.get("hardware") or []
    graves = [x for x in hw if x.get("grave")]
    r.lineas.append(f"Errores de hardware informados por el procesador: {len(hw)}" + (f" ({len(graves)} graves)" if graves else ""))
    r.lineas += ["  " + x["texto"] for x in graves[:3]]
    if graves:
        fallas.append(f"{plural(len(graves), 'error grave', 'errores graves')} de hardware (procesador, RAM, placa o fuente)")
        r.acciones.append("Errores de hardware: probar RAM y procesador, sacar overclock/XMP y revisar la fuente")
    elif hw:
        r.advertencias.append(f"{plural(len(hw), 'error corregido', 'errores corregidos')} de hardware: el equipo los "
                              "pudo corregir, pero si aumentan hay un componente fallando")

    disco = ev.get("disco") or []
    dgraves = [x for x in disco if x.get("grave")]
    r.lineas.append(f"Errores de disco y del sistema de archivos: {len(disco)}" + (f" ({len(dgraves)} graves)" if dgraves else ""))
    r.lineas += ["  " + x["texto"] for x in (dgraves or disco)[:3]]
    if dgraves:
        fallas.append(f"{plural(len(dgraves), 'error grave', 'errores graves')} de lectura del disco")
        r.acciones.append("Errores de disco: hacer copia de los datos y revisar el disco y su cable")
    elif len(disco) >= 5:
        r.advertencias.append(f"{len(disco)} errores leves de disco (reintentos o reinicios del controlador): revisar cable y disco")
    elif disco:
        r.advertencias.append(f"{plural(len(disco), 'error leve', 'errores leves')} de disco")

    video = ev.get("video") or []
    r.lineas.append(f"Cuelgues del driver de video: {len(video)}")
    if video:
        r.advertencias.append(f"El driver de video se colgó {plural(len(video), 'vez', 'veces')}: probar la placa de video y "
                              "reinstalar su driver")

    mem = ev.get("memoria") or []
    if mem:
        r.advertencias.append(f"El sistema se quedó sin memoria {plural(len(mem), 'vez', 'veces')}: poca RAM para el uso que se le da")

    drv = ev.get("drivers") or []
    if drv:
        r.lineas.append(f"Dispositivos con el driver con error: {len(drv)}")
        r.lineas += [f"  {d['nombre']} (código {d.get('codigo')})" for d in drv[:6]]
        r.advertencias.append(f"{plural(len(drv), 'dispositivo', 'dispositivos')} sin driver o con el driver con error")
        r.acciones.append("Instalar los drivers que faltan (del fabricante o de Windows Update)")

    _espacio(r, volumenes)
    if fallas:
        r.poner(FALLA, "; ".join(fallas))
    else:
        r.poner(PASA, f"Sin pantallazos, apagados repetidos ni errores graves en {plural(dias, 'día')}")
    return r


def _espacio(r: Resultado, volumenes: list[dict]) -> None:
    for v in volumenes or []:
        if not v.get("total"):
            continue
        pct = 100 * v["libre"] / v["total"]
        r.lineas.append(f"Espacio libre en {v['nombre']}: {tam(v['libre'])} de {tam(v['total'])} ({pct:.0f} %)")
        if v.get("sistema") and (pct < 10 or v["libre"] < 10e9):
            r.advertencias.append(f"Queda poco espacio en {v['nombre']} ({pct:.0f} %): el sistema se pone lento y puede fallar al actualizarse")
            r.acciones.append(f"Liberar espacio en {v['nombre']}")


def ejecutar(ctx: Contexto) -> Resultado:
    ctx.progreso.set_fase("Sistema: leyendo los registros de errores...")
    ref = ctx.config.referencia(ctx.plat.clave_equipo(ctx.equipo)) or {}
    dias = ctx.config.get("dias_eventos")
    desde = max(time.time() - dias * DIA, ref.get("desde") or 0)
    ev = ctx.plat.eventos(desde)
    ctx.progreso.set_porcentaje(80)
    try:
        vols = ctx.plat.volumenes()
    except Exception:  # noqa: BLE001
        vols = []
    return evaluar(ev, vols, desde, ref.get("motivo", "") if ref.get("desde", 0) >= desde else "")
