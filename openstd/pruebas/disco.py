"""Prueba de disco. SOLO LECTURA: no escribe nada en el disco del cliente.

1) Salud SMART con smartctl (SATA, NVMe y la mayoría de los USB). En Windows, si no está smartctl, se usa
   lo que informa el propio Windows (estado de salud y contadores de confiabilidad).
2) Lectura de la superficie en bloques de 128 KB, midiendo cuánto tarda cada bloque y clasificándolo
   como hace Victoria: <5, <20, <50, <200, <600, >600 ms o ilegible.
   Modo rápido: 256 zonas de 16 MB repartidas por todo el disco. Modo completo: el disco entero.
"""
from __future__ import annotations

import json
import time

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from ..util import correr, duracion, plural, tam
from . import Contexto

BLOQUE = 128 * 1024
ZONAS_RAPIDO = 256
BLOQUES_ZONA = 128               # 16 MB por zona
LIMITES_MS = (5, 20, 50, 200, 600)
NOMBRES_CLASE = ["", "<5 ms", "<20 ms", "<50 ms", "<200 ms", "<600 ms", ">600 ms", "ilegibles"]
CLAVES_SMART = ("smart_status", "ata_smart_attributes", "nvme_smart_health_information_log")


# ============================================================================ SMART
def _num(d, *claves):
    for k in claves:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d if isinstance(d, (int, float)) and not isinstance(d, bool) else None


def leer_smart(exe: str | None, disco: dict) -> tuple[dict | None, int, str]:
    """Corre smartctl. Devuelve (json, código de salida, motivo si no se pudo)."""
    if not exe:
        return None, 0, "falta smartctl (paquete smartmontools)"
    intentos = [[]]
    if disco.get("es_usb"):
        intentos.append(["-d", "sat"])
    if disco.get("tipo") == "NVMe":
        intentos.append(["-d", "nvme"])
    ultimo = "smartctl no respondió"
    for extra in intentos:
        cod, out = correr([exe, "-a", "-j"] + extra + [disco["smart_ruta"]], 120)
        try:
            j = json.loads(out[out.index("{"):])
        except ValueError:
            continue
        if any(k in j for k in CLAVES_SMART):
            return j, cod, ""
        msgs = (j.get("smartctl") or {}).get("messages") or []
        ultimo = "el disco o su adaptador no informa SMART" + (f" ({msgs[0].get('string', '')})" if msgs else "")
    return None, 0, ultimo


def evaluar_smart(j: dict, cod: int = 0) -> tuple[list[str], list[str], list[str]]:
    """Devuelve (líneas, advertencias, fallas)."""
    L, A, F = [], [], []
    protocolo = str((j.get("device") or {}).get("protocol") or "")
    rot = _num(j, "rotation_rate")
    es_nvme = protocolo.lower() == "nvme" or "nvme_smart_health_information_log" in j
    es_ssd = es_nvme or rot == 0
    tipo = "SSD NVMe" if es_nvme else "SSD SATA" if rot == 0 else f"HDD {rot} rpm" if rot else protocolo or "?"
    L.append(f"Modelo: {j.get('model_name') or '-'} · serie: {j.get('serial_number') or '-'} · firmware: {j.get('firmware_version') or '-'} · {tipo}")
    horas, ciclos, temp = _num(j, "power_on_time", "hours"), _num(j, "power_cycle_count"), _num(j, "temperature", "current")
    L.append(f"Horas de uso: {horas if horas is not None else '-'}" + (f" ({horas / 24:.0f} días)" if horas else "")
             + f" · encendidos: {ciclos if ciclos is not None else '-'}"
             + f" · temperatura: {str(temp) + ' °C' if temp is not None else '-'}")
    paso = (j.get("smart_status") or {}).get("passed") if isinstance(j.get("smart_status"), dict) else None
    L.append("Estado SMART general: " + ("no informado" if paso is None else "BIEN (PASSED)" if paso else "MAL (FAILED)"))
    if paso is False:
        F.append("El propio disco informa que está fallando (SMART FAILED)")

    vida = None
    for a in (j.get("ata_smart_attributes") or {}).get("table") or []:
        aid, nombre = a.get("id"), a.get("name", "")
        raw = _num(a, "raw", "value") or 0
        valor, umbral = a.get("value"), a.get("thresh")
        if a.get("when_failed") == "now":
            F.append(f"Atributo {aid} ({nombre}) debajo del umbral del fabricante (valor {valor}, umbral {umbral})")
        elif a.get("when_failed") == "past":
            A.append(f"El atributo {aid} ({nombre}) estuvo debajo del umbral en el pasado")
        if aid == 5:
            L.append(f"Sectores reasignados (5): {raw}")
            if raw > 0:
                A.append(f"El disco ya reemplazó {plural(raw, 'sector dañado', 'sectores dañados')} (atributo 5)")
        elif aid == 197:
            L.append(f"Sectores pendientes (197): {raw}")
            if raw > 0:
                F.append(f"{plural(raw, 'sector pendiente', 'sectores pendientes')}: ilegibles, esperando ser reemplazados (atributo 197)")
        elif aid == 198:
            L.append(f"Incorregibles (198): {raw}")
            if raw > 0:
                F.append(f"{plural(raw, 'sector incorregible', 'sectores incorregibles')} (atributo 198)")
        elif aid == 187 and raw > 0:
            A.append(f"{plural(raw, 'error incorregible informado', 'errores incorregibles informados')} (atributo 187)")
        elif aid == 199:
            L.append(f"Errores de comunicación (199): {raw}")
            if raw > 0:
                A.append("Errores de comunicación con el disco (atributo 199): revisar el cable SATA y el conector")
        elif aid == 10 and raw > 0:
            A.append("Reintentos de arranque del motor (atributo 10): posible problema mecánico o de alimentación")
        if es_ssd and vida is None and any(p in nombre.lower() for p in ("wear", "life")) and isinstance(valor, int) and 0 < valor <= 100:
            vida = (valor, nombre)
    if vida:
        L.append(f"Vida útil restante estimada: {vida[0]} % (indicador del fabricante: {vida[1]})")
        if vida[0] <= 10:
            A.append(f"El SSD está cerca del fin de su vida útil ({vida[0]} % restante)")

    nv = j.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        critico = nv.get("critical_warning") or 0
        reserva, umbral_res = nv.get("available_spare"), nv.get("available_spare_threshold")
        usado, err_medio = nv.get("percentage_used"), nv.get("media_errors")
        L.append(f"Desgaste: {usado if usado is not None else '-'} % de la vida nominal · reserva: {reserva if reserva is not None else '-'} % "
                 f"(umbral {umbral_res if umbral_res is not None else '-'} %) · errores de integridad: {err_medio if err_medio is not None else '-'}")
        if nv.get("data_units_written"):
            L.append(f"Datos escritos: {nv['data_units_written'] * 512000 / 1e12:.1f} TB".replace(".", ","))
        if critico:
            F.append(f"Advertencia crítica NVMe activa (0x{critico:02X})")
        if reserva is not None and umbral_res is not None and reserva < umbral_res:
            F.append("La reserva de bloques está debajo del umbral del fabricante")
        if err_medio:
            F.append(f"{plural(err_medio, 'error', 'errores')} de integridad de datos (media errors)")
        if usado is not None and usado >= 90:
            A.append(f"Desgaste del {usado} % de la vida nominal del SSD")

    if cod and cod > 0:
        if cod & 8 and paso is not False:
            F.append("smartctl informa que el disco está fallando")
        if cod & 128:
            A.append("Hay autotests fallidos en el historial del disco")
    if temp is not None and ((not es_ssd and temp >= 55) or (es_ssd and temp >= 75)):
        A.append(f"Temperatura alta del disco ({temp} °C)")
    return L, A, F


def evaluar_salud_so(s: dict | None) -> tuple[list[str], list[str], list[str]]:
    """Sin smartctl en Windows: estado de salud y contadores que informa el propio Windows."""
    L, A, F = [], [], []
    if not s:
        return L, A, F
    salud = str(s.get("salud") or "")
    L.append(f"Salud según el sistema: {salud or '-'} · estado: {s.get('estado') or '-'}")
    if salud.lower() == "unhealthy":
        F.append("Windows informa que el disco está fallando")
    elif salud.lower() == "warning":
        A.append("Windows marca el disco con advertencia (desgaste o reserva baja)")
    c = s.get("rel")
    if isinstance(c, dict):
        horas = c.get("horas") if c.get("horas") is not None else "-"
        temp = f"{c['temp']} °C" if c.get("temp") else "-"
        desgaste = f"{c['desgaste']} %" if c.get("desgaste") is not None else "-"
        L.append(f"Horas de uso: {horas} · temperatura: {temp} · desgaste: {desgaste}")
        inc = (c.get("lect_inc") or 0) + (c.get("escr_inc") or 0)
        if inc:
            F.append(f"{plural(inc, 'error', 'errores')} de lectura/escritura sin corregir")
        if (c.get("desgaste") or 0) >= 90:
            A.append(f"Desgaste del {c['desgaste']} % de la vida útil")
    return L, A, F


# ============================================================================ superficie
def clase(ms: float) -> int:
    for i, lim in enumerate(LIMITES_MS):
        if ms < lim:
            return i + 1
    return 6


def escanear(lector, tam_bytes: int, sector: int, completo: bool, progreso, reloj=time.perf_counter) -> dict:
    """Lee el disco con el lector dado. Devuelve conteos por clase, ilegibles, curva de velocidad, etc."""
    est = {"conteo": [0] * 8, "ilegibles": [], "curva": [], "leidos": 0, "bytes": 0, "detenido": None, "error": None,
           "segundos": 0.0, "completo": False}
    total = tam_bytes // BLOQUE
    if total < 1:
        est["error"] = "el disco es demasiado chico"
        return est
    if completo or total <= ZONAS_RAPIDO * BLOQUES_ZONA:
        zonas, largo = 1, total
        est["completo"] = True
    else:
        zonas, largo = ZONAS_RAPIDO, BLOQUES_ZONA
    planificados = zonas * largo
    tramo = largo if zonas > 1 else max(128, total // 500)        # bloques por punto de la curva
    buf = bytearray(BLOQUE)
    seguidos, t_ini, ultimo_aviso = 0, time.time(), 0.0
    try:
        for z in range(zonas):
            inicio = 0 if zonas == 1 else int(z * (total - largo) / (zonas - 1))
            b_tramo, ms_tramo, n_tramo, ini_tramo = 0, 0.0, 0, inicio
            for k in range(largo):
                if progreso.cancelado:
                    est["detenido"] = "cancelado"
                    return est
                bloque = inicio + k
                off = bloque * BLOQUE
                t0 = reloj()
                ok, err = True, ""
                try:
                    if lector.leer(off, buf) != BLOQUE:
                        ok, err = False, "lectura incompleta"
                except OSError as e:
                    ok, err = False, e.strerror or str(e)
                ms = (reloj() - t0) * 1000
                salto = zonas > 1 and k == 0          # el primer bloque de cada zona incluye el salto del cabezal
                c = clase(ms) if ok else 7
                if not salto or not ok:
                    est["conteo"][c] += 1
                est["leidos"] += 1
                if ok:
                    seguidos = 0
                    est["bytes"] += BLOQUE
                    if not salto:
                        b_tramo += BLOQUE
                        ms_tramo += ms
                else:
                    seguidos += 1
                    if len(est["ilegibles"]) < 200:
                        est["ilegibles"].append(f"bloque ilegible en el sector {off // sector:,} ({err})".replace(",", "."))
                        progreso.log(est["ilegibles"][-1])
                    if seguidos >= 50:
                        est["detenido"] = "50 bloques seguidos ilegibles: se detuvo para no forzar más el disco"
                        return est
                    if est["conteo"][7] >= 500:
                        est["detenido"] = "más de 500 bloques ilegibles: se detuvo para no forzar más el disco"
                        return est
                n_tramo += 1
                if n_tramo >= tramo or k == largo - 1:
                    if b_tramo and ms_tramo:
                        est["curva"].append(((ini_tramo + n_tramo / 2) / total, b_tramo / 1e6 / (ms_tramo / 1000)))
                    b_tramo, ms_tramo, n_tramo, ini_tramo = 0, 0.0, 0, bloque + 1
                ahora = time.time()
                if ahora - ultimo_aviso > 1:
                    ultimo_aviso = ahora
                    hechos = est["leidos"]
                    progreso.set_porcentaje(100.0 * hechos / planificados)
                    resta = (ahora - t_ini) * (planificados - hechos) / max(1, hechos)
                    vel = est["curva"][-1][1] if est["curva"] else 0
                    progreso.fase = f"Superficie: {100.0 * hechos / planificados:.1f} % · {vel:.0f} MB/s · faltan ≈ {duracion(resta)}"
    finally:
        est["segundos"] = time.time() - t_ini
    return est


def evaluar_superficie(e: dict) -> tuple[list[str], list[str], list[str]]:
    L, A, F = [], [], []
    c = e["conteo"]
    if e["error"]:
        L.append("No se pudo leer la superficie: " + e["error"])
    if e["leidos"]:
        L.append("Superficie: " + ("lectura completa del disco" if e["completo"] else "lectura rápida (256 zonas de 16 MB repartidas)")
                 + f" · {tam(e['bytes'])} en {duracion(e['segundos'])}" + (" · interrumpida" if e["detenido"] else ""))
        L.append("Tiempos por bloque: " + " · ".join(f"{NOMBRES_CLASE[i]}: {c[i]}" for i in range(1, 8)))
        if e["curva"]:
            vs = [v for _, v in e["curva"]]
            L.append(f"Velocidad de lectura: promedio {sum(vs) / len(vs):.0f} MB/s · mínima {min(vs):.0f} · máxima {max(vs):.0f} MB/s")
        L += ["  " + x for x in e["ilegibles"][:8]]
        if len(e["ilegibles"]) > 8:
            L.append(f"  ... y {len(e['ilegibles']) - 8} bloques ilegibles más")
    if e["detenido"] and e["detenido"] != "cancelado":
        L.append("Lectura detenida: " + e["detenido"])
    if c[7]:
        F.append(f"{plural(c[7], 'bloque ilegible', 'bloques ilegibles')} en la superficie")
    if c[6] >= 3:
        F.append(f"{c[6]} bloques tardaron más de 600 ms (superficie dañada)")
    elif c[6]:
        A.append(f"{plural(c[6], 'bloque tardó', 'bloques tardaron')} más de 600 ms: zona lenta, conviene repetir la lectura")
    if c[5] >= 10:
        A.append(f"{c[5]} bloques entre 200 y 600 ms: la superficie puede estar degradándose")
    return L, A, F


def _advertencia_velocidad(disco: dict, curva: list) -> str | None:
    if not curva:
        return None
    prom = sum(v for _, v in curva) / len(curva)
    if disco.get("tipo") == "HDD" and prom < 50:
        return f"Lectura lenta para un disco mecánico ({prom:.0f} MB/s)"
    if disco.get("tipo") == "NVMe" and prom < 300:
        return f"Lectura lenta para un SSD NVMe ({prom:.0f} MB/s)"
    if disco.get("tipo") == "SSD" and prom < 150 and not disco.get("es_usb"):
        return f"Lectura lenta para un SSD ({prom:.0f} MB/s): puerto SATA lento, cable o SSD degradado"
    return None


# ============================================================================ prueba completa
def ejecutar(ctx: Contexto, disco: dict, completo: bool | None = None) -> Resultado:
    p = ctx.progreso
    nombre = disco.get("id")
    r = Resultado(f"DISCO_{nombre}", f"Disco {nombre}: {disco.get('modelo')}")
    r.lineas.append(f"{disco.get('modelo')} · {tam(disco.get('tam') or 0)} · {disco.get('tipo')}"
                    + (f" · {disco['bus']}" if disco.get("bus") else "") + (" · tiene el sistema" if disco.get("es_sistema") else ""))
    if completo is None:
        completo = ctx.config.get("disco_modo") == "completo"

    p.set_fase(f"Disco {nombre}: leyendo la salud (SMART)...")
    j, cod, motivo = leer_smart(ctx.plat.smartctl(ctx.config.carpetas_herramientas), disco)
    if j:
        L, A, F = evaluar_smart(j, cod)
        hay_salud = True
    elif disco.get("salud_so"):
        L, A, F = evaluar_salud_so(disco["salud_so"])
        L.insert(0, f"SMART completo: no disponible ({motivo}); se usa lo que informa el sistema")
        hay_salud = True
    else:
        L, A, F = [f"SMART: no disponible ({motivo})"], [], []
        hay_salud = False
    r.lineas += L
    r.advertencias += A
    fallas = list(F)
    if p.cancelado:
        return r.poner(NO_EVALUADO, "Cancelado")

    p.set_fase(f"Disco {nombre}: lectura {'completa' if completo else 'rápida'} de la superficie (solo lectura)...")
    try:
        lector = ctx.plat.abrir_disco(disco)
    except PermissionError:
        lector, error = None, "acceso denegado: hay que abrir el programa como administrador (Windows) o con sudo (Linux)"
    except OSError as e:
        lector, error = None, f"no se pudo abrir el disco: {e.strerror or e}"
    if lector is None:
        e = {"conteo": [0] * 8, "ilegibles": [], "curva": [], "leidos": 0, "bytes": 0, "detenido": None, "error": error,
             "segundos": 0, "completo": False}
    else:
        try:
            e = escanear(lector, int(disco.get("tam") or 0), int(disco.get("sector") or 512), completo, p)
        finally:
            lector.cerrar()
    L, A, F = evaluar_superficie(e)
    r.lineas += L
    r.advertencias += A
    fallas += F
    if e["curva"]:
        r.grafico = {"tipo": "velocidad_disco", "puntos": [(round(x, 4), round(v, 1)) for x, v in e["curva"]]}
        lento = _advertencia_velocidad(disco, e["curva"])
        if lento:
            r.advertencias.append(lento)

    if fallas:
        r.poner(FALLA, "; ".join(fallas))
        r.acciones.append(f"Disco {nombre}: hacer una copia de los datos cuanto antes y reemplazarlo")
    elif e["detenido"] == "cancelado":
        r.poner(NO_EVALUADO, "Cancelado")
    elif e["error"] and not hay_salud:
        r.poner(NO_EVALUADO, "No se pudo leer ni la salud ni la superficie: " + e["error"])
    elif e["error"]:
        r.poner(NO_EVALUADO, "La salud está bien, pero no se pudo leer la superficie: " + e["error"])
    elif not hay_salud:
        r.poner(PASA, "Superficie sin fallas (sin datos SMART para confirmar la salud)")
    else:
        r.poner(PASA, "Salud y superficie sin fallas" + ("" if e["completo"] else " (lectura rápida por muestreo)"))
    return r
