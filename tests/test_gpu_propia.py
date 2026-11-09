"""La prueba de placa de video propia: evaluación de resultados (sin GPU) y el proceso real (si hay alguna placa)."""
import json
import subprocess
import sys

import pytest

from openstd.gpu import estres
from openstd.pruebas import gpu
from openstd.resultado import FALLA, NO_EVALUADO, PASA


def _pase(**k):
    d = {"tipo": "pase", "pases": 3, "errores_vram": 0, "errores_calculo": 0, "errores_cpu": 0, "bytes_vram": 512 * 2 ** 20,
         "despachos": 18, "muestras_cpu": 12}
    d.update(k)
    return d


INICIO = {"tipo": "inicio", "api": "Vulkan", "nombre": "GPU X", "driver": "1.0", "vram": 512 * 2 ** 20}


def test_sin_errores_pasa():
    r = gpu.evaluar_propia("Vulkan", [INICIO, _pase(), dict(_pase(), tipo="fin")], 0, False, [])
    assert r.veredicto == PASA and r.titulo == "Placa de video (Vulkan)"
    assert any("512 MB" in l for l in r.lineas)


def test_errores_de_vram():
    r = gpu.evaluar_propia("OpenGL", [INICIO, dict(_pase(errores_vram=7), tipo="fin")], 0, False, [])
    assert r.veredicto == FALLA and "VRAM" in r.motivo and r.acciones


def test_calculos_distintos():
    for k in ("errores_calculo", "errores_cpu"):
        r = gpu.evaluar_propia("OpenGL", [INICIO, dict(_pase(**{k: 1}), tipo="fin")], 0, False, [])
        assert r.veredicto == FALLA and "inestable" in r.motivo


def test_driver_que_se_cuelga():
    msgs = [INICIO, _pase(), {"tipo": "error", "fase": "carga", "texto": "Device lost"}]
    assert gpu.evaluar_propia("Vulkan", msgs, 4, False, []).veredicto == FALLA
    # cuelgue registrado por el sistema (TDR) aunque la prueba haya terminado
    r = gpu.evaluar_propia("Vulkan", [INICIO, dict(_pase(), tipo="fin")], 0, False, ["Display evento 4101"])
    assert r.veredicto == FALLA


def test_errores_del_programa_no_son_falla():
    r = gpu.evaluar_propia("Vulkan", [{"tipo": "error", "fase": "inicio", "texto": "no hay ninguna placa con Vulkan"}], 3, False, [])
    assert r.veredicto == NO_EVALUADO
    r = gpu.evaluar_propia("Vulkan", [INICIO, _pase(), {"tipo": "error", "fase": "carga", "texto": "KeyError: 'x'"}], 4, False, [])
    assert r.veredicto == NO_EVALUADO
    assert gpu.evaluar_propia("Vulkan", [], -9, False, []).veredicto == NO_EVALUADO
    assert gpu.evaluar_propia("Vulkan", [INICIO, _pase()], None, True, []).veredicto == NO_EVALUADO


def test_opciones():
    hw = [{"api": "Vulkan", "nombre": "A", "software": False}, {"api": "D3D12", "nombre": "A", "software": False},
          {"api": "D3D12", "nombre": "WARP", "software": True}]
    ops = gpu.opciones_propias(hw)
    assert [o["id"] for o in ops] == ["todas", "api:D3D12", "api:Vulkan"]
    assert "DirectX 12" in ops[0]["nombre"] and len(ops[0]["adaptadores"]) == 2
    assert gpu.opciones_propias([hw[2]]) == []


def test_cuenta_entera_igual_que_en_la_gpu():
    # valores conocidos del hash PCG (los mismos que da el shader)
    assert estres.pcg(0) == 129708002 and estres.pcg(1) == 2831084092
    assert estres.entero_cpu(5, 77, 0) == estres.pcg(5 ^ 77)


# ============================================================================ con una placa real (o por software)
def _adaptadores():
    p = subprocess.run([sys.executable, "-m", "openstd.gpu.estres", "--listar"], capture_output=True, timeout=120)
    for linea in p.stdout.decode().splitlines():
        d = json.loads(linea)
        if d.get("tipo") == "adaptadores":
            return d["lista"]
    return []


@pytest.fixture(scope="module")
def adaptador():
    pytest.importorskip("wgpu")
    lista = _adaptadores()
    if not lista:
        pytest.skip("no hay ninguna placa de video (ni por software) en esta máquina")
    return sorted(lista, key=lambda a: a["software"])[0]


def _correr(api, *extra):
    p = subprocess.run([sys.executable, "-m", "openstd.gpu.estres", "--api", api, "--segundos", "2", "--mb", "64", *extra],
                       capture_output=True, timeout=300)
    return [json.loads(l) for l in p.stdout.decode().splitlines() if l.startswith("{")]


@pytest.mark.lento
def test_proceso_real_sin_errores(adaptador):
    msgs = _correr(adaptador["api"])
    fin = [m for m in msgs if m["tipo"] == "fin"]
    assert fin, msgs
    assert fin[0]["pases"] >= 1 and fin[0]["errores_vram"] == 0 and fin[0]["errores_calculo"] == 0 and fin[0]["errores_cpu"] == 0


@pytest.mark.lento
def test_proceso_real_detecta_errores(adaptador):
    fin = [m for m in _correr(adaptador["api"], "--simular-error") if m["tipo"] == "fin"]
    assert fin and fin[0]["errores_vram"] > 0


# ---------------------------------------------------------------- ventana con la escena 3D
def test_fps_de_la_ventana_en_el_reporte():
    fin = dict(_pase(fps=42.5, resolucion="1024x640"), tipo="fin")
    r = gpu.evaluar_propia("Vulkan", [INICIO, fin], 0, False, [])
    assert r.veredicto == PASA
    assert any("42 FPS" in l and "1024x640" in l for l in r.lineas)


def test_cerrar_la_ventana_no_es_pasa_ni_falla():
    fin = dict(_pase(ventana_cerrada=True), tipo="fin")
    r = gpu.evaluar_propia("Vulkan", [INICIO, fin], 0, False, [])
    assert r.veredicto == NO_EVALUADO and "ventana" in r.motivo


def test_cerrar_la_ventana_con_errores_sigue_siendo_falla():
    fin = dict(_pase(ventana_cerrada=True, errores_vram=3), tipo="fin")
    assert gpu.evaluar_propia("Vulkan", [INICIO, fin], 0, False, []).veredicto == FALLA


def test_sin_ventana_se_avisa_y_se_evalua_igual():
    aviso = {"tipo": "aviso", "texto": "No se pudo abrir la ventana de la prueba (no hay pantalla); sigue sin ventana"}
    r = gpu.evaluar_propia("Vulkan", [INICIO, aviso, dict(_pase(), tipo="fin")], 0, False, [])
    assert r.veredicto == PASA and any("No se pudo abrir la ventana" in l for l in r.lineas)


def test_letra_de_la_ventana():
    from openstd.gpu import ventana
    c = ventana.a_codigos("Memoria de vídeo · 45°C ñ ¿?")
    assert len(c) == ventana.COLUMNAS
    texto = "".join(chr(x) for x in c).rstrip()
    assert texto.startswith("MEMORIA DE VIDEO \x01 45\x02C N")
    assert all(x == 32 or chr(x) in ventana.FUENTE for x in c)
    for ch, filas in ventana.FUENTE.items():
        v = [int(f, 16) for f in filas.split()]
        assert len(v) == 7 and all(0 <= x < 32 for x in v), ch
    assert len(ventana.tabla_fuente()) == 128 * 8 * 4


@pytest.mark.lento
def test_proceso_real_con_ventana(adaptador):
    """Con pantalla abre la ventana y mide FPS; sin pantalla avisa y sigue igual. En los dos casos la prueba termina."""
    msgs = _correr(adaptador["api"], "--ventana")
    fin = [m for m in msgs if m["tipo"] == "fin"]
    assert fin, msgs
    assert fin[0]["errores_vram"] == 0 and fin[0]["errores_calculo"] == 0
    assert "fps" in fin[0] or any(m["tipo"] == "aviso" for m in msgs)
