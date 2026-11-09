import time

from openstd.plataforma.base import eventos_vacios, numero_hex
from openstd.plataforma.linux import clasificar_linea, interpretar_journal, interpretar_kernel
from openstd.plataforma.windows import interpretar_eventos, lista
from openstd.pruebas import sistema
from openstd.resultado import FALLA, NO_EVALUADO, PASA

AHORA = 1_800_000_000.0
DESDE = AHORA - 30 * 86400
VOL = [{"nombre": "C:\\", "total": 500e9, "libre": 200e9, "sistema": True}]


def _ev(**k):
    e = eventos_vacios(True, fuente="prueba")
    e.update(k)
    return e


def test_sin_errores_pasa():
    r = sistema.evaluar(_ev(), VOL, DESDE, ahora=AHORA)
    assert r.veredicto == PASA and "30 días" in r.motivo


def test_no_disponible():
    r = sistema.evaluar(eventos_vacios(False, "sin permisos"), VOL, DESDE, ahora=AHORA)
    assert r.veredicto == NO_EVALUADO and r.motivo == "sin permisos"


def test_pantallazo_con_causa():
    r = sistema.evaluar(_ev(pantallazos=[{"t": AHORA - 100, "codigo": 0x124, "texto": "pantallazo azul"},
                                         {"t": AHORA - 50, "codigo": 0x124, "texto": "pantallazo azul"}]), VOL, DESDE, ahora=AHORA)
    assert r.veredicto == FALLA
    assert any("WHEA_UNCORRECTABLE_ERROR: 2 veces" in l and "procesador, RAM" in l for l in r.lineas)
    assert r.acciones


def test_apagados():
    uno = sistema.evaluar(_ev(apagados=[{"t": AHORA, "texto": ""}]), VOL, DESDE, ahora=AHORA)
    assert uno.veredicto == PASA and uno.advertencias
    tres = sistema.evaluar(_ev(apagados=[{"t": AHORA, "texto": ""}] * 3), VOL, DESDE, ahora=AHORA)
    assert tres.veredicto == FALLA


def test_hardware_grave_y_leve():
    leve = sistema.evaluar(_ev(hardware=[{"t": AHORA, "texto": "corregido", "grave": False}] * 4), VOL, DESDE, ahora=AHORA)
    assert leve.veredicto == PASA and any("corregidos" in a for a in leve.advertencias)
    grave = sistema.evaluar(_ev(hardware=[{"t": AHORA, "texto": "MCE fatal", "grave": True}]), VOL, DESDE, ahora=AHORA)
    assert grave.veredicto == FALLA


def test_disco_y_espacio_y_drivers():
    r = sistema.evaluar(_ev(disco=[{"t": AHORA, "texto": "reintento", "grave": False}] * 5,
                            drivers=[{"nombre": "Wi-Fi", "codigo": 28}]),
                        [{"nombre": "C:\\", "total": 500e9, "libre": 5e9, "sistema": True}], DESDE, ahora=AHORA)
    assert r.veredicto == PASA
    textos = " ".join(r.advertencias)
    assert "errores leves de disco" in textos and "poco espacio" in textos and "1 dispositivo" in textos
    r = sistema.evaluar(_ev(disco=[{"t": AHORA, "texto": "bloque dañado", "grave": True}]), VOL, DESDE, ahora=AHORA)
    assert r.veredicto == FALLA


def test_contar_desde_hoy_cambia_el_periodo(ctx):
    clave = ctx.plat.clave_equipo(ctx.equipo)
    hace_2_dias = time.time() - 2 * 86400
    ctx.config.poner_referencia(clave, hace_2_dias, "cambio de RAM")
    r = sistema.ejecutar(ctx)
    assert any(f"eventos:{int(hace_2_dias)}" == x for x in ctx.plat.llamadas)
    assert any("cambio de RAM" in l for l in r.lineas)


# ============================================================================ Windows
def test_eventos_de_windows():
    d = {"bsod": [{"t": 1, "id": 1001, "p": "WER", "a": ["0x0000007e (0xffff)", "x"]}],
         "kp41": [{"t": 2, "a": ["0"]}, {"t": 3, "a": ["126"]}],
         "whea": {"t": 4, "id": 17, "l": 3},                          # un solo evento llega como objeto suelto
         "disco": [{"t": 5, "id": 7, "p": "disk"}, {"t": 6, "id": 153, "p": "stornvme"}],
         "ntfs": {}, "tdr": [], "video": {}, "memoria": {},              # vacíos de PowerShell
         "drivers": [{"n": "Wi-Fi", "c": 28}, {"n": "Deshabilitado", "c": 22}]}
    ev = interpretar_eventos(d)
    assert ev["disponible"]
    assert ev["pantallazos"][0]["codigo"] == 0x7E
    assert len(ev["apagados"]) == 1                # el 41 con código de pantallazo ya está contado
    assert ev["hardware"] == [{"t": 4, "texto": "error de hardware WHEA (evento 17)", "grave": False}]
    assert [x["grave"] for x in ev["disco"]] == [True, False]
    assert ev["video"] == [] and ev["memoria"] == []
    assert ev["drivers"] == [{"nombre": "Wi-Fi", "codigo": 28}]
    assert interpretar_eventos(None)["disponible"] is False


def test_lista_de_powershell():
    assert lista(None) == [] and lista({}) == [] and lista({"a": 1}) == [{"a": 1}] and lista([{}, {"a": 1}]) == [{"a": 1}]
    assert numero_hex("0x0000007e (0x1)") == 0x7E and numero_hex("126") == 126 and numero_hex("") is None


# ============================================================================ Linux
def test_lineas_del_kernel():
    casos = {
        "mce: [Hardware Error]: Machine check events logged": "hardware_leve",
        "mce: [Hardware Error]: CPU 0: Machine Check: 0 Bank 5: uncorrected error": "hardware_grave",
        "EDAC MC0: 1 CE memory read error on CPU_SrcID#0": "hardware_leve",
        "EDAC MC0: 1 UE memory read error": "hardware_grave",
        "blk_update_request: I/O error, dev sda, sector 12345 op 0x0:(READ)": "disco_grave",
        "I/O error, dev loop0, sector 0": None,
        "Buffer I/O error on dev sr0, logical block 0": None,
        "ata1.00: exception Emask 0x0 SAct 0x0 SErr 0x0 action 0x0": "disco_leve",
        "ata1.00: error: { UNC }": "disco_grave",
        "nvme nvme0: I/O 12 QID 3 timeout, aborting": "disco_leve",
        "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.": "video",
        "amdgpu 0000:03:00.0: amdgpu: GPU reset begin!": "video",
        "Out of memory: Killed process 1234 (chrome)": "memoria",
        "usb 1-1: new high-speed USB device": None,
    }
    for linea, esperado in casos.items():
        assert clasificar_linea(linea) == esperado, linea


def test_journal_y_periodo():
    salida = ("1799999000.5 pc kernel: EDAC MC0: 1 UE memory read error\n"
              "1700000000.0 pc kernel: EDAC MC0: 1 UE memory read error\n"
              "basura sin fecha\n"
              "1799999100.0 pc kernel: NVRM: Xid 79\n")
    lineas = interpretar_journal(salida)
    assert len(lineas) == 3
    ev = interpretar_kernel(lineas, DESDE)
    assert len(ev["hardware"]) == 1 and ev["hardware"][0]["grave"] and len(ev["video"]) == 1
    r = sistema.evaluar(ev, VOL, DESDE, ahora=AHORA)
    assert r.veredicto == FALLA
