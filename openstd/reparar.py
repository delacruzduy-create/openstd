"""Reparar Windows (versión Pro): punto de restauración, DISM, SFC, CHKDSK, Windows Update, red y arranque.

Cada reparación devuelve un Resultado como las pruebas, así el reporte, el historial y el correo son los mismos:
- PASA: estaba bien o quedó reparado.
- FALLA: encontró un problema y no lo pudo reparar.
- NO EVALUADO: no se pudo hacer (sin permisos, sin internet, se canceló).
En `grafico` va {"tipo": "reparacion", "reparo": bool, "reinicio": bool} para armar la conclusión.

Los textos de SFC y DISM cambian con el idioma de Windows: se buscan en español, inglés y portugués y, si no
aparece ninguno, se lee el registro CBS.log, que siempre está en inglés.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import unicodedata

from .pruebas import Contexto
from .resultado import FALLA, NO_EVALUADO, PASA, Resultado
from .util import SIN_VENTANA

# clave, título, minutos estimados, marcado por defecto
REPARACIONES = [
    ("punto", "Punto de restauración", 1, True),
    ("dism", "Imagen de Windows (DISM)", 15, True),
    ("sfc", "Archivos del sistema (SFC)", 15, True),
    ("chkdsk", "Sistema de archivos del disco (CHKDSK)", 3, True),
    ("wu", "Windows Update", 1, True),
    ("red", "Red (Winsock y TCP/IP)", 1, False),
    ("arranque", "Arranque de Windows (BCD)", 1, False),
]
TITULOS = {c: t for c, t, _, _ in REPARACIONES}

# códigos de salida de DISM (se comparan sin signo)
DISM_SIN_ORIGEN = {0x800F081F, 0x800F0906, 0x800F0907, 0x800F0950}
DISM_NO_SOPORTADO = {87, 0x80070057}


def _normal(texto: str) -> str:
    """minúsculas y sin acentos, para buscar frases sin importar el idioma ni la codificación."""
    t = unicodedata.normalize("NFKD", texto or "")
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).lower().split())


def _hay(texto: str, frases: tuple[str, ...]) -> bool:
    n = _normal(texto)
    return any(f in n for f in frases)


def _info(r: Resultado, reparo: bool = False, reinicio: bool = False) -> Resultado:
    r.grafico = {"tipo": "reparacion", "reparo": reparo, "reinicio": reinicio}
    return r


# ============================================================================ correr programas mostrando el avance
def _decodificar(datos: bytes) -> str:
    """SFC escribe en UTF-16 (con bytes 0); los demás programas de consola, en la página de códigos OEM."""
    if datos.count(b"\x00") > len(datos) // 4:
        inicio = 1 if datos[:1] == b"\x00" else 0       # por si quedó desfasado un byte
        return datos[inicio:].decode("utf-16-le", "replace").lstrip("\ufeff")
    return datos.decode("oem" if os.name == "nt" else "utf-8", "replace")


def _ultimo_porcentaje(texto: str) -> float | None:
    numeros = re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s?%", texto)
    for n in reversed(numeros):
        v = float(n.replace(",", "."))
        if 0 <= v <= 100:
            return v
    return None


def correr_vivo(cmd: list[str], ctx: Contexto, timeout: float = 3600) -> tuple[int, str]:
    """Corre un programa, va pasando el porcentaje que imprime a la barra del panel y devuelve (código sin signo, salida).
    Si el técnico cancela o se pasa el tiempo, se corta. Códigos propios: -1 no se pudo abrir, -2 tiempo agotado, -3 cancelado."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=SIN_VENTANA)
    except OSError as e:
        return -1, str(e)
    buf = bytearray()
    corte = {"motivo": 0}

    def vigilar():
        fin = time.time() + timeout
        while p.poll() is None:
            if ctx.progreso.cancelado or time.time() > fin:
                corte["motivo"] = -3 if ctx.progreso.cancelado else -2
                try:
                    p.kill()
                except OSError:
                    pass
                return
            time.sleep(0.5)
    threading.Thread(target=vigilar, daemon=True, name="vigilar-reparacion").start()
    ultimo = 0.0
    while True:
        trozo = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        if not trozo:
            break
        buf += trozo
        if time.time() - ultimo > 1:
            ultimo = time.time()
            cola = bytes(buf[-(1200 if len(buf) % 2 == 0 else 1201):])
            v = _ultimo_porcentaje(_decodificar(cola))
            if v is not None:
                ctx.progreso.set_porcentaje(v)
    p.wait()
    if corte["motivo"]:
        return corte["motivo"], _decodificar(bytes(buf))
    return p.returncode & 0xFFFFFFFF, _decodificar(bytes(buf))


def _ps(script: str, timeout: float = 300):
    from .plataforma.windows import extraer_json, ps
    return extraer_json(ps(script, timeout)[1])


def _cortado(r: Resultado, cod: int) -> Resultado | None:
    if cod == -3:
        return _info(r.poner(NO_EVALUADO, "Se canceló antes de terminar"))
    if cod == -2:
        return _info(r.poner(NO_EVALUADO, "No terminó a tiempo y se cortó"))
    if cod == -1:
        return _info(r.poner(NO_EVALUADO, "No se pudo abrir el programa de Windows"))
    return None


# ============================================================================ punto de restauración
SCRIPT_PUNTO = r"""
$antes = @(Get-ComputerRestorePoint).Count
$err = ''
try { Checkpoint-Computer -Description 'OpenSTD Test - antes de reparar' -RestorePointType MODIFY_SETTINGS -ErrorAction Stop -WarningVariable w 3>$null }
catch { $err = $_.Exception.Message }
$pts = @(Get-ComputerRestorePoint)
$u = $pts | Select-Object -Last 1
$ult = $null
if ($u) { $ult = E ($u.ConvertToDateTime($u.CreationTime)) }
@{antes=$antes; despues=$pts.Count; error=$err; aviso=($w -join ' '); ultimo=$ult} | ConvertTo-Json -Compress
"""


def punto(ctx: Contexto) -> Resultado:
    r = Resultado("REP_PUNTO", TITULOS["punto"])
    ctx.progreso.set_fase("Creando un punto de restauración (por si hay que volver atrás)...")
    d = _ps(SCRIPT_PUNTO, 300)
    if not isinstance(d, dict):
        return _info(r.poner(NO_EVALUADO, "No se pudo consultar la restauración del sistema"))
    if (d.get("despues") or 0) > (d.get("antes") or 0):
        r.lineas.append("Si algo sale mal, se puede volver a este punto desde 'Restaurar sistema' (rstrui).")
        return _info(r.poner(PASA, "Punto de restauración creado"))
    ultimo = d.get("ultimo")
    if ultimo and time.time() - ultimo < 86400:
        r.lineas.append("Windows crea un solo punto por día: se usa el último, del "
                        + time.strftime("%d/%m/%Y %H:%M", time.localtime(ultimo)) + ".")
        return _info(r.poner(PASA, "Ya había un punto de restauración de hoy"))
    texto = d.get("error") or d.get("aviso") or ""
    if texto:
        r.lineas.append("Windows respondió: " + str(texto)[:200])
    r.acciones.append("Activar la protección del sistema en el disco C: (Propiedades del sistema > Protección del sistema) "
                      "para poder volver atrás si una reparación sale mal.")
    return _info(r.poner(NO_EVALUADO, "No se pudo crear: la protección del sistema parece estar desactivada"))


# ============================================================================ DISM
DISM_SANO = ("no component store corruption detected", "no se detectaron danos en el almacen de componentes",
             "no se ha detectado ningun dano en el almacen de componentes", "nenhum dano ao repositorio de componentes foi detectado",
             "nao foi detectado nenhum dano no repositorio de componentes")
DISM_REPARABLE = ("component store is repairable", "almacen de componentes se puede reparar", "almacen de componentes es reparable",
                  "repositorio de componentes pode ser reparado", "repositorio de componentes e reparavel")


def dism(ctx: Contexto) -> Resultado:
    r = Resultado("REP_DISM", TITULOS["dism"])
    ctx.progreso.set_fase("DISM: revisando la imagen de Windows...")
    cod, salida = correr_vivo(["dism.exe", "/Online", "/Cleanup-Image", "/CheckHealth"], ctx, 600)
    if (x := _cortado(r, cod)) is not None:
        return x
    if cod in DISM_NO_SOPORTADO:
        return _info(r.poner(NO_EVALUADO, "Esta versión de Windows no tiene la reparación con DISM (hace falta Windows 8 o más nuevo)"))
    danada = _hay(salida, DISM_REPARABLE)
    sana = _hay(salida, DISM_SANO)
    if danada:
        r.lineas.append("La revisión rápida encontró daños en el almacén de componentes de Windows.")
    ctx.progreso.set_fase("DISM: reparando la imagen de Windows (descarga lo que falta de Windows Update, puede tardar 10 a 30 minutos)...")
    ctx.progreso.set_porcentaje(0)
    cod, salida = correr_vivo(["dism.exe", "/Online", "/Cleanup-Image", "/RestoreHealth"], ctx, 5400)
    if (x := _cortado(r, cod)) is not None:
        return x
    for linea in salida.splitlines():
        if linea.strip().lower().startswith(("error", "the restore", "la operaci", "a opera")):
            r.lineas.append("DISM: " + linea.strip()[:160])
    if cod == 0:
        if danada:
            return _info(r.poner(PASA, "La imagen de Windows estaba dañada y quedó reparada"), reparo=True)
        r.lineas.append("No hacía falta reparar nada." if sana else "DISM terminó sin errores.")
        return _info(r.poner(PASA, "La imagen de Windows está sana"))
    if cod in DISM_SIN_ORIGEN:
        r.acciones.append("DISM no encontró los archivos para reparar: conectar la PC a internet y repetir, o montar un ISO del mismo "
                          "Windows y correr: DISM /Online /Cleanup-Image /RestoreHealth /Source:X:\\sources\\install.wim")
        motivo = "No encontró los archivos para reparar (sin internet o Windows Update roto)"
    else:
        motivo = f"DISM terminó con el error 0x{cod:08X}"
    if danada:
        return _info(r.poner(FALLA, "La imagen de Windows está dañada y no se pudo reparar: " + motivo[0].lower() + motivo[1:]))
    return _info(r.poner(NO_EVALUADO, motivo))


# ============================================================================ SFC
SFC_SANO = ("did not find any integrity violations", "no encontro ninguna infraccion de integridad",
            "no encontro ninguna violacion de integridad", "nao encontrou nenhuma violacao de integridade")
SFC_REPARO = ("successfully repaired them", "los reparo correctamente", "los reparo satisfactoriamente", "e os reparou com exito",
              "e os reparou com sucesso")
SFC_NO_PUDO = ("unable to fix some", "no pudo reparar algunos", "no pudo corregir algunos", "nao conseguiu reparar alguns",
               "nao pode reparar alguns", "nao conseguiu corrigir alguns")
SFC_PENDIENTE = ("system repair pending", "reparacion del sistema pendiente", "reparo do sistema pendente")
SFC_NO_CORRIO = ("could not perform the requested operation", "no pudo realizar la operacion solicitada",
                 "nao pode executar a operacao solicitada", "could not start the repair service", "no pudo iniciar el servicio de reparacion")
ARCHIVO = re.compile(r"([\w.\-]+\.(?:dll|exe|sys|mui|cat|manifest|ttf|winmd|xml|cpl|ocx|drv|inf|mof))", re.I)


def cbs_sfc(desde: float, ruta: str | None = None) -> dict:
    """Lee CBS.log (siempre en inglés) desde que arrancó SFC: archivos reparados y los que no se pudieron reparar."""
    ruta = ruta or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Logs", "CBS", "CBS.log")
    r = {"leido": False, "reparados": [], "sin_reparar": []}
    try:
        with open(ruta, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8 * 1024 * 1024))
            texto = f.read().decode("utf-8", "replace")
    except OSError:
        return r
    r["leido"] = True
    limite = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(desde - 5))
    for linea in texto.splitlines():
        if "[SR]" not in linea or linea[:19] < limite:
            continue
        m = ARCHIVO.search(linea.split("[SR]", 1)[1])
        nombre = m.group(1) if m else ""
        if "Cannot repair member file" in linea or "Could not reproject corrupted file" in linea:
            if nombre and nombre not in r["sin_reparar"]:
                r["sin_reparar"].append(nombre)
        elif "Repairing corrupted file" in linea or "Repaired file" in linea:
            if nombre and nombre not in r["reparados"]:
                r["reparados"].append(nombre)
    r["sin_reparar"] = [x for x in r["sin_reparar"] if x not in r["reparados"]]
    return r


def sfc(ctx: Contexto) -> Resultado:
    r = Resultado("REP_SFC", TITULOS["sfc"])
    ctx.progreso.set_fase("SFC: revisando y reparando los archivos del sistema (10 a 30 minutos)...")
    desde = time.time()
    cod, salida = correr_vivo(["sfc.exe", "/scannow"], ctx, 5400)
    if (x := _cortado(r, cod)) is not None:
        return x
    cbs = cbs_sfc(desde)
    if cbs["reparados"]:
        r.lineas.append("Archivos reparados: " + ", ".join(cbs["reparados"][:12]) + (" y más" if len(cbs["reparados"]) > 12 else ""))
    if cbs["sin_reparar"]:
        r.lineas.append("Sin reparar: " + ", ".join(cbs["sin_reparar"][:12]) + (" y más" if len(cbs["sin_reparar"]) > 12 else ""))
    if _hay(salida, SFC_PENDIENTE):
        r.acciones.append("Windows tiene una reparación pendiente: reiniciar la PC y volver a correr la reparación.")
        return _info(r.poner(NO_EVALUADO, "Hay una reparación de Windows pendiente: hace falta reiniciar primero"), reinicio=True)
    no_pudo = _hay(salida, SFC_NO_PUDO) or (cbs["sin_reparar"] and not _hay(salida, SFC_REPARO + SFC_SANO))
    if no_pudo:
        r.acciones.append("Quedaron archivos del sistema dañados: correr de nuevo la reparación con DISM conectado a internet. "
                          "Si sigue igual, conviene reparar Windows con un ISO (actualización en el lugar) o reinstalar.")
        return _info(r.poner(FALLA, "Encontró archivos del sistema dañados y no pudo reparar algunos"), reparo=bool(cbs["reparados"]))
    if _hay(salida, SFC_REPARO) or cbs["reparados"]:
        return _info(r.poner(PASA, "Encontró archivos del sistema dañados y los reparó"), reparo=True, reinicio=True)
    if _hay(salida, SFC_SANO) or (cod == 0 and cbs["leido"]):
        return _info(r.poner(PASA, "Los archivos del sistema están bien"))
    if _hay(salida, SFC_NO_CORRIO):
        return _info(r.poner(NO_EVALUADO, "SFC no pudo hacer la revisión (probar en modo seguro)"))
    texto = " ".join(salida.split())[-200:]
    if texto:
        r.lineas.append("SFC respondió: " + texto)
    return _info(r.poner(NO_EVALUADO, "No se pudo interpretar el resultado de SFC"))


# ============================================================================ CHKDSK
def chkdsk(ctx: Contexto) -> Resultado:
    r = Resultado("REP_CHKDSK", TITULOS["chkdsk"])
    unidad = os.environ.get("SystemDrive", "C:")
    ctx.progreso.set_fase(f"CHKDSK: revisando el sistema de archivos de {unidad} sin reiniciar...")
    cod, salida = correr_vivo(["chkdsk.exe", unidad, "/scan"], ctx, 3600)
    if (x := _cortado(r, cod)) is not None:
        return x
    if cod == 0:
        return _info(r.poner(PASA, f"El sistema de archivos de {unidad} está bien"))
    if cod in (1, 2):
        return _info(r.poner(PASA, f"Encontró errores en el sistema de archivos de {unidad} y los corrigió"), reparo=True)
    if cod == 3:
        c, s = correr_vivo(["fsutil.exe", "dirty", "set", unidad], ctx, 60)
        if c == 0:
            r.advertencias.append(f"Hay errores en {unidad} que solo se pueden corregir al arrancar: se repararán solos en el próximo "
                                  "reinicio (la PC tarda unos minutos más en arrancar).")
            r.acciones.append("Reiniciar la PC y dejar que termine la revisión del disco al arrancar, sin apagarla.")
            return _info(r.poner(PASA, "Encontró errores: la reparación quedó programada para el próximo reinicio"), reparo=True, reinicio=True)
        r.acciones.append(f"Correr 'chkdsk {unidad} /f' como administrador y aceptar que se haga al reiniciar.")
        return _info(r.poner(FALLA, f"Hay errores en el sistema de archivos de {unidad} que no se pudieron corregir"))
    return _info(r.poner(NO_EVALUADO, f"CHKDSK terminó con el código {cod}"))


# ============================================================================ Windows Update
SCRIPT_WU = r"""
$r = @{renombradas=@(); errores=@(); servicios=@()}
$ts = Get-Date -Format 'yyyyMMddHHmmss'
foreach ($c in @("$env:SystemRoot\SoftwareDistribution", "$env:SystemRoot\System32\catroot2")) {
  if (-not (Test-Path $c)) { continue }
  $hoja = Split-Path $c -Leaf
  $ok = $false; $msj = ''
  for ($i = 0; $i -lt 4 -and -not $ok; $i++) {
    foreach ($s in 'usosvc','wuauserv','bits','cryptsvc','msiserver') { Stop-Service -Name $s -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    try { Rename-Item -Path $c -NewName "$hoja.old_$ts" -ErrorAction Stop; $ok = $true } catch { $msj = $_.Exception.Message }
  }
  if ($ok) { $r.renombradas += "$hoja.old_$ts" } else { $r.errores += "$hoja`: $msj" }
}
foreach ($s in 'cryptsvc','bits','wuauserv') { Start-Service -Name $s -ErrorAction SilentlyContinue }
foreach ($s in 'cryptsvc','bits','wuauserv') { $x = Get-Service $s -ErrorAction SilentlyContinue
  $r.servicios += @{n=$s; e=$(if ($x) { [string]$x.Status } else { 'no existe' })} }
$r | ConvertTo-Json -Compress -Depth 4
"""


def wu(ctx: Contexto) -> Resultado:
    r = Resultado("REP_WU", TITULOS["wu"])
    ctx.progreso.set_fase("Windows Update: deteniendo los servicios y vaciando la caché de actualizaciones...")
    d = _ps(SCRIPT_WU, 300)
    if not isinstance(d, dict):
        return _info(r.poner(NO_EVALUADO, "No se pudo reiniciar Windows Update"))
    ren = [x for x in (d.get("renombradas") or []) if x]
    errores = [x for x in (d.get("errores") or []) if x]
    if ren:
        r.lineas.append("Carpetas viejas guardadas como: " + ", ".join(ren) + " (se pueden borrar cuando Windows Update ande bien).")
    for e in errores:
        r.lineas.append("No se pudo renombrar " + e[:160])
    if errores and not ren:
        return _info(r.poner(NO_EVALUADO, "No se pudo vaciar la caché: algún servicio de Windows Update no se detuvo"))
    r.acciones.append("Abrir Windows Update y buscar actualizaciones: la primera búsqueda tarda más de lo normal.")
    return _info(r.poner(PASA, "Windows Update quedó reiniciado (caché de actualizaciones vacía)"), reparo=True)


# ============================================================================ red
def red(ctx: Contexto) -> Resultado:
    r = Resultado("REP_RED", TITULOS["red"])
    ctx.progreso.set_fase("Red: reiniciando Winsock, TCP/IP y la caché DNS...")
    pasos = [(["netsh.exe", "winsock", "reset"], "Winsock"), (["netsh.exe", "int", "ip", "reset"], "TCP/IP"),
             (["ipconfig.exe", "/flushdns"], "caché DNS")]
    bien, mal = [], []
    for cmd, nombre in pasos:
        cod, _ = correr_vivo(cmd, ctx, 120)
        if (x := _cortado(r, cod)) is not None:
            return x
        (bien if cod == 0 else mal).append(nombre)
    if bien:
        r.lineas.append("Reiniciado: " + ", ".join(bien) + ".")
    if mal:
        r.lineas.append("Con avisos: " + ", ".join(mal) + " (suele pasar con algunas claves protegidas y no impide el arreglo).")
    if "Winsock" not in bien:
        return _info(r.poner(NO_EVALUADO, "No se pudo reiniciar Winsock"))
    r.acciones.append("Reiniciar la PC para que tome la configuración de red nueva. Si usaba IP fija o un proxy, volver a cargarlos.")
    return _info(r.poner(PASA, "La configuración de red quedó reiniciada"), reparo=True, reinicio=True)


# ============================================================================ arranque
def arranque(ctx: Contexto) -> Resultado:
    r = Resultado("REP_ARRANQUE", TITULOS["arranque"])
    raiz = os.environ.get("SystemRoot", r"C:\Windows")
    os.makedirs(ctx.config.carpeta, exist_ok=True)
    copia = os.path.join(ctx.config.carpeta, time.strftime("bcd_copia_%Y%m%d_%H%M%S"))
    ctx.progreso.set_fase("Arranque: guardando una copia de la configuración de arranque...")
    cod, salida = correr_vivo(["bcdedit.exe", "/export", copia], ctx, 120)
    if (x := _cortado(r, cod)) is not None:
        return x
    if cod != 0:
        r.lineas.append("bcdedit: " + " ".join(salida.split())[:160])
        return _info(r.poner(NO_EVALUADO, "No se pudo leer la configuración de arranque: no se tocó nada"))
    r.lineas.append(f"Copia de la configuración anterior: {copia} (se restaura con: bcdedit /import \"{copia}\").")
    ctx.progreso.set_fase("Arranque: rehaciendo los archivos de arranque de Windows...")
    cod, salida = correr_vivo(["bcdboot.exe", raiz], ctx, 300)
    if (x := _cortado(r, cod)) is not None:
        return x
    if cod == 0:
        return _info(r.poner(PASA, "Los archivos de arranque de Windows quedaron rehechos"), reparo=True)
    r.lineas.append("bcdboot: " + " ".join(salida.split())[:160])
    r.acciones.append("Si la PC no arranca, reparar el arranque desde un pendrive de instalación de Windows (Reparar el equipo > "
                      "Solucionar problemas > Reparación de inicio).")
    return _info(r.poner(FALLA, "No se pudieron rehacer los archivos de arranque"))


FUNCIONES = {"punto": punto, "dism": dism, "sfc": sfc, "chkdsk": chkdsk, "wu": wu, "red": red, "arranque": arranque}


def ejecutar(ctx: Contexto, clave: str) -> Resultado:
    return FUNCIONES[clave](ctx)


# ============================================================================ textos para el reporte
def es_reparacion(resultados: list[Resultado]) -> bool:
    return bool(resultados) and all(r.codigo.startswith("REP_") for r in resultados)


def _dato(r: Resultado, k: str) -> bool:
    return bool((r.grafico or {}).get(k))


def veredicto(resultados: list[Resultado]) -> str:
    if not resultados:
        return "SIN PRUEBAS"
    if any(r.veredicto == FALLA for r in resultados):
        return "QUEDARON PROBLEMAS"
    if all(r.veredicto == NO_EVALUADO for r in resultados):
        return "NO SE PUDO REPARAR"
    if any(_dato(r, "reparo") and r.veredicto == PASA for r in resultados):
        return "REPARADO"
    return "SIN PROBLEMAS"


def conclusion(resultados: list[Resultado]) -> str:
    arreglado = [r.titulo for r in resultados if r.veredicto == PASA and _dato(r, "reparo")]
    bien = [r.titulo for r in resultados if r.veredicto == PASA and not _dato(r, "reparo")]
    mal = [r.titulo for r in resultados if r.veredicto == FALLA]
    no_ev = [r.titulo for r in resultados if r.veredicto == NO_EVALUADO]
    partes = []
    if arreglado:
        partes.append(f"Se reparó: {', '.join(arreglado)}.")
    if bien:
        partes.append(f"Estaba bien: {', '.join(bien)}.")
    if mal:
        partes.append(f"No se pudo reparar: {', '.join(mal)} (ver qué hacer).")
    if no_ev:
        partes.append(f"No se pudo hacer: {', '.join(no_ev)}.")
    if any(_dato(r, "reinicio") for r in resultados):
        partes.append("Reiniciá la PC para terminar de aplicar los cambios.")
    return " ".join(partes) or "No se hizo ninguna reparación."
