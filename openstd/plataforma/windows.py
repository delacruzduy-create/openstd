"""Windows: todo se lee con PowerShell 5.1 (viene con Windows 10 y 11) y con salida JSON.

Nada depende del idioma de Windows: se usan IDs de eventos, nombres de proveedores y clases CIM.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from ..util import SIN_VENTANA, correr
from .base import LectorDisco, Plataforma, buscar, eventos_vacios, numero_hex
from .linux import nvidia_smi

PRELUDIO = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function E($d) { if ($d -is [datetime]) { [int64](($d.ToUniversalTime() - [datetime]'1970-01-01T00:00:00').TotalSeconds) } else { $null } }
"""


def ps(script: str, timeout: float = 90) -> tuple[int, str]:
    """Corre un script de PowerShell y devuelve (código, salida UTF-8)."""
    cod = base64.b64encode((PRELUDIO + script).encode("utf-16-le")).decode()
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", cod],
                           capture_output=True, timeout=timeout, creationflags=SIN_VENTANA)
    except subprocess.TimeoutExpired:
        return -1, "PowerShell no terminó a tiempo"
    except OSError as e:
        return -1, f"no se pudo abrir PowerShell: {e}"
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace").lstrip("\ufeff")


def extraer_json(texto: str):
    texto = (texto or "").strip()
    for i, c in enumerate(texto):
        if c in "{[":
            try:
                return json.loads(texto[i:])
            except ValueError:
                break
    for linea in reversed(texto.splitlines()):
        if linea.strip()[:1] in "{[":
            try:
                return json.loads(linea)
            except ValueError:
                continue
    return None


def ps_json(script: str, timeout: float = 90):
    return extraer_json(ps(script, timeout)[1])


def lista(x) -> list:
    """PowerShell devuelve un objeto suelto cuando hay uno solo."""
    if x is None or x == {}:          # un resultado vacío de PowerShell llega como {}
        return []
    return [e for e in x if e != {}] if isinstance(x, list) else [x]


# ============================================================================ equipo
SCRIPT_EQUIPO = r"""
$cs = Get-CimInstance Win32_ComputerSystem
$bios = Get-CimInstance Win32_BIOS
$bb = Get-CimInstance Win32_BaseBoard
$enc = Get-CimInstance Win32_SystemEnclosure | Select-Object -First 1
$os = Get-CimInstance Win32_OperatingSystem
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$cpu = @(Get-CimInstance Win32_Processor | ForEach-Object {
    @{ n = ([string]$_.Name).Trim(); nucleos = [int]$_.NumberOfCores; hilos = [int]$_.NumberOfLogicalProcessors; mhz = [int]$_.MaxClockSpeed } })
$ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
    @{ cap = [double]$_.Capacity; vel = [int]$_.ConfiguredClockSpeed; fab = ([string]$_.Manufacturer).Trim()
       parte = ([string]$_.PartNumber).Trim(); ranura = [string]$_.DeviceLocator; tipo = [int]$_.SMBIOSMemoryType } })
$pd = @(Get-PhysicalDisk | ForEach-Object {
    $c = $_ | Get-StorageReliabilityCounter
    @{ num = [int]$_.DeviceId; n = ([string]$_.FriendlyName).Trim(); serie = ([string]$_.SerialNumber).Trim(); medio = [string]$_.MediaType
       bus = [string]$_.BusType; tam = [double]$_.Size; salud = [string]$_.HealthStatus; estado = [string](@($_.OperationalStatus) -join ',')
       sector = [int]$_.LogicalSectorSize
       rel = $(if ($c) { @{ temp = $c.Temperature; desgaste = $c.Wear; horas = $c.PowerOnHours
                            lect_inc = $c.ReadErrorsUncorrected; escr_inc = $c.WriteErrorsUncorrected } } else { $null }) } })
$dd = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { @{ num = [int]$_.Index; tam = [double]$_.Size; status = [string]$_.Status } })
$sysdisk = (Get-Partition -DriveLetter ($env:SystemDrive.Substring(0, 1))).DiskNumber
$bat = @(Get-CimInstance Win32_Battery).Count
$vid = @(Get-CimInstance Win32_VideoController | ForEach-Object { @{ n = [string]$_.Name; v = [string]$_.DriverVersion } })
@{ fabricante = ([string]$cs.Manufacturer).Trim(); modelo = ([string]$cs.Model).Trim()
   serie = ([string]$bios.SerialNumber).Trim(); bios = [string]$bios.SMBIOSBIOSVersion; bios_fecha = (E $bios.ReleaseDate)
   placa = (([string]$bb.Manufacturer) + ' ' + ([string]$bb.Product)).Trim(); chasis = @($enc.ChassisTypes | ForEach-Object { [int]$_ })
   cpu = $cpu; ram = $ram; ram_visible = [double]$cs.TotalPhysicalMemory
   discos = $pd; discos_wmi = $dd; disco_sistema = $sysdisk; baterias = $bat; video = $vid
   windows = ([string]$os.Caption + ' ' + [string]$cv.DisplayVersion).Trim(); build = [int]$os.BuildNumber
   nombre_pc = [string]$env:COMPUTERNAME } | ConvertTo-Json -Compress -Depth 6
"""

TIPOS_RAM = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4",
             34: "DDR5", 35: "LPDDR5"}
CHASIS_PORTATIL = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
GENERICOS = ("o.e.m", "default string", "system product name", "system manufacturer", "to be filled",
             "not applicable", "not specified", "none", "123456789", "system serial number")


def _limpio(s) -> str:
    s = str(s or "").strip()
    return "" if not s or any(g in s.lower() for g in GENERICOS) else s


def normalizar_equipo(d: dict) -> dict:
    """Pasa lo que devuelve SCRIPT_EQUIPO al formato común (ver plataforma/base.py)."""
    d = d or {}
    tam_wmi = {str(x.get("num")): x.get("tam") for x in lista(d.get("discos_wmi"))}
    discos = []
    for x in lista(d.get("discos")):
        num = x.get("num")
        bus = str(x.get("bus") or "")
        medio = str(x.get("medio") or "")
        tipo = "NVMe" if bus.upper() == "NVME" else "HDD" if medio == "HDD" else "SSD" if medio == "SSD" else "?"
        tam = min(v for v in (x.get("tam"), tam_wmi.get(str(num))) if v) if (x.get("tam") or tam_wmi.get(str(num))) else 0
        discos.append({"id": str(num), "ruta": rf"\\.\PhysicalDrive{num}", "smart_ruta": f"/dev/pd{num}",
                       "modelo": x.get("n") or "(sin modelo)", "serie": _limpio(x.get("serie")), "tam": int(tam or 0), "tipo": tipo,
                       "bus": bus.upper(), "es_usb": bus.upper() == "USB", "es_sistema": num == d.get("disco_sistema"),
                       "sector": int(x.get("sector") or 512),
                       "salud_so": {"salud": x.get("salud"), "estado": x.get("estado"), "rel": x.get("rel")}})
    discos.sort(key=lambda z: int(z["id"]) if z["id"].isdigit() else 99)
    cpu = (lista(d.get("cpu")) or [{}])[0]
    modulos = [{"ranura": m.get("ranura") or "", "tam": int(m.get("cap") or 0), "tipo": TIPOS_RAM.get(m.get("tipo"), ""),
                "velocidad": f"{m['vel']} MT/s" if m.get("vel") else "", "fabricante": _limpio(m.get("fab")),
                "parte": _limpio(m.get("parte"))} for m in lista(d.get("ram"))]
    chasis = set(lista(d.get("chasis")))
    return {
        "fabricante": _limpio(d.get("fabricante")), "modelo": _limpio(d.get("modelo")), "serie": _limpio(d.get("serie")),
        "placa": _limpio(d.get("placa")), "bios": str(d.get("bios") or "").strip(), "nombre_pc": d.get("nombre_pc") or "",
        "sistema": f"{d.get('windows') or 'Windows'} (compilación {d.get('build')})" if d.get("build") else (d.get("windows") or "Windows"),
        "es_portatil": bool(chasis & CHASIS_PORTATIL) or bool(d.get("baterias")),
        "cpu": {"nombre": cpu.get("n") or "desconocido", "nucleos": cpu.get("nucleos") or 0,
                "hilos": cpu.get("hilos") or os.cpu_count() or 1, "mhz": cpu.get("mhz")},
        "ram": {"instalada": sum(m["tam"] for m in modulos) or int(d.get("ram_visible") or 0),
                "visible": int(d.get("ram_visible") or 0), "modulos": modulos},
        "video": [{"nombre": v.get("n") or "", "driver": v.get("v") or ""} for v in lista(d.get("video"))],
        "discos": discos, "baterias": int(d.get("baterias") or 0),
    }


# ============================================================================ batería
def interpretar_powercfg(texto: str) -> list[dict]:
    """Informe XML de 'powercfg /batteryreport /xml'."""
    try:
        raiz = ET.fromstring(texto) if texto else None
    except ET.ParseError:
        return []
    if raiz is None:
        return []
    r = []
    for i, b in enumerate(e for e in raiz.iter() if e.tag.split("}")[-1] == "Battery"):
        c = {x.tag.split("}")[-1]: (x.text or "").strip() for x in b}

        def num(k, c=c):
            try:
                return int(float(c.get(k) or 0)) or None
            except ValueError:
                return None
        r.append({"nombre": c.get("Id") or f"batería {i + 1}", "fabricante": c.get("Manufacturer", ""), "modelo": c.get("Id", ""),
                  "quimica": c.get("Chemistry", ""), "diseno": num("DesignCapacity"), "actual": num("FullChargeCapacity"),
                  "unidad": "mWh", "ciclos": num("CycleCount"), "estado": "", "carga": None})
    return r


# ============================================================================ eventos
SCRIPT_EVENTOS = r"""
$desde = [datetimeoffset]::FromUnixTimeSeconds(__DESDE__).LocalDateTime
function Ev($f, $max) {
    $f.StartTime = $desde
    try {
        @(Get-WinEvent -FilterHashtable $f -MaxEvents $max -ErrorAction Stop | ForEach-Object {
            @{ t = (E $_.TimeCreated); id = [int]$_.Id; p = [string]$_.ProviderName; l = [int]$_.Level
               a = @($_.Properties | Select-Object -First 4 | ForEach-Object { [string]$_.Value }) } })
    } catch { @() }
}
$discos = 'disk', 'stornvme', 'storahci', 'iaStorA', 'iaStorAC', 'iaStorAVC', 'iaStorV', 'nvme', 'UASPStor', 'atapi', 'mpt3sas'
$video = 'nvlddmkm', 'amdkmdag', 'amdwddmg', 'igfx', 'igfxn'
$o = @{
    bsod = @(Ev @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WER-SystemErrorReporting'; Id = 1001 } 50)
    kp41 = @(Ev @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Kernel-Power'; Id = 41 } 50)
    whea = @(Ev @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WHEA-Logger' } 100)
    disco = @(Ev @{ LogName = 'System'; ProviderName = $discos; Id = 7, 11, 51, 129, 153 } 200)
    ntfs = @(Ev @{ LogName = 'System'; ProviderName = 'Ntfs', 'Microsoft-Windows-Ntfs'; Id = 55, 98, 137, 140; Level = 1, 2 } 100)
    tdr = @(Ev @{ LogName = 'System'; ProviderName = 'Display'; Id = 4101 } 50)
    video = @(Ev @{ LogName = 'System'; ProviderName = $video; Level = 1, 2 } 50)
    memoria = @(Ev @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-Resource-Exhaustion-Detector'; Id = 2004 } 30)
    drivers = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode <> 0' | ForEach-Object {
        @{ n = [string]$_.Name; c = [int]$_.ConfigManagerErrorCode } })
}
$o | ConvertTo-Json -Compress -Depth 5
"""

TEXTO_DISCO = {7: ("bloque dañado en el disco", True), 11: ("error del controlador del disco", False),
               51: ("error al leer o escribir la memoria virtual en el disco", False),
               129: ("el disco dejó de responder y se reinició", False), 153: ("el disco tuvo que reintentar una lectura", False)}
CODIGOS_IGNORADOS = {22, 45}       # 22 = deshabilitado a mano, 45 = dispositivo desconectado


def interpretar_eventos(d: dict | None) -> dict:
    if not isinstance(d, dict):
        return eventos_vacios(False, "No se pudieron leer los registros de Windows")
    ev = eventos_vacios(True, fuente="registro de eventos de Windows")
    for e in lista(d.get("bsod")):
        a = lista(e.get("a"))
        codigo = numero_hex(a[0]) if a else None
        ev["pantallazos"].append({"t": e.get("t") or 0, "codigo": codigo, "texto": "pantallazo azul"})
    for e in lista(d.get("kp41")):
        a = lista(e.get("a"))
        if a and (numero_hex(a[0]) or 0) != 0:
            continue                  # ya figura como pantallazo azul
        ev["apagados"].append({"t": e.get("t") or 0, "texto": "se apagó o reinició sin cerrar Windows"})
    for e in lista(d.get("whea")):
        ev["hardware"].append({"t": e.get("t") or 0, "texto": f"error de hardware WHEA (evento {e.get('id')})",
                               "grave": (e.get("l") or 4) <= 2})
    for e in lista(d.get("disco")):
        texto, grave = TEXTO_DISCO.get(e.get("id"), (f"error de disco (evento {e.get('id')})", False))
        ev["disco"].append({"t": e.get("t") or 0, "texto": f"{texto} ({e.get('p')})", "grave": grave})
    for e in lista(d.get("ntfs")):
        ev["disco"].append({"t": e.get("t") or 0, "texto": "sistema de archivos NTFS dañado (correr chkdsk)", "grave": False})
    for e in lista(d.get("tdr")) + lista(d.get("video")):
        ev["video"].append({"t": e.get("t") or 0, "texto": f"el driver de video dejó de responder ({e.get('p')})"})
    for e in lista(d.get("memoria")):
        ev["memoria"].append({"t": e.get("t") or 0, "texto": "Windows se quedó sin memoria"})
    for x in lista(d.get("drivers")):
        if x.get("c") not in CODIGOS_IGNORADOS:
            ev["drivers"].append({"nombre": x.get("n") or "(sin nombre)", "codigo": x.get("c")})
    return ev


SCRIPT_EVENTOS_GPU = r"""
$desde = [datetimeoffset]::FromUnixTimeSeconds(__DESDE__).LocalDateTime
$ev = @()
foreach ($f in @(@{ LogName = 'System'; ProviderName = 'Display'; Id = 4101 },
                 @{ LogName = 'System'; ProviderName = 'nvlddmkm'; Level = 1, 2 },
                 @{ LogName = 'System'; ProviderName = 'amdkmdag'; Level = 1, 2 },
                 @{ LogName = 'System'; ProviderName = 'amdwddmg'; Level = 1, 2 })) {
    $f.StartTime = $desde
    try { $ev += @(Get-WinEvent -FilterHashtable $f -MaxEvents 20 -ErrorAction Stop | ForEach-Object { @{ p = [string]$_.ProviderName; id = [int]$_.Id; t = (E $_.TimeCreated) } }) } catch { }
}
ConvertTo-Json -InputObject @($ev) -Compress -Depth 3
"""

# ============================================================================ sensores
SCRIPT_SENSORES = r"""
$ns = $null
$vuelta = 0
while ($true) {
    if (-not $ns -and ($vuelta % 15) -eq 0) {
        if (Get-CimInstance -Namespace root -ClassName __NAMESPACE -Filter "Name='LibreHardwareMonitor'") { $ns = 'root/LibreHardwareMonitor' }
        elseif (Get-CimInstance -Namespace root -ClassName __NAMESPACE -Filter "Name='OpenHardwareMonitor'") { $ns = 'root/OpenHardwareMonitor' }
    }
    $vuelta++
    $o = @{}
    $p = Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation -Filter "Name='_Total'"
    if ($p) { $o.mhz = [double]$p.ProcessorFrequency * [double]$p.PercentProcessorPerformance / 100 }
    $z = @(Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation)
    if ($z.Count) {
        $o.acpi = ($z | ForEach-Object { if ($_.HighPrecisionTemperature) { $_.HighPrecisionTemperature / 10 - 273.15 } else { $_.Temperature - 273.15 } } |
                   Measure-Object -Maximum).Maximum
    }
    $g = @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine | Where-Object { $_.Name -like '*engtype_3D' })
    if ($g.Count) { $o.gpu3d = [double](($g | Measure-Object -Property UtilizationPercentage -Sum).Sum) }
    if ($ns) {
        $o.hw = @(Get-CimInstance -Namespace $ns -ClassName Sensor | Where-Object { $_.SensorType -eq 'Temperature' } |
            ForEach-Object { @{ n = [string]$_.Name; i = [string]$_.Identifier; v = [double]$_.Value } })
    }
    [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress -Depth 3))
    [Console]::Out.Flush()
    Start-Sleep -Seconds 2
}
"""
NOMBRES_CPU = ("cpu package", "core (tctl/tdie)", "core max", "cpu cores", "core average", "tdie", "tctl")


def interpretar_sensores(o: dict) -> dict:
    """Una línea del bucle de sensores -> {"cpu_temp", "gpu_uso", "mhz", "fuente_temp"}."""
    r = {"cpu_temp": None, "gpu_uso": None, "mhz": o.get("mhz") or None, "fuente_temp": ""}
    hw = lista(o.get("hw"))
    for nombre in NOMBRES_CPU:
        vals = [s.get("v") for s in hw if (s.get("n") or "").lower() == nombre and "/cpu" in (s.get("i") or "").lower()
                and 0 < (s.get("v") or 0) < 130]
        if vals:
            r["cpu_temp"], r["fuente_temp"] = max(vals), "LibreHardwareMonitor"
            break
    if r["cpu_temp"] is None and o.get("acpi") and 5 < o["acpi"] < 130:
        r["cpu_temp"], r["fuente_temp"] = round(o["acpi"], 1), "zona térmica ACPI (aproximada)"
    if o.get("gpu3d") is not None:
        r["gpu_uso"] = min(100.0, float(o["gpu3d"]))
    return r


class LectorWindows(LectorDisco):
    sin_cache = True        # \\.\PhysicalDriveN no pasa por la caché de archivos


class Windows(Plataforma):
    nombre = "Windows"

    def __init__(self):
        self._ps_sensores = None
        self._ultimo: dict = {}
        self._despierto = threading.Event()

    def equipo(self) -> dict:
        return normalizar_equipo(ps_json(SCRIPT_EQUIPO, 120) or {})

    def abrir_disco(self, disco: dict) -> LectorDisco:
        return LectorWindows(disco["ruta"])

    def smartctl(self, carpetas_herramientas: list[str]) -> str | None:
        for c in buscar(carpetas_herramientas, "smartctl.exe", profundidad=3):
            return c
        for c in (shutil.which("smartctl"), r"C:\Program Files\smartmontools\bin\smartctl.exe"):
            if c and os.path.exists(c):
                return c
        return None

    def baterias(self) -> list[dict]:
        ruta = os.path.join(tempfile.gettempdir(), "openstd_bateria.xml")
        try:
            os.remove(ruta)
        except OSError:
            pass
        correr(["powercfg", "/batteryreport", "/xml", "/output", ruta], 60)
        try:
            with open(ruta, encoding="utf-8", errors="replace") as f:
                return interpretar_powercfg(f.read())
        except OSError:
            return []

    def eventos(self, desde: float) -> dict:
        return interpretar_eventos(ps_json(SCRIPT_EVENTOS.replace("__DESDE__", str(int(desde))), 180))

    # ------------------------------------------------------------------ GPU
    def _gpu_herramientas(self, carpetas: list[str]) -> list[dict]:
        ops = []
        for exe in buscar(carpetas, "FurMark.exe", "furmark.exe", profundidad=3):
            d = os.path.dirname(exe)
            if any(o["cwd"] == d for o in ops):
                continue
            if os.path.exists(os.path.join(d, "FurMark_GUI.exe")) or "2" in os.path.basename(d):
                cmd = [exe, "--demo", "furmark-gl", "--width", "1280", "--height", "720"]
                nombre = "FurMark 2"
            else:
                cmd = [exe, "/nogui", "/nomenubar", "/noscore", "/width=1280", "/height=720", "/msaa=0", "/run_mode=2",
                       "/disable_catalyst_warning"]
                nombre = "FurMark 1"
            ops.append({"nombre": nombre, "cmd": cmd, "cwd": d, "entorno": {}})
        return ops

    def gpu_preparar(self) -> dict:
        r = super().gpu_preparar()
        videos = self.equipo().get("video") or []
        if videos and all("basic" in v["nombre"].lower() or "básico" in v["nombre"].lower() for v in videos):
            r["problema"] = "Windows usa el driver básico de Microsoft: instalar el driver de la placa de video antes de probarla"
        return r

    def gpu_errores(self, desde: float, marca=None) -> list[str]:
        ev = lista(ps_json(SCRIPT_EVENTOS_GPU.replace("__DESDE__", str(int(desde) - 2)), 60))
        return [f"{e.get('p')} evento {e.get('id')}" for e in ev if isinstance(e, dict)]

    def preferir_gpu_dedicada(self, exe: str) -> None:
        """En notebooks con dos placas, que Windows use la dedicada para esta herramienta."""
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\DirectX\UserGpuPreferences") as k:
                winreg.SetValueEx(k, exe, 0, winreg.REG_SZ, "GpuPreference=2;")
        except OSError:
            pass

    # ------------------------------------------------------------------ sensores
    def iniciar_sensores(self) -> None:
        if self._ps_sensores and self._ps_sensores.poll() is None:
            return
        cod = base64.b64encode((PRELUDIO + SCRIPT_SENSORES).encode("utf-16-le")).decode()
        try:
            self._ps_sensores = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", cod],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=SIN_VENTANA)
        except OSError:
            self._ps_sensores = None
            return
        threading.Thread(target=self._leer_sensores_ps, args=(self._ps_sensores,), daemon=True, name="sensores-ps").start()

    def _leer_sensores_ps(self, p) -> None:
        for linea in p.stdout:
            try:
                self._ultimo = interpretar_sensores(json.loads(linea.decode("utf-8", "replace")))
                self._ultimo["t"] = time.time()
            except ValueError:
                continue

    def detener_sensores(self) -> None:
        if self._ps_sensores and self._ps_sensores.poll() is None:
            self._ps_sensores.kill()
        self._ps_sensores = None

    def leer_sensores(self) -> dict:
        r = dict(self._ultimo) if time.time() - self._ultimo.get("t", 0) < 10 else {}
        nv = nvidia_smi(self._nvidia_smi())
        if nv:
            r["gpu_temp"], r["gpu_uso"] = nv.get("temp"), nv.get("uso")
        return r

    @staticmethod
    def _nvidia_smi() -> str | None:
        for r in (shutil.which("nvidia-smi"), os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")):
            if r and os.path.exists(r):
                return r
        return None

    def mantener_despierto(self, si: bool) -> None:
        """Mientras corre el diagnóstico, Windows no suspende la PC."""
        if not si:
            self._despierto.clear()
            return
        if self._despierto.is_set():
            return
        self._despierto.set()

        def bucle():
            import ctypes
            k = ctypes.windll.kernel32
            while self._despierto.is_set():
                k.SetThreadExecutionState(0x80000000 | 0x00000001)      # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                time.sleep(30)
            k.SetThreadExecutionState(0x80000000)
        threading.Thread(target=bucle, daemon=True, name="despierto").start()
