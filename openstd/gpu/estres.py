"""Prueba de placa de video propia de OpenSTD Test. No depende de FurMark ni de otros programas.

Usa wgpu (WebGPU nativo), que habla con la placa por Vulkan, Direct3D 12, OpenGL o Metal: así se prueba
la placa por cada API que tenga el driver. En cada pasada:

  1. Memoria de video (VRAM): llena bloques de memoria con un patrón que cambia en cada pasada y en otra
     orden lo vuelve a leer y lo compara en la misma GPU. Cualquier diferencia es un error de VRAM.
  2. Cálculo verificado: miles de hilos hacen cuentas enteras y de coma flotante; la cuenta se hace dos
     veces y se comparan los resultados (una GPU estable da siempre exactamente lo mismo). Además una muestra
     de los resultados enteros se recalcula en el procesador.
  3. Carga sostenida: se repite hasta completar el tiempo, para que la placa caliente como en un juego.

Corre en un proceso aparte (`python -m openstd.gpu.estres`) para que, si el driver de video se cuelga,
el programa principal siga vivo y lo informe. Escribe una línea JSON por evento en la salida estándar.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

M32 = 0xFFFFFFFF
HILOS = 256                 # hilos por grupo
GRUPOS = 1024               # grupos por despacho (el shader recorre el buffer "a saltos")
OBJETIVO_DESPACHO = 0.25    # segundos por despacho de carga: lejos de los 2 s en que Windows reinicia el driver

WGSL = """
struct Params { semilla: u32, iters: u32, n: u32, extra: u32 };
@group(0) @binding(0) var<uniform> p: Params;
@group(0) @binding(1) var<storage, read_write> a: array<u32>;
@group(0) @binding(2) var<storage, read_write> b: array<u32>;
@group(0) @binding(3) var<storage, read_write> err: array<atomic<u32>>;

fn pcg(v: u32) -> u32 {
    let s = v * 747796405u + 2891336453u;
    let w = ((s >> ((s >> 28u) + 4u)) ^ s) * 277803737u;
    return (w >> 22u) ^ w;
}

// ---------------------------------------------------------------- VRAM: escribir el patrón
@compute @workgroup_size(256)
fn escribir(@builtin(global_invocation_id) id: vec3<u32>, @builtin(num_workgroups) ng: vec3<u32>) {
    let paso = ng.x * 256u;
    for (var i = id.x; i < p.n; i = i + paso) {
        a[i] = pcg(i ^ p.semilla);
    }
}

// ---------------------------------------------------------------- VRAM: releer y contar diferencias
@compute @workgroup_size(256)
fn verificar(@builtin(global_invocation_id) id: vec3<u32>, @builtin(num_workgroups) ng: vec3<u32>) {
    let paso = ng.x * 256u;
    var malos = 0u;
    for (var i = id.x; i < p.n; i = i + paso) {
        if (a[i] != pcg(i ^ p.semilla)) { malos = malos + 1u; }
    }
    if (malos > 0u) { atomicAdd(&err[0], malos); }
}

// ---------------------------------------------------------------- cálculo: enteros (a) y coma flotante (b)
@compute @workgroup_size(256)
fn calcular(@builtin(global_invocation_id) id: vec3<u32>, @builtin(num_workgroups) ng: vec3<u32>) {
    let paso = ng.x * 256u;
    for (var i = id.x; i < p.n; i = i + paso) {
        var x = pcg(i ^ p.semilla);
        var f = f32(x & 0xFFFFu) * 0.0001 + 1.0;
        for (var k = 0u; k < p.iters; k = k + 1u) {
            x = x * 1664525u + 1013904223u;
            x = x ^ (x >> 13u);
            f = fma(f, 0.999983, 0.0001) + sin(f) * 0.00001;
        }
        a[i] = x;
        b[i] = bitcast<u32>(f);
    }
}

// ---------------------------------------------------------------- cálculo: comparar con la pasada anterior
@compute @workgroup_size(256)
fn comparar(@builtin(global_invocation_id) id: vec3<u32>, @builtin(num_workgroups) ng: vec3<u32>) {
    // a = resultado de la primera pasada, b = el de la segunda: tienen que ser idénticos
    let paso = ng.x * 256u;
    var malos = 0u;
    for (var i = id.x; i < p.n; i = i + paso) {
        if (a[i] != b[i]) { malos = malos + 1u; }
    }
    if (malos > 0u) { atomicAdd(&err[1], malos); }
}
"""


NOMBRES = {"Vulkan": "Vulkan", "D3D12": "DirectX 12", "OpenGL": "OpenGL", "Metal": "Metal", "D3D11": "DirectX 11"}


class VentanaCerrada(Exception):
    """El usuario cerró la ventana de la prueba."""


def _reloj(seg: float) -> str:
    seg = int(seg)
    return f"{seg // 60}:{seg % 60:02d}"


def pcg(v: int) -> int:
    s = (v * 747796405 + 2891336453) & M32
    w = (((s >> ((s >> 28) + 4)) ^ s) * 277803737) & M32
    return (w >> 22) ^ w


def entero_cpu(i: int, semilla: int, iters: int) -> int:
    """Lo mismo que hace el shader 'calcular' con la parte entera, en el procesador."""
    x = pcg(i ^ semilla)
    for _ in range(iters):
        x = (x * 1664525 + 1013904223) & M32
        x ^= x >> 13
    return x


def decir(**d) -> None:
    print(json.dumps(d, ensure_ascii=False), flush=True)


# ============================================================================ listar
def adaptadores() -> list[dict]:
    import wgpu
    r = []
    for a in wgpu.gpu.enumerate_adapters_sync():
        i = a.info
        r.append({"api": i.get("backend_type"), "nombre": i.get("device") or "?", "tipo": i.get("adapter_type"),
                  "driver": i.get("description") or "", "vendor_id": i.get("vendor_id"),
                  "software": i.get("adapter_type") == "CPU" or "basic render" in (i.get("device") or "").lower()
                  or "llvmpipe" in (i.get("device") or "").lower()})
    return r


# ============================================================================ prueba
class Prueba:
    def __init__(self, api: str, indice: int = 0, mb_vram: int = 512, simular_error: bool = False):
        self.simular_error = simular_error
        import wgpu
        self.wgpu = wgpu
        candidatos = [a for a in wgpu.gpu.enumerate_adapters_sync() if a.info.get("backend_type") == api]
        if not candidatos:
            raise LookupError(f"no hay ninguna placa con {api}")
        self.adapter = candidatos[min(indice, len(candidatos) - 1)]
        lim = self.adapter.limits
        self.max_buffer = min(int(lim.get("max-storage-buffer-binding-size") or 128 * 2 ** 20),
                              int(lim.get("max-buffer-size") or 256 * 2 ** 20), 256 * 2 ** 20) // 4 * 4
        self.device = self.adapter.request_device_sync(required_limits={
            "max-storage-buffer-binding-size": self.max_buffer, "max-buffer-size": self.max_buffer})
        self.mb_vram = mb_vram
        bu = wgpu.BufferUsage
        self.mod = self.device.create_shader_module(code=WGSL)
        ss = wgpu.ShaderStage.COMPUTE
        almacen = {"type": wgpu.BufferBindingType.storage}
        self.bgl = self.device.create_bind_group_layout(entries=[
            {"binding": 0, "visibility": ss, "buffer": {"type": wgpu.BufferBindingType.uniform}},
            {"binding": 1, "visibility": ss, "buffer": almacen}, {"binding": 2, "visibility": ss, "buffer": almacen},
            {"binding": 3, "visibility": ss, "buffer": almacen}])
        capa = self.device.create_pipeline_layout(bind_group_layouts=[self.bgl])
        self.pipes = {n: self.device.create_compute_pipeline(layout=capa, compute={"module": self.mod, "entry_point": n})
                      for n in ("escribir", "verificar", "calcular", "comparar")}
        self.params = self.device.create_buffer(size=16, usage=bu.UNIFORM | bu.COPY_DST)
        self.err = self.device.create_buffer(size=16, usage=bu.STORAGE | bu.COPY_SRC | bu.COPY_DST)
        self.vacio = self.device.create_buffer(size=16, usage=bu.STORAGE)
        # buffers de VRAM (patrón) y de cálculo
        self.vram = []
        restante = mb_vram * 2 ** 20
        while restante >= 2 ** 20:
            tam = min(self.max_buffer, restante)
            try:
                self.vram.append(self.device.create_buffer(size=tam, usage=bu.STORAGE))
            except Exception:  # noqa: BLE001 - sin más memoria: se prueba lo que se pudo reservar
                break
            restante -= tam
        self.n_calc = HILOS * GRUPOS * 4
        tam_calc = self.n_calc * 4
        self.calc = [self.device.create_buffer(size=tam_calc, usage=bu.STORAGE | bu.COPY_SRC | bu.COPY_DST) for _ in range(4)]

    @property
    def bytes_vram(self) -> int:
        return sum(b.size for b in self.vram)

    def _grupo(self, pipe, buf_a, buf_b, tam_a=None):
        return self.device.create_bind_group(layout=self.bgl, entries=[
            {"binding": 0, "resource": {"buffer": self.params, "offset": 0, "size": 16}},
            {"binding": 1, "resource": {"buffer": buf_a, "offset": 0, "size": tam_a or buf_a.size}},
            {"binding": 2, "resource": {"buffer": buf_b, "offset": 0, "size": buf_b.size}},
            {"binding": 3, "resource": {"buffer": self.err, "offset": 0, "size": 16}}])

    def _correr(self, pipe, grupo, semilla, iters, n):
        import struct
        self.device.queue.write_buffer(self.params, 0, struct.pack("<4I", semilla & M32, iters, n, 0))
        enc = self.device.create_command_encoder()
        cp = enc.begin_compute_pass()
        cp.set_pipeline(self.pipes[pipe])
        cp.set_bind_group(0, grupo)
        cp.dispatch_workgroups(GRUPOS, 1, 1)
        cp.end()
        self.device.queue.submit([enc.finish()])

    def _errores(self) -> tuple[int, int]:
        import struct
        v = struct.unpack("<4I", bytes(self.device.queue.read_buffer(self.err)))
        self.device.queue.write_buffer(self.err, 0, b"\0" * 16)
        return v[0], v[1]

    def _leer(self, buf, n=None) -> list[int]:
        import struct
        datos = bytes(self.device.queue.read_buffer(buf))
        n = n or len(datos) // 4
        return list(struct.unpack(f"<{n}I", datos[:n * 4]))

    def calibrar(self, objetivo: float = OBJETIVO_DESPACHO) -> int:
        """Elige cuántas vueltas hace cada hilo para que un despacho dure lo pedido (~0,25 s; menos con ventana)."""
        iters = 16
        g = self._grupo("calcular", self.calc[0], self.calc[1])
        self._correr("calcular", g, 1, iters, self.n_calc)        # la primera vez incluye preparar el shader
        self._leer(self.err)
        while True:
            t0 = time.perf_counter()
            self._correr("calcular", g, 1, iters, self.n_calc)
            self._leer(self.err)                      # espera a que termine
            dt = time.perf_counter() - t0
            if dt >= objetivo / 2 or iters >= 1 << 16:
                return max(16, min(1 << 16, int(iters * objetivo / max(dt, 1e-3))))
            iters *= 4

    def ejecutar(self, segundos: float, ventana=None) -> dict:
        r = {"pases": 0, "errores_vram": 0, "errores_calculo": 0, "errores_cpu": 0, "bytes_vram": self.bytes_vram,
             "despachos": 0, "muestras_cpu": 0}
        api = self.adapter.info.get("backend_type") or ""
        nombre = self.adapter.info.get("device") or ""
        estado = {"fin": time.time() + segundos, "iters": 0}

        def dibujar(aviso: str = ""):
            """Un cuadro de la ventana entre despacho y despacho. Cerrar la ventana corta la prueba."""
            if ventana is None:
                return
            if ventana.cerrada:
                raise VentanaCerrada()
            resta = max(0.0, estado["fin"] - time.time())
            errores = r["errores_vram"] + r["errores_calculo"] + r["errores_cpu"]
            transcurrido = segundos - resta
            ventana.cuadro([
                "OpenSTD Test · prueba de placa de video",
                f"{nombre[:44]} · {NOMBRES.get(api, api)}",
                f"{ventana.fps:.0f} FPS (promedio {ventana.fps_promedio:.0f}) · {ventana.tam[0]}x{ventana.tam[1]}"
                f" · {_reloj(transcurrido)} / {_reloj(segundos)}",
                aviso or f"Pasada {r['pases'] + 1} · memoria de video verificada: {r['bytes_vram'] // 2 ** 20} MB",
                f"Errores: memoria de video {r['errores_vram']} · calculo {r['errores_calculo']} · CPU {r['errores_cpu']}"
                + ("" if errores else " · todo bien"),
                "",
                "Cerrar esta ventana (o Esc) detiene la prueba",
            ], transcurrido / max(segundos, 1e-3), errores > 0)

        try:
            dibujar("Calibrando la carga...")
            iters = self.calibrar(0.03 if ventana else OBJETIVO_DESPACHO)
            decir(tipo="calibrado", iters=iters, vram=self.bytes_vram)
            rnd = random.Random(12345)
            estado["fin"] = fin = time.time() + segundos
            self._bucle(r, iters, rnd, fin, dibujar)
        except VentanaCerrada:
            r["ventana_cerrada"] = True
        if ventana is not None:
            r["fps"] = round(ventana.fps_promedio, 1)
            r["resolucion"] = f"{ventana.tam[0]}x{ventana.tam[1]}"
        return r

    def _bucle(self, r, iters, rnd, fin, dibujar) -> None:
        g_vram = [(self._grupo("escribir", b, self.vacio), self._grupo("verificar", b, self.vacio), b.size // 4) for b in self.vram]
        while True:
            semilla = rnd.getrandbits(32)
            # 1) VRAM: escribir todos los bloques; después releerlos (los datos quedan un rato guardados)
            for ge, _, n in g_vram:
                self._correr("escribir", ge, semilla, 0, n)
                dibujar()
            # 2) cálculo dos veces sobre buffers distintos
            pares = [(self.calc[0], self.calc[1]), (self.calc[2], self.calc[3])]
            for xa, xb in pares:
                self._correr("calcular", self._grupo("calcular", xa, xb), semilla, iters, self.n_calc)
                dibujar()
            for _, gv, n in g_vram:
                self._correr("verificar", gv, semilla ^ (1 if self.simular_error else 0), 0, n)
                dibujar()
            # 3) comparar resultado entero y flotante de las dos pasadas
            self._correr("comparar", self._grupo("comparar", self.calc[0], self.calc[2]), 0, 0, self.n_calc)
            self._correr("comparar", self._grupo("comparar", self.calc[1], self.calc[3]), 0, 0, self.n_calc)
            ev, ec = self._errores()
            r["errores_vram"] += ev
            r["errores_calculo"] += ec
            r["despachos"] += 2 * len(g_vram) + 4
            # 4) una muestra de resultados enteros, recalculada en el procesador
            enteros = self._leer(self.calc[0])
            for i in rnd.sample(range(self.n_calc), 4):
                r["muestras_cpu"] += 1
                if enteros[i] != entero_cpu(i, semilla, iters):
                    r["errores_cpu"] += 1
            r["pases"] += 1
            decir(tipo="pase", **r, iters=iters, resta=max(0, fin - time.time()))
            if time.time() >= fin or (r["errores_vram"] + r["errores_calculo"] + r["errores_cpu"]) > 1000:
                break
            dibujar()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prueba de placa de video de OpenSTD Test")
    ap.add_argument("--listar", action="store_true")
    ap.add_argument("--api", default="")
    ap.add_argument("--indice", type=int, default=0)
    ap.add_argument("--segundos", type=float, default=60)
    ap.add_argument("--mb", type=int, default=512, help="MB de memoria de video a verificar")
    ap.add_argument("--ventana", action="store_true", help="mostrar la prueba en una ventana con una escena 3D")
    ap.add_argument("--simular-error", action="store_true", help="para probar que los errores se detectan")
    a = ap.parse_args(argv)
    try:
        import wgpu  # noqa: F401
    except ImportError:
        decir(tipo="error", fase="inicio", texto="Falta la librería wgpu (pip install wgpu)")
        return 3
    if a.listar:
        try:
            decir(tipo="adaptadores", lista=adaptadores())
        except Exception as e:  # noqa: BLE001
            decir(tipo="error", fase="inicio", texto=f"{type(e).__name__}: {e}")
            return 3
        return 0
    fase = "inicio"
    try:
        p = Prueba(a.api, a.indice, a.mb, a.simular_error)
        i = p.adapter.info
        decir(tipo="inicio", api=a.api, nombre=i.get("device"), driver=i.get("description") or "", vram=p.bytes_vram)
        ventana = None
        if a.ventana:
            try:
                from .ventana import Ventana, hay_pantalla
                if not hay_pantalla():
                    raise RuntimeError("no hay una sesión gráfica")
                ventana = Ventana(p.device, p.adapter, f"OpenSTD Test · {NOMBRES.get(a.api, a.api)} · {i.get('device')}")
            except Exception as e:  # noqa: BLE001 - sin ventana la prueba sigue igual
                decir(tipo="aviso", texto=f"No se pudo abrir la ventana de la prueba ({type(e).__name__}: {e}"[:300] + "); sigue sin ventana")
        fase = "carga"
        try:
            r = p.ejecutar(a.segundos, ventana)
        finally:
            if ventana is not None:
                ventana.cerrar()
        decir(tipo="fin", **r)
        return 0
    except Exception as e:  # noqa: BLE001
        decir(tipo="error", fase=fase, texto=f"{type(e).__name__}: {e}"[:400])
        return 3 if fase == "inicio" else 4


if __name__ == "__main__":
    sys.exit(main())
