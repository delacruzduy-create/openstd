"""Pruebas reales de CPU y RAM (cortas) y de GPU con una herramienta simulada."""
import sys

import pytest

from openstd.pruebas import cierre_por_hardware, cpu, gpu, ram
from openstd.resultado import FALLA, NO_EVALUADO, PASA


@pytest.mark.lento
def test_cpu_sana(ctx):
    r = cpu.ejecutar(ctx, 2, hilos=2)
    assert r.veredicto == PASA, r.a_texto()
    assert any("0 errores" in l for l in r.lineas)


@pytest.mark.lento
def test_cpu_que_calcula_mal_es_falla(ctx):
    r = cpu.ejecutar(ctx, 2, hilos=1, _fallar=True)
    assert r.veredicto == FALLA
    assert any("coma flotante" in l for l in r.lineas)


@pytest.mark.lento
def test_cpu_cancelada(ctx):
    ctx.progreso.cancelar.set()
    assert cpu.ejecutar(ctx, 30, hilos=1).veredicto == NO_EVALUADO


@pytest.mark.lento
def test_ram_sana(ctx):
    r = ram.ejecutar(ctx, 1, mb_max=96)
    assert r.veredicto == PASA, r.a_texto()


@pytest.mark.lento
def test_ram_con_un_bit_dado_vuelta_es_falla(ctx):
    r = ram.ejecutar(ctx, 1, mb_max=64, _corromper=True)
    assert r.veredicto == FALLA, r.a_texto()
    assert any("esperado" in l and "1 bit" in l for l in r.lineas)


def test_comparar_memoria():
    a = bytes(range(256)) * 4
    b = bytearray(a)
    assert ram.comparar(a, bytes(b), "x") is None
    b[10] ^= 0x03
    e = ram.comparar(memoryview(b), a, "0.1.2")
    assert "byte 10" in e and "2 bits" in e and "1 byte mal" in e


def test_patrones_cambian_de_lugar():
    refs = [bytes([i]) for i in range(20)]
    assert ram.patron(refs, 0, 0) != ram.patron(refs, 0, 1)
    assert ram.patron(refs, 5, 2) in refs[16:]


def test_cierres_de_hardware():
    assert cierre_por_hardware(0) is None and cierre_por_hardware(1) is None and cierre_por_hardware(-9) is None
    assert cierre_por_hardware(-11) == "acceso inválido a memoria"
    assert "C0000005" in cierre_por_hardware(0xC0000005)
    assert cierre_por_hardware(0xC000013A) is None          # Ctrl+C


# ============================================================================ GPU
def _op(segundos, codigo=0):
    return {"id": 0, "nombre": "Herramienta simulada", "cwd": None, "entorno": {},
            "cmd": [sys.executable, "-c", f"import time, sys; time.sleep({segundos}); sys.exit({codigo})"]}


def test_gpu_completa(ctx):
    r = gpu.ejecutar_externa(ctx, 5, _op(30))
    assert r.veredicto == PASA, r.a_texto()


def test_gpu_herramienta_que_se_cierra(ctx):
    r = gpu.ejecutar_externa(ctx, 5, _op(0.5, 3))
    assert r.veredicto == NO_EVALUADO and "código 3" in r.motivo


def test_gpu_con_cuelgues_del_driver(ctx):
    ctx.plat._gpu_errores = ["Display evento 4101"]
    r = gpu.ejecutar_externa(ctx, 5, _op(30))
    assert r.veredicto == FALLA and r.acciones


def test_gpu_sin_herramienta(ctx):
    r = gpu.ejecutar_externa(ctx, 5, None)
    assert r.veredicto == NO_EVALUADO and "FurMark" in r.motivo


def test_gpu_sin_driver(ctx, monkeypatch):
    monkeypatch.setattr(ctx.plat, "gpu_preparar", lambda: {"problema": "No hay aceleración 3D", "aviso": None, "entorno": {}, "lineas": []})
    r = gpu.ejecutar_externa(ctx, 5, _op(30))
    assert r.veredicto == NO_EVALUADO and r.acciones


def test_comando_propio(plat):
    ops = plat.gpu_opciones([], '"/opt/mi prueba/carga" --pantalla-completa')
    assert ops[0]["nombre"].startswith("Comando propio")
    assert ops[0]["cmd"][-1] == "--pantalla-completa"
