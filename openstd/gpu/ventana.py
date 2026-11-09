"""Ventana de la prueba de placa de video: una escena 3D animada (tipo benchmark) con los datos en vivo.

La escena se dibuja con la misma placa y la misma API que se están probando, mientras por detrás sigue la
verificación de memoria de video y de cálculos. Lo que se ve es carga real y la marcha de la prueba; los
errores los sigue detectando la verificación (la imagen no se compara).

Usa glfw para la ventana y wgpu para dibujar. Si no se puede abrir (no hay pantalla, falta glfw, Linux con
sudo sin acceso a la sesión gráfica...), la prueba sigue igual sin ventana.
"""
from __future__ import annotations

import os
import struct
import time
import unicodedata

COLUMNAS = 64
LINEAS = 7

# Letra de 5x7 puntos: cada fila es un número de 5 bits (el bit 4 es la columna de la izquierda).
FUENTE = {
    "0": "0E 11 13 15 19 11 0E", "1": "04 0C 04 04 04 04 0E", "2": "0E 11 01 02 04 08 1F", "3": "1F 02 04 02 01 11 0E",
    "4": "02 06 0A 12 1F 02 02", "5": "1F 10 1E 01 01 11 0E", "6": "06 08 10 1E 11 11 0E", "7": "1F 01 02 04 08 08 08",
    "8": "0E 11 11 0E 11 11 0E", "9": "0E 11 11 0F 01 02 0C",
    "A": "0E 11 11 1F 11 11 11", "B": "1E 11 11 1E 11 11 1E", "C": "0E 11 10 10 10 11 0E", "D": "1C 12 11 11 11 12 1C",
    "E": "1F 10 10 1E 10 10 1F", "F": "1F 10 10 1E 10 10 10", "G": "0E 11 10 17 11 11 0F", "H": "11 11 11 1F 11 11 11",
    "I": "0E 04 04 04 04 04 0E", "J": "07 02 02 02 02 12 0C", "K": "11 12 14 18 14 12 11", "L": "10 10 10 10 10 10 1F",
    "M": "11 1B 15 15 11 11 11", "N": "11 11 19 15 13 11 11", "O": "0E 11 11 11 11 11 0E", "P": "1E 11 11 1E 10 10 10",
    "Q": "0E 11 11 11 15 12 0D", "R": "1E 11 11 1E 14 12 11", "S": "0F 10 10 0E 01 01 1E", "T": "1F 04 04 04 04 04 04",
    "U": "11 11 11 11 11 11 0E", "V": "11 11 11 11 11 0A 04", "W": "11 11 11 15 15 15 0A", "X": "11 11 0A 04 0A 11 11",
    "Y": "11 11 11 0A 04 04 04", "Z": "1F 01 02 04 08 10 1F",
    ":": "00 0C 0C 00 0C 0C 00", ".": "00 00 00 00 00 0C 0C", ",": "00 00 00 00 0C 04 08", "%": "18 19 02 04 08 13 03",
    "/": "00 01 02 04 08 10 00", "-": "00 00 00 1F 00 00 00", "+": "00 04 04 1F 04 04 00", "(": "02 04 08 08 08 04 02",
    ")": "08 04 02 02 02 04 08", "=": "00 00 1F 00 1F 00 00", "|": "04 04 04 04 04 04 04",
    "\x01": "00 00 00 0C 0C 00 00",     # punto medio
    "\x02": "0C 12 12 0C 00 00 00",     # grado
}


def tabla_fuente() -> bytes:
    filas = [0] * (128 * 8)
    for ch, h in FUENTE.items():
        for i, v in enumerate(h.split()):
            filas[ord(ch) * 8 + i] = int(v, 16)
    return struct.pack(f"<{len(filas)}I", *filas)


def a_codigos(texto: str) -> list[int]:
    """Pasa un texto a los códigos que entiende la letra de la ventana (mayúsculas, sin tildes)."""
    texto = texto.replace("·", "\x01").replace("°", "\x02").replace("×", "X")
    texto = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c)).upper()
    r = [ord(c) if c in FUENTE else 32 for c in texto[:COLUMNAS]]
    return r + [32] * (COLUMNAS - len(r))


WGSL = """
struct U { tiempo: f32, ancho: f32, alto: f32, progreso: f32, alerta: f32, escala: f32, pasos: f32, extra: f32 };
@group(0) @binding(0) var<uniform> u: U;
@group(0) @binding(1) var<storage, read> fuente: array<u32>;
@group(0) @binding(2) var<storage, read> texto: array<u32>;

@vertex
fn vs(@builtin(vertex_index) i: u32) -> @builtin(position) vec4<f32> {
    let x = f32((i << 1u) & 2u) * 2.0 - 1.0;
    let y = f32(i & 2u) * 2.0 - 1.0;
    return vec4<f32>(x, y, 0.0, 1.0);
}

fn rot(a: f32) -> mat2x2<f32> { let c = cos(a); let s = sin(a); return mat2x2<f32>(c, -s, s, c); }
fn hash3(c: vec3<f32>) -> f32 { return fract(sin(dot(c, vec3<f32>(12.9898, 78.233, 37.719))) * 43758.5453); }

fn toro(p: vec3<f32>, t: vec2<f32>) -> f32 {
    let q = vec2<f32>(length(p.xz) - t.x, p.y);
    return length(q) - t.y;
}

fn smin(a: f32, b: f32, k: f32) -> f32 {
    let h = clamp(0.5 + 0.5 * (b - a) / k, 0.0, 1.0);
    return mix(b, a, h) - k * h * (1.0 - h);
}

// la escena: anillos que giran en una grilla infinita, unidos a un piso ondulado
fn mapa(p0: vec3<f32>) -> vec2<f32> {
    var p = p0;
    p.z = p.z + u.tiempo * 3.0;
    let celda = round(p / 4.0);
    var r = p - 4.0 * celda;
    let h = hash3(celda);
    let rxy = rot(u.tiempo * (0.6 + h) + h * 6.28) * r.xy;
    r = vec3<f32>(rxy.x, rxy.y, r.z);
    let ryz = rot(u.tiempo * 0.7 + h * 3.0) * r.yz;
    r = vec3<f32>(r.x, ryz.x, ryz.y);
    let d1 = toro(r, vec2<f32>(1.1, 0.28 + 0.12 * h));
    let piso = p0.y + 2.6 + 0.25 * sin(p.x * 0.9 + u.tiempo) * sin(p.z * 0.7);
    let d = smin(d1, piso, 0.6);
    return vec2<f32>(d, select(0.0, 1.0, d1 < piso));
}

fn normal(p: vec3<f32>) -> vec3<f32> {
    let e = vec2<f32>(0.0015, -0.0015);
    return normalize(e.xyy * mapa(p + e.xyy).x + e.yyx * mapa(p + e.yyx).x +
                     e.yxy * mapa(p + e.yxy).x + e.xxx * mapa(p + e.xxx).x);
}

fn sombra(ro: vec3<f32>, rd: vec3<f32>) -> f32 {
    var res = 1.0;
    var t = 0.05;
    for (var i = 0; i < 32; i = i + 1) {
        let h = mapa(ro + rd * t).x;
        res = min(res, 10.0 * h / t);
        t = t + clamp(h, 0.03, 0.6);
        if (res < 0.002 || t > 14.0) { break; }
    }
    return clamp(res, 0.0, 1.0);
}

fn oclusion(p: vec3<f32>, n: vec3<f32>) -> f32 {
    var o = 0.0;
    var s = 1.0;
    for (var i = 1; i <= 5; i = i + 1) {
        let h = 0.08 * f32(i);
        o = o + (h - mapa(p + n * h).x) * s;
        s = s * 0.7;
    }
    return clamp(1.0 - 2.5 * o, 0.0, 1.0);
}

fn escena(frag: vec2<f32>) -> vec3<f32> {
    let uv = (frag - 0.5 * vec2<f32>(u.ancho, u.alto)) / u.alto * vec2<f32>(1.0, -1.0);
    let ro = vec3<f32>(2.0 + 0.8 * sin(u.tiempo * 0.3), 2.0 + 0.3 * sin(u.tiempo * 0.5), 0.0);
    var rd = normalize(vec3<f32>(uv.x, uv.y - 0.25, 1.3));
    let gx = rot(0.15 * sin(u.tiempo * 0.4)) * rd.xy;
    rd = normalize(vec3<f32>(gx.x, gx.y, rd.z));
    let cielo = mix(vec3<f32>(0.02, 0.05, 0.09), vec3<f32>(0.05, 0.22, 0.30), clamp(rd.y * 1.5 + 0.4, 0.0, 1.0));
    var t = 0.0;
    var m = -1.0;
    let pasos = i32(u.pasos);
    for (var i = 0; i < pasos; i = i + 1) {
        let h = mapa(ro + rd * t);
        if (h.x < 0.001 * t) { m = h.y; break; }
        t = t + h.x * 0.8;
        if (t > 40.0) { break; }
    }
    if (m < 0.0) { return cielo; }
    let p = ro + rd * t;
    let n = normal(p);
    let luz = normalize(vec3<f32>(0.6, 0.8, -0.4));
    var base = vec3<f32>(0.10, 0.12, 0.14);
    if (m > 0.5) {
        let k = hash3(round((p + vec3<f32>(0.0, 0.0, u.tiempo * 3.0)) / 4.0));
        base = mix(vec3<f32>(0.18, 0.65, 0.79), vec3<f32>(0.12, 0.75, 0.61), k);
    }
    let dif = clamp(dot(n, luz), 0.0, 1.0) * sombra(p + n * 0.01, luz);
    let esp = pow(clamp(dot(reflect(rd, n), luz), 0.0, 1.0), 32.0) * dif;
    let ao = oclusion(p, n);
    var col = base * (0.18 * ao + 1.1 * dif) + vec3<f32>(0.9) * esp * 0.6;
    col = col + base * 0.25 * clamp(-n.y, 0.0, 1.0);
    return mix(col, cielo, 1.0 - exp(-0.0025 * t * t));
}

fn letra(px: vec2<f32>) -> f32 {
    let s = u.escala;
    let q = px - vec2<f32>(16.0, 16.0);
    if (q.x < 0.0 || q.y < 0.0) { return -1.0; }
    let cx = u32(q.x / (6.0 * s));
    let cy = u32(q.y / (10.0 * s));
    if (cx >= 64u || cy >= 7u) { return -1.0; }
    let ch = texto[cy * 64u + cx];
    let gx = u32(q.x / s) % 6u;
    let gy = u32(q.y / s) % 10u;
    if (ch == 32u || gx >= 5u || gy >= 7u) { return 0.0; }
    return f32((fuente[ch * 8u + gy] >> (4u - gx)) & 1u);
}

@fragment
fn fs(@builtin(position) pos: vec4<f32>) -> @location(0) vec4<f32> {
    var col = escena(pos.xy);
    // panel de datos arriba a la izquierda
    let w = 16.0 + 64.0 * 6.0 * u.escala + 8.0;
    let h = 16.0 + 7.0 * 10.0 * u.escala + 4.0;
    if (pos.x > 8.0 && pos.x < w && pos.y > 8.0 && pos.y < h) {
        col = col * 0.35;
        let l = letra(pos.xy);
        if (l > 0.5) {
            let fila = u32((pos.y - 16.0) / (10.0 * u.escala));
            var tc = vec3<f32>(0.92, 0.95, 0.97);
            if (fila == 0u) { tc = vec3<f32>(0.18, 0.75, 0.88); }
            if (fila == 4u) { tc = select(vec3<f32>(0.30, 0.90, 0.55), vec3<f32>(1.0, 0.30, 0.25), u.alerta > 0.5); }
            if (fila == 6u) { tc = vec3<f32>(0.55, 0.60, 0.65); }
            col = tc;
        }
    }
    // barra de avance abajo
    if (pos.y > u.alto - 8.0) {
        let lleno = pos.x < u.ancho * u.progreso;
        col = select(vec3<f32>(0.08, 0.10, 0.12), select(vec3<f32>(0.12, 0.75, 0.61), vec3<f32>(1.0, 0.3, 0.25), u.alerta > 0.5), lleno);
    }
    return vec4<f32>(col, 1.0);
}
"""


def hay_pantalla() -> bool:
    if os.name == "nt" or os.uname().sysname == "Darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


class Ventana:
    def __init__(self, device, adapter, titulo: str, ancho: int = 1024, alto: int = 640):
        import glfw
        import wgpu
        self.glfw = glfw
        self.device = device
        if not glfw.init():
            raise RuntimeError("no se pudo iniciar glfw")
        glfw.window_hint(glfw.CLIENT_API, glfw.NO_API)
        glfw.window_hint(glfw.RESIZABLE, True)
        self.ventana = glfw.create_window(ancho, alto, titulo, None, None)
        if not self.ventana:
            glfw.terminate()
            raise RuntimeError("no se pudo crear la ventana")
        self.titulo = titulo
        self._icono()
        info = _present_info(glfw, self.ventana)
        self.ctx = wgpu.gpu.get_canvas_context(info)
        self.formato = self.ctx.get_preferred_format(adapter)
        self.ctx.configure(device=device, format=self.formato)
        self.tam = (0, 0)
        self._medir()
        bu = wgpu.BufferUsage
        self.uni = device.create_buffer(size=32, usage=bu.UNIFORM | bu.COPY_DST)
        fuente = tabla_fuente()
        self.buf_fuente = device.create_buffer(size=len(fuente), usage=bu.STORAGE | bu.COPY_DST)
        device.queue.write_buffer(self.buf_fuente, 0, fuente)
        self.buf_texto = device.create_buffer(size=COLUMNAS * LINEAS * 4, usage=bu.STORAGE | bu.COPY_DST)
        mod = device.create_shader_module(code=WGSL)
        fs = wgpu.ShaderStage.FRAGMENT
        bgl = device.create_bind_group_layout(entries=[
            {"binding": 0, "visibility": fs, "buffer": {"type": wgpu.BufferBindingType.uniform}},
            {"binding": 1, "visibility": fs, "buffer": {"type": wgpu.BufferBindingType.read_only_storage}},
            {"binding": 2, "visibility": fs, "buffer": {"type": wgpu.BufferBindingType.read_only_storage}}])
        self.grupo = device.create_bind_group(layout=bgl, entries=[
            {"binding": 0, "resource": {"buffer": self.uni, "offset": 0, "size": 32}},
            {"binding": 1, "resource": {"buffer": self.buf_fuente, "offset": 0, "size": self.buf_fuente.size}},
            {"binding": 2, "resource": {"buffer": self.buf_texto, "offset": 0, "size": self.buf_texto.size}}])
        self.pipe = device.create_render_pipeline(
            layout=device.create_pipeline_layout(bind_group_layouts=[bgl]),
            vertex={"module": mod, "entry_point": "vs", "buffers": []},
            primitive={"topology": wgpu.PrimitiveTopology.triangle_list},
            depth_stencil=None, multisample=None,
            fragment={"module": mod, "entry_point": "fs", "targets": [{"format": self.formato}]})
        self.t0 = time.perf_counter()
        self.cuadros = 0
        self._marcas: list[float] = []
        self.fps = 0.0
        self.pasos = 110.0                  # pasos del trazado de rayos: baja sola si la placa no llega
        glfw.show_window(self.ventana)
        glfw.focus_window(self.ventana)

    def _icono(self):
        try:
            from PIL import Image
            ruta = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "icono.png")
            self.glfw.set_window_icon(self.ventana, 1, [Image.open(ruta).convert("RGBA")])
        except Exception:  # noqa: BLE001 - el ícono es solo estético
            pass

    def _medir(self):
        tam = self.glfw.get_framebuffer_size(self.ventana)
        if tuple(tam) != self.tam:
            self.tam = (int(tam[0]), int(tam[1]))
            self.ctx.set_physical_size(*self.tam)

    @property
    def cerrada(self) -> bool:
        return bool(self.glfw.window_should_close(self.ventana))

    @property
    def fps_promedio(self) -> float:
        dt = time.perf_counter() - self.t0
        return self.cuadros / dt if dt > 0 else 0.0

    def cuadro(self, lineas: list[str], progreso: float, alerta: bool) -> None:
        glfw = self.glfw
        glfw.poll_events()
        if glfw.get_key(self.ventana, glfw.KEY_ESCAPE) == glfw.PRESS:
            glfw.set_window_should_close(self.ventana, True)
        self._medir()
        if self.tam[0] < 8 or self.tam[1] < 8:          # minimizada: no se dibuja
            return
        ahora = time.perf_counter()
        self._marcas = [m for m in self._marcas if ahora - m < 1.0] + [ahora]
        if len(self._marcas) > 1:
            self.fps = (len(self._marcas) - 1) / (self._marcas[-1] - self._marcas[0])
            if len(self._marcas) > 4:
                if self.fps < 8:
                    self.pasos = max(24.0, self.pasos * 0.9)
                elif self.fps > 40:
                    self.pasos = min(110.0, self.pasos + 1)
        escala = 2.0 if self.tam[0] >= 900 else 1.0
        self.device.queue.write_buffer(self.uni, 0, struct.pack(
            "<8f", ahora - self.t0, self.tam[0], self.tam[1], min(1.0, max(0.0, progreso)), 1.0 if alerta else 0.0, escala, self.pasos, 0.0))
        codigos = []
        for i in range(LINEAS):
            codigos += a_codigos(lineas[i] if i < len(lineas) else "")
        self.device.queue.write_buffer(self.buf_texto, 0, struct.pack(f"<{len(codigos)}I", *codigos))
        tex = self.ctx.get_current_texture()
        enc = self.device.create_command_encoder()
        rp = enc.begin_render_pass(color_attachments=[{"view": tex.create_view(), "resolve_target": None,
                                                       "clear_value": (0, 0, 0, 1), "load_op": "clear", "store_op": "store"}])
        rp.set_pipeline(self.pipe)
        rp.set_bind_group(0, self.grupo)
        rp.draw(3, 1, 0, 0)
        rp.end()
        self.device.queue.submit([enc.finish()])
        self.ctx.present()
        self.cuadros += 1
        if self.cuadros % 15 == 0:
            glfw.set_window_title(self.ventana, f"{self.titulo}  ·  {self.fps:.0f} FPS")

    def cerrar(self) -> None:
        try:
            self.ctx.unconfigure()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.glfw.destroy_window(self.ventana)
            self.glfw.terminate()
        except Exception:  # noqa: BLE001
            pass


def _present_info(glfw, ventana) -> dict:
    import sys
    if sys.platform.startswith("win"):
        return {"method": "screen", "platform": "windows", "window": int(glfw.get_win32_window(ventana))}
    if sys.platform.startswith("darwin"):
        return {"method": "screen", "platform": "cocoa", "window": int(glfw.get_cocoa_window(ventana))}
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wl = glfw.get_wayland_display() if hasattr(glfw, "get_wayland_display") else None
        if wl:
            return {"method": "screen", "platform": "wayland", "window": int(glfw.get_wayland_window(ventana)), "display": int(wl)}
        x11 = glfw.get_x11_display() if hasattr(glfw, "get_x11_display") else None
    if x11:
        return {"method": "screen", "platform": "x11", "window": int(glfw.get_x11_window(ventana)), "display": int(x11)}
    raise RuntimeError("no hay una pantalla X11 ni Wayland disponible")
