import itertools

import pytest

from openstd.pruebas import disco
from openstd.resultado import FALLA, NO_EVALUADO, PASA, Progreso
from openstd.util import ES_LINUX

MB = 1024 * 1024


class LectorFalso:
    """Simula un disco: los bloques de `ilegibles` dan error y los de `lentos` tardan `ms_lento` ms."""

    sin_cache = True

    def __init__(self, ilegibles=(), lentos=(), ms_lento=700):
        self.ilegibles, self.lentos, self.ms_lento = set(ilegibles), set(lentos), ms_lento
        self.t = 0.0
        self.lecturas = 0

    def leer(self, offset, buf):
        self.lecturas += 1
        b = offset // disco.BLOQUE
        self.t += (self.ms_lento if b in self.lentos else 1) / 1000
        if b in self.ilegibles:
            raise OSError(5, "Error de E/S")
        return len(buf)

    def reloj(self):
        return self.t

    def cerrar(self):
        pass


def _escanear(lector, tam, completo=False):
    pasos = itertools.count()
    return disco.escanear(lector, tam, 512, completo, Progreso(), reloj=lambda: lector.t + next(pasos) * 0)


def test_superficie_sana():
    e = _escanear(LectorFalso(), 64 * MB)
    assert e["completo"] and e["leidos"] == 512 and e["conteo"][7] == 0
    L, A, F = disco.evaluar_superficie(e)
    assert F == [] and A == []
    assert e["curva"]


def test_bloques_ilegibles_son_falla():
    e = _escanear(LectorFalso(ilegibles={10, 11}), 64 * MB)
    assert e["conteo"][7] == 2
    _, _, F = disco.evaluar_superficie(e)
    assert any("ilegibles" in f for f in F)


def test_bloques_lentos():
    _, A, F = disco.evaluar_superficie(_escanear(LectorFalso(lentos={5}), 64 * MB))
    assert F == [] and any("600 ms" in a for a in A)
    _, _, F = disco.evaluar_superficie(_escanear(LectorFalso(lentos={5, 50, 100}), 64 * MB))
    assert any("600 ms" in f for f in F)


def test_se_detiene_si_el_disco_no_responde():
    e = _escanear(LectorFalso(ilegibles=set(range(100, 400))), 64 * MB)
    assert "seguidos" in e["detenido"]
    assert e["leidos"] < 512


def test_modo_rapido_lee_zonas_repartidas():
    lector = LectorFalso()
    tam = 10 * 1024 * MB                       # 10 GB: más que 256 zonas de 16 MB
    e = _escanear(lector, tam)
    assert not e["completo"]
    assert e["leidos"] == disco.ZONAS_RAPIDO * disco.BLOQUES_ZONA


def test_cancelar():
    p = Progreso()
    p.cancelar.set()
    e = disco.escanear(LectorFalso(), 64 * MB, 512, False, p)
    assert e["detenido"] == "cancelado"


# ============================================================================ SMART
SANO = {"device": {"protocol": "ATA"}, "model_name": "SSD X", "serial_number": "S1", "rotation_rate": 0,
        "smart_status": {"passed": True}, "power_on_time": {"hours": 1000}, "power_cycle_count": 50,
        "temperature": {"current": 35},
        "ata_smart_attributes": {"table": [{"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "thresh": 10, "raw": {"value": 0}},
                                           {"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0, "raw": {"value": 0}},
                                           {"id": 231, "name": "SSD_Life_Left", "value": 95, "thresh": 0, "raw": {"value": 95}}]}}


def test_smart_sano():
    L, A, F = disco.evaluar_smart(SANO)
    assert F == [] and A == []
    assert any("Vida útil restante estimada: 95" in l for l in L)


def test_smart_con_sectores_pendientes():
    j = {**SANO, "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 90, "thresh": 10, "raw": {"value": 8}},
        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "thresh": 0, "raw": {"value": 3}},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "thresh": 0, "raw": {"value": 12}}]}}
    _, A, F = disco.evaluar_smart(j)
    assert any("pendientes" in f for f in F)
    assert any("reemplazó 8 sectores" in a for a in A)
    assert any("cable SATA" in a for a in A)


def test_smart_failed_y_umbral():
    j = {**SANO, "smart_status": {"passed": False},
         "ata_smart_attributes": {"table": [{"id": 1, "name": "Raw_Read_Error_Rate", "value": 5, "thresh": 6, "when_failed": "now",
                                             "raw": {"value": 0}}]}}
    _, _, F = disco.evaluar_smart(j)
    assert len(F) == 2


def test_smart_nvme():
    j = {"device": {"protocol": "NVMe"}, "smart_status": {"passed": True},
         "nvme_smart_health_information_log": {"critical_warning": 0, "available_spare": 100, "available_spare_threshold": 10,
                                               "percentage_used": 95, "media_errors": 0, "data_units_written": 2_000_000}}
    L, A, F = disco.evaluar_smart(j)
    assert F == [] and any("95 %" in a for a in A)
    j["nvme_smart_health_information_log"].update(critical_warning=4, media_errors=2, available_spare=5)
    _, _, F = disco.evaluar_smart(j)
    assert len(F) == 3


def test_salud_segun_windows():
    _, _, F = disco.evaluar_salud_so({"salud": "Unhealthy", "estado": "OK", "rel": {"lect_inc": 2}})
    assert len(F) == 2
    L, A, F = disco.evaluar_salud_so({"salud": "Healthy", "estado": "OK", "rel": None})
    assert F == [] and A == []


# ============================================================================ prueba completa
def test_prueba_completa_sobre_un_archivo(ctx, tmp_path):
    img = tmp_path / "disco.img"
    img.write_bytes(b"\0" * (32 * MB))
    d = dict(ctx.equipo["discos"][0], ruta=str(img), tam=32 * MB)
    r = disco.ejecutar(ctx, d)
    assert r.veredicto == PASA, r.a_texto()
    assert r.grafico and r.grafico["tipo"] == "velocidad_disco"
    assert any("sin datos SMART" in r.motivo for _ in [0])


def test_sin_permisos_es_no_evaluado(ctx, tmp_path):
    d = dict(ctx.equipo["discos"][0], ruta=str(tmp_path / "no-existe"), tam=32 * MB)
    r = disco.ejecutar(ctx, d)
    assert r.veredicto == NO_EVALUADO


def test_superficie_mala_es_falla(ctx, monkeypatch):
    monkeypatch.setattr(ctx.plat, "abrir_disco", lambda d: LectorFalso(ilegibles={3}))
    d = dict(ctx.equipo["discos"][0], tam=32 * MB)
    r = disco.ejecutar(ctx, d)
    assert r.veredicto == FALLA
    assert r.acciones


@pytest.mark.skipif(not ES_LINUX, reason="lectura directa de Linux")
def test_lector_linux_sin_cache(tmp_path):
    from openstd.plataforma.linux import LectorLinux
    img = tmp_path / "disco.img"
    img.write_bytes(bytes(range(256)) * (4 * MB // 256))
    lector = LectorLinux(str(img))
    buf = bytearray(disco.BLOQUE)
    try:
        assert lector.leer(disco.BLOQUE, buf) == disco.BLOQUE
        assert buf[:4] == bytes([0, 1, 2, 3])
    finally:
        lector.cerrar()
