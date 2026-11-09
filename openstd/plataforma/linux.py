"""Linux: lee el equipo de /sys, /proc, dmidecode, lsblk y lspci, y los errores del kernel de journalctl."""
from __future__ import annotations

import errno
import glob
import json
import mmap
import os
import re
import shutil
import time

from ..util import correr, hay, leer, leer_int
from .base import LectorDisco, Plataforma, buscar, eventos_vacios

GENERICOS = ("o.e.m", "default string", "system product name", "system manufacturer", "to be filled",
             "not applicable", "not specified", "none", "123456789", "system serial number")
CHIPS_CPU = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz")
CHIPS_GPU = ("amdgpu", "radeon", "nouveau", "i915", "xe")
CHASIS_PORTATIL = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}

# ---------------------------------------------------------------------------- patrones del registro del kernel
PATRONES = {
    "hardware_grave": [r"mce: \[Hardware Error\].*(uncorrected|fatal|panic)", r"Machine check.*(fatal|panic)",
                       r"EDAC .*\bUE\b", r"PCIe Bus Error: severity=(Uncorrected|Fatal)", r"Hardware Error.*fatal"],
    "hardware_leve": [r"mce: \[Hardware Error\]", r"EDAC .*\bCE\b", r"PCIe Bus Error: severity=Corrected",
                      r"\bHardware Error\b"],
    "disco_grave": [r"I/O error, dev (?!loop|sr|fd|zram)\w+", r"Buffer I/O error on dev (?!loop|sr|fd)\w+",
                    r"EXT4-fs error", r"BTRFS (error|critical)", r"XFS \(\w+\): (Corruption|metadata I/O error)",
                    r"ata\d+(\.\d+)?: .*(UNC|media error)", r"Medium Error"],
    "disco_leve": [r"ata\d+(\.\d+)?: (exception Emask|failed command|hard resetting link|SError)",
                   r"nvme\d+(n\d+)?: (I/O \d+ QID \d+ timeout|controller is down|resetting controller)"],
    "video": [r"NVRM: Xid", r"GPU HANG", r"gpu hang", r"ring \S+ timeout", r"amdgpu.*(timeout|GPU fault|GPU reset|page fault)",
              r"nouveau.*(fault|timeout)", r"i915.*(hang|GPU reset)", r"\bxe\b.*(hang|reset)", r"radeon.*(lockup|GPU reset)"],
    "memoria": [r"Out of memory: Killed process", r"oom-kill"],
}
_COMPILADOS = {k: [re.compile(p, re.I) for p in v] for k, v in PATRONES.items()}


def clasificar_linea(linea: str) -> str | None:
    """A qué grupo pertenece una línea del kernel (o None si no interesa)."""
    for grupo in ("hardware_grave", "hardware_leve", "disco_grave", "disco_leve", "video", "memoria"):
        if any(p.search(linea) for p in _COMPILADOS[grupo]):
            return grupo
    return None


def interpretar_kernel(lineas: list[tuple[float, str]], desde: float) -> dict:
    """lineas: [(timestamp, texto)]. Devuelve el dict de eventos normalizado."""
    ev = eventos_vacios(True)
    for t, texto in lineas:
        if t < desde:
            continue
        g = clasificar_linea(texto)
        if not g:
            continue
        texto = texto.strip()[:200]
        if g.startswith("hardware"):
            ev["hardware"].append({"t": t, "texto": texto, "grave": g.endswith("grave")})
        elif g.startswith("disco"):
            ev["disco"].append({"t": t, "texto": texto, "grave": g.endswith("grave")})
        elif g == "video":
            ev["video"].append({"t": t, "texto": texto})
        elif g == "memoria":
            ev["memoria"].append({"t": t, "texto": texto})
    return ev


def interpretar_journal(salida: str) -> list[tuple[float, str]]:
    """journalctl -o short-unix: '1730900000.123456 host kernel: texto'."""
    r = []
    for linea in salida.splitlines():
        m = re.match(r"^(\d+(?:\.\d+)?)\s+\S+\s+(?:kernel:\s*)?(.*)$", linea)
        if m:
            r.append((float(m.group(1)), m.group(2)))
    return r


def interpretar_lsblk(salida: str) -> list[dict]:
    try:
        datos = json.loads(salida).get("blockdevices", [])
    except (ValueError, AttributeError):
        return []
    r = []
    for d in datos:
        nombre = str(d.get("name") or "")
        if d.get("type") != "disk" or nombre.startswith(("loop", "zram", "ram", "sr", "fd")):
            continue
        rota = str(d.get("rota")).lower() in ("1", "true")
        tran = str(d.get("tran") or "").lower()
        tipo = "NVMe" if nombre.startswith("nvme") or tran == "nvme" else ("HDD" if rota else "SSD")
        r.append({"id": nombre, "ruta": "/dev/" + nombre, "smart_ruta": "/dev/" + nombre,
                  "modelo": (d.get("model") or "").strip() or "(sin modelo)", "serie": (d.get("serial") or "").strip(),
                  "tam": int(d.get("size") or 0), "tipo": tipo, "bus": tran.upper(), "es_usb": tran == "usb",
                  "es_sistema": False, "sector": 512, "salud_so": None})
    return r


def interpretar_dmidecode_ram(salida: str) -> list[dict]:
    modulos = []
    for bloque in salida.split("\n\n"):
        if "Memory Device" not in bloque:
            continue
        campos = {}
        for linea in bloque.splitlines():
            if ":" in linea:
                k, v = linea.split(":", 1)
                campos[k.strip()] = v.strip()
        m = re.match(r"(\d+)\s*(MB|GB|TB)", campos.get("Size", ""))
        if not m:
            continue        # "No Module Installed"
        modulos.append({"ranura": campos.get("Locator", ""), "tam": int(m.group(1)) * {"MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}[m.group(2)],
                        "tipo": campos.get("Type", ""), "velocidad": campos.get("Configured Memory Speed", campos.get("Speed", "")),
                        "fabricante": _limpio(campos.get("Manufacturer", "")), "parte": _limpio(campos.get("Part Number", ""))})
    return modulos


def interpretar_lspci(salida: str) -> list[dict]:
    r, actual = [], None
    for linea in salida.splitlines():
        if not linea.startswith(("\t", " ")):
            actual = None
            if re.search(r"VGA compatible controller|3D controller|Display controller", linea):
                desc = linea.split(": ", 1)[-1].strip()
                actual = {"nombre": re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", desc).replace("(rev", " (rev").strip(), "driver": "",
                          "vendor": "nvidia" if "[10de:" in linea else "amd" if "[1002:" in linea else "intel" if "[8086:" in linea else ""}
                r.append(actual)
        elif actual is not None and "Kernel driver in use:" in linea:
            actual["driver"] = linea.split(":", 1)[1].strip()
    return r


def leer_baterias(raiz: str = "/sys") -> list[dict]:
    r = []
    for d in sorted(glob.glob(os.path.join(raiz, "class", "power_supply", "*"))):
        if leer(os.path.join(d, "type")) != "Battery" or leer(os.path.join(d, "scope")) == "Device":
            continue        # scope=Device: batería de un mouse o joystick
        energia = leer_int(os.path.join(d, "energy_full"))
        r.append({"nombre": os.path.basename(d), "fabricante": leer(os.path.join(d, "manufacturer"), "") or "",
                  "modelo": leer(os.path.join(d, "model_name"), "") or "", "quimica": leer(os.path.join(d, "technology"), "") or "",
                  "actual": energia or leer_int(os.path.join(d, "charge_full")),
                  "diseno": leer_int(os.path.join(d, "energy_full_design")) or leer_int(os.path.join(d, "charge_full_design")),
                  "unidad": "µWh" if energia else "µAh", "ciclos": leer_int(os.path.join(d, "cycle_count")),
                  "estado": leer(os.path.join(d, "status"), "") or "", "carga": leer_int(os.path.join(d, "capacity"))})
    return r


def leer_temperaturas(raiz: str = "/sys") -> dict:
    """Temperaturas de /sys/class/hwmon: {"cpu": °C o None, "gpu": °C o None, "fuente": texto}."""
    cpu, gpu, fuente = [], [], ""
    for d in glob.glob(os.path.join(raiz, "class", "hwmon", "hwmon*")):
        chip = leer(os.path.join(d, "name"), "?") or "?"
        for entrada in glob.glob(os.path.join(d, "temp*_input")):
            v = leer_int(entrada)
            if v is None or not 0 < v / 1000 < 130:
                continue
            etiqueta = (leer(entrada.replace("_input", "_label"), "") or "").lower()
            if chip in CHIPS_CPU:
                cpu.append((etiqueta in ("tdie", "package id 0", "tctl"), v / 1000, chip))
            elif chip in CHIPS_GPU:
                gpu.append(v / 1000)
    t_cpu = None
    if cpu:
        preferidas = [c for c in cpu if c[0]]
        t_cpu = max(c[1] for c in (preferidas or cpu))
        fuente = (preferidas or cpu)[0][2]
    return {"cpu": t_cpu, "gpu": max(gpu) if gpu else None, "fuente": fuente}


def _limpio(s: str) -> str:
    s = (s or "").strip()
    return "" if not s or any(g in s.lower() for g in GENERICOS) else s


class LectorLinux(LectorDisco):
    """Lee el disco con O_DIRECT (sin la caché del sistema) para medir tiempos reales."""

    def __init__(self, ruta: str):
        self.sin_cache = True
        try:
            self._fd = os.open(ruta, os.O_RDONLY | getattr(os, "O_DIRECT", 0))
        except OSError as e:
            if e.errno != errno.EINVAL:
                raise
            self._fd = os.open(ruta, os.O_RDONLY)       # el dispositivo no acepta O_DIRECT
            self.sin_cache = False
        self._buf = mmap.mmap(-1, 128 * 1024)          # alineado a página, como pide O_DIRECT

    def leer(self, offset: int, buf) -> int:
        n = len(buf)
        if n > len(self._buf):
            self._buf.close()
            self._buf = mmap.mmap(-1, n)
        vista = memoryview(self._buf)[:n]
        leidos = os.preadv(self._fd, [vista], offset)
        if not self.sin_cache:
            os.posix_fadvise(self._fd, offset, n, os.POSIX_FADV_DONTNEED)
        buf[:leidos] = vista[:leidos]
        vista.release()
        return leidos

    def cerrar(self) -> None:
        os.close(self._fd)
        self._buf.close()


class Linux(Plataforma):
    nombre = "Linux"
    OFFLOAD_NVIDIA = {"__NV_PRIME_RENDER_OFFLOAD": "1", "__GLX_VENDOR_LIBRARY_NAME": "nvidia", "__VK_LAYER_NV_optimus": "NVIDIA_only"}
    SOFTWARE = ("llvmpipe", "softpipe", "software rasterizer")

    # ------------------------------------------------------------------ equipo
    def _dmi(self, clave: str) -> str:
        mapa = {"system-manufacturer": "sys_vendor", "system-product-name": "product_name", "system-serial-number": "product_serial",
                "baseboard-manufacturer": "board_vendor", "baseboard-product-name": "board_name", "bios-version": "bios_version",
                "bios-release-date": "bios_date", "chassis-type": "chassis_type"}
        v = _limpio(leer("/sys/class/dmi/id/" + mapa.get(clave, "x"), "") or "")
        if v or not hay("dmidecode"):
            return v
        cod, out = correr(["dmidecode", "-s", clave], timeout=15)
        return _limpio(out.splitlines()[0]) if cod == 0 and out.strip() and not out.startswith("#") else ""

    def equipo(self) -> dict:
        import psutil
        lscpu = {}
        for linea in correr(["lscpu"], 10)[1].splitlines():
            if ":" in linea:
                k, v = linea.split(":", 1)
                lscpu[k.strip()] = v.strip()
        try:
            nucleos = int(lscpu.get("Core(s) per socket", 0)) * int(lscpu.get("Socket(s)", 1))
        except ValueError:
            nucleos = psutil.cpu_count(logical=False) or 0
        modulos = interpretar_dmidecode_ram(correr(["dmidecode", "-t", "17"], 15)[1]) if hay("dmidecode") else []
        discos = interpretar_lsblk(correr(["lsblk", "-J", "-b", "-d", "-o", "NAME,MODEL,SERIAL,SIZE,TRAN,ROTA,TYPE"], 15)[1])
        sistema = self._disco_sistema()
        for d in discos:
            d["es_sistema"] = d["id"] == sistema
            d["sector"] = leer_int(f"/sys/block/{d['id']}/queue/logical_block_size", 512) or 512
        os_rel = {}
        for l in (leer("/etc/os-release", "") or "").splitlines():
            if "=" in l:
                k, v = l.split("=", 1)
                os_rel[k] = v.strip('"')
        chasis = leer_int("/sys/class/dmi/id/chassis_type", 0) or 0
        fab, mod = self._dmi("system-manufacturer"), self._dmi("system-product-name")
        bios = " ".join(x for x in (self._dmi("bios-version"), self._dmi("bios-release-date")) if x)
        mhz = lscpu.get("CPU max MHz", "")
        bats = leer_baterias()
        return {
            "fabricante": fab, "modelo": mod, "serie": self._dmi("system-serial-number"),
            "placa": " ".join(x for x in (self._dmi("baseboard-manufacturer"), self._dmi("baseboard-product-name")) if x),
            "bios": bios, "nombre_pc": (leer("/etc/hostname", "") or os.uname().nodename).strip(),
            "sistema": f"{os_rel.get('PRETTY_NAME', 'Linux')} · kernel {os.uname().release}",
            "es_portatil": chasis in CHASIS_PORTATIL or bool(bats),
            "cpu": {"nombre": lscpu.get("Model name", "desconocido"), "nucleos": nucleos,
                    "hilos": psutil.cpu_count() or os.cpu_count() or 1, "mhz": int(float(mhz)) if mhz else None},
            "ram": {"instalada": sum(m["tam"] for m in modulos) or psutil.virtual_memory().total,
                    "visible": psutil.virtual_memory().total, "modulos": modulos},
            "video": [{"nombre": v["nombre"], "driver": v["driver"]} for v in interpretar_lspci(correr(["lspci", "-nnk"], 15)[1])],
            "discos": discos, "baterias": len(bats),
        }

    def _disco_sistema(self) -> str | None:
        cod, out = correr(["findmnt", "-n", "-o", "SOURCE", "/"], 10)
        fuente = out.strip().split("[")[0]
        if cod != 0 or not fuente.startswith("/dev/"):
            return None
        cod, out = correr(["lsblk", "-no", "PKNAME", fuente], 10)
        return out.strip().splitlines()[0] if out.strip() else os.path.basename(fuente)

    # ------------------------------------------------------------------ disco / batería
    def abrir_disco(self, disco: dict) -> LectorDisco:
        return LectorLinux(disco["ruta"])

    def smartctl(self, carpetas_herramientas: list[str]) -> str | None:
        return shutil.which("smartctl") or shutil.which("smartctl", path="/usr/sbin:/sbin")

    def baterias(self) -> list[dict]:
        return leer_baterias()

    # ------------------------------------------------------------------ sistema
    def _registro_kernel(self, desde: float) -> tuple[list[tuple[float, str]], str]:
        if hay("journalctl"):
            cod, out = correr(["journalctl", "-k", "-o", "short-unix", "--no-pager", "-q", "--since", f"@{int(desde)}"], 60)
            if cod == 0:
                return interpretar_journal(out), "registro del kernel (journalctl)"
        cod, out = correr(["dmesg", "--time-format", "iso"] if hay("dmesg") else ["false"], 30)
        if cod == 0:
            arranque = time.time() - float((leer("/proc/uptime", "0") or "0").split()[0])
            lineas = []
            for l in out.splitlines():
                m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", l)
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) if m else arranque
                lineas.append((t, l))
            return lineas, "registro del kernel desde el último arranque (dmesg)"
        return [], ""

    def eventos(self, desde: float) -> dict:
        lineas, fuente = self._registro_kernel(desde)
        if not fuente:
            return eventos_vacios(False, "No se pudo leer el registro del kernel (hace falta root o journalctl)")
        ev = interpretar_kernel(lineas, desde)
        ev["fuente"] = fuente
        # Cuelgues del kernel guardados por pstore (si el equipo lo soporta)
        for f in glob.glob("/sys/fs/pstore/dmesg-*") + glob.glob("/var/lib/systemd/pstore/*/dmesg*"):
            try:
                t = os.path.getmtime(f)
            except OSError:
                continue
            if t >= desde:
                ev["pantallazos"].append({"t": t, "codigo": None, "texto": "cuelgue del kernel (kernel panic) guardado en pstore"})
        return ev

    # ------------------------------------------------------------------ GPU
    def _gpu_herramientas(self, carpetas: list[str]) -> list[dict]:
        ops = []
        for exe in buscar(carpetas, "furmark", profundidad=3):
            if os.access(exe, os.X_OK):
                d = os.path.dirname(exe)
                ops.append({"nombre": "FurMark 2 (OpenGL)", "cmd": [exe, "--demo", "furmark-gl", "--width", "1280", "--height", "720"],
                            "cwd": d, "entorno": {}})
                ops.append({"nombre": "FurMark 2 (Vulkan)", "cmd": [exe, "--demo", "furmark-vk", "--width", "1280", "--height", "720"],
                            "cwd": d, "entorno": {}})
                break
        for exe in ("glmark2", "glmark2-es2", "glmark2-wayland"):
            if hay(exe):
                ops.append({"nombre": f"{exe} (OpenGL, en bucle)", "cmd": [exe, "--run-forever", "-s", "1280x720"], "cwd": None, "entorno": {}})
                break
        return ops

    def _renderer(self, entorno: dict | None = None) -> str | None:
        if not hay("glxinfo"):
            return None
        _, out = correr(["glxinfo", "-B"], 20, entorno=entorno)
        m = re.search(r"OpenGL renderer string:\s*(.+)", out)
        return m.group(1).strip() if m else None

    def gpu_preparar(self) -> dict:
        r = {"problema": None, "aviso": None, "entorno": {}, "lineas": []}
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            r["problema"] = "No hay sesión gráfica abierta: la prueba de GPU necesita el escritorio"
            return r
        placas = interpretar_lspci(correr(["lspci", "-nnk"], 15)[1])
        vendors = {p["vendor"] for p in placas}
        base = self._renderer()
        if base is None:
            r["aviso"] = "Sin glxinfo (paquete mesa-utils) no se pudo confirmar qué placa hace el trabajo"
            return r
        r["lineas"].append("Renderer: " + base)
        if "nvidia" in vendors and "nvidia" not in base.lower():
            off = self._renderer(self.OFFLOAD_NVIDIA)
            if off and "nvidia" in off.lower():
                r["entorno"] = dict(self.OFFLOAD_NVIDIA)
                r["lineas"][-1] = "Renderer: " + off + " (equipo híbrido: la carga va a la placa dedicada)"
                return r
            if re.match(r"NV[0-9A-F]{2,3}\b", base) or "nouveau" in base.lower():
                r["aviso"] = "Se prueba con el driver libre 'nouveau': la placa NVIDIA trabaja más lenta y la prueba es parcial"
                return r
            if not any(x in base.lower() for x in self.SOFTWARE):
                r["aviso"] = "Hay una placa NVIDIA sin driver: la prueba se hace con la otra placa"
                return r
        if any(x in base.lower() for x in self.SOFTWARE):
            r["problema"] = "No hay aceleración 3D (dibuja el procesador): falta instalar el driver de la placa de video"
            return r
        if len(vendors) > 1:
            prime = self._renderer({"DRI_PRIME": "1"})
            if prime and prime != base and not any(x in prime.lower() for x in self.SOFTWARE):
                r["entorno"] = {"DRI_PRIME": "1"}
                r["lineas"][-1] = "Renderer: " + prime + " (equipo híbrido: la carga va a la placa dedicada)"
        return r

    def gpu_errores(self, desde: float, marca=None) -> list[str]:
        lineas, _ = self._registro_kernel(desde - 1)
        return [t for ts, t in lineas if ts >= desde - 1 and clasificar_linea(t) == "video"]

    # ------------------------------------------------------------------ sensores
    def leer_sensores(self) -> dict:
        t = leer_temperaturas()
        nv = nvidia_smi()
        mhz = None
        vals = [leer_int(f) for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
        vals = [v for v in vals if v]
        if vals:
            mhz = sum(vals) / len(vals) / 1000
        return {"cpu_temp": t["cpu"], "gpu_temp": nv.get("temp") if nv.get("temp") is not None else t["gpu"],
                "gpu_uso": nv.get("uso"), "mhz": mhz, "fuente_temp": f"sensor {t['fuente']}" if t["fuente"] else ""}

    def mantener_despierto(self, si: bool) -> None:
        # systemd-inhibit necesitaría un proceso aparte; en los equipos de prueba se recomienda desactivar la suspensión
        pass


def nvidia_smi(exe: str | None = None) -> dict:
    """Temperatura y uso de la placa NVIDIA (si hay driver). {} si no hay."""
    exe = exe or shutil.which("nvidia-smi")
    if not exe:
        return {}
    cod, out = correr([exe, "--query-gpu=temperature.gpu,utilization.gpu", "--format=csv,noheader,nounits"], 10)
    if cod != 0:
        return {}
    try:
        temp, uso = [float(x) for x in out.strip().splitlines()[0].split(",")[:2]]
    except (ValueError, IndexError):
        return {}
    return {"temp": temp, "uso": uso}
