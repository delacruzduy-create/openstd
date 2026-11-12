"""Reparar Windows (versión Pro): se simulan las salidas de DISM, SFC y CHKDSK en los idiomas de Windows,
así nada toca el sistema real."""
import sys
import time

import pypdf
import pytest

from openstd import reparar, reporte
from openstd.diagnostico import SOLO_PRO_REPARAR, Diagnostico
from openstd.resultado import FALLA, NO_EVALUADO, PASA, Resultado
from tests.conftest import PlataformaFalsa, activar_pro


class WindowsFalso(PlataformaFalsa):
    nombre = "Windows"


def _salidas(monkeypatch, respuestas: dict, llamadas: list | None = None):
    """respuestas: {"dism /CheckHealth": (código, salida), ...}; la clave es el programa + el último argumento."""
    def falso(cmd, ctx, timeout=3600):
        clave = cmd[0].replace(".exe", "") + " " + cmd[-1]
        if llamadas is not None:
            llamadas.append(clave)
        return respuestas.get(clave, (0, ""))
    monkeypatch.setattr(reparar, "correr_vivo", falso)


# ============================================================================ piezas
def test_decodificar_utf16_de_sfc_y_porcentaje():
    texto = "Verificación 45% completada.\r\nVerificación 100% completada."
    assert reparar._decodificar(texto.encode("utf-16-le")) == texto
    assert reparar._decodificar(b"\x00" + texto.encode("utf-16-le")[:-1]).startswith("Verificaci")
    assert reparar._ultimo_porcentaje("[====   25,5% ]  [===== 61.2%  ]") == 61.2
    assert reparar._ultimo_porcentaje("Stage: 99%; Total: 30%; ETA: 0:01:00") == 30
    assert reparar._ultimo_porcentaje("sin números") is None


def test_correr_vivo_pasa_el_avance_y_se_puede_cancelar(ctx):
    cod, salida = reparar.correr_vivo([sys.executable, "-c", "print('10%'); print('80%'); print('fin')"], ctx, 30)
    assert cod == 0 and "fin" in salida
    ctx.progreso.cancelar.set()
    inicio = time.time()
    cod, _ = reparar.correr_vivo([sys.executable, "-c", "import time; time.sleep(30)"], ctx, 60)
    assert cod == -3 and time.time() - inicio < 10
    cod, _ = reparar.correr_vivo(["programa-que-no-existe-123"], ctx)
    assert cod == -1


def test_cbs_log(tmp_path):
    ahora = time.time()
    viejo = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ahora - 3600))
    nuevo = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ahora + 1))
    log = tmp_path / "CBS.log"
    log.write_text(
        f"{viejo}, Info  CSI  00000001 [SR] Repairing corrupted file \\??\\C:\\WINDOWS\\System32\\viejo.dll from store\n"
        f"{nuevo}, Info  CSI  00000002 [SR] Repairing corrupted file \\??\\C:\\WINDOWS\\System32\\drivers\\ntfs.sys from store\n"
        f"{nuevo}, Info  CSI  00000003 [SR] Cannot repair member file [l:10]'shell32.dll' of Microsoft-Windows-Shell\n"
        f"{nuevo}, Info  CSI  00000004 [SR] Verify complete\n", encoding="utf-8")
    r = reparar.cbs_sfc(ahora, str(log))
    assert r == {"leido": True, "reparados": ["ntfs.sys"], "sin_reparar": ["shell32.dll"]}
    assert reparar.cbs_sfc(ahora, str(tmp_path / "no-existe.log"))["leido"] is False


# ============================================================================ SFC
@pytest.mark.parametrize("salida,veredicto,reparo", [
    ("Protección de recursos de Windows no encontró ninguna infracción de integridad.", PASA, False),
    ("Windows Resource Protection found corrupt files and successfully repaired them.", PASA, True),
    ("A Proteção de Recursos do Windows encontrou arquivos corrompidos e os reparou com êxito.", PASA, True),
    ("Protección de recursos de Windows encontró archivos dañados, pero no pudo reparar algunos de ellos.", FALLA, False),
    ("Windows Resource Protection could not perform the requested operation.", NO_EVALUADO, False),
])
def test_sfc_en_varios_idiomas(ctx, monkeypatch, salida, veredicto, reparo):
    _salidas(monkeypatch, {"sfc /scannow": (0, salida)})
    monkeypatch.setattr(reparar, "cbs_sfc", lambda desde: {"leido": False, "reparados": [], "sin_reparar": []})
    r = reparar.sfc(ctx)
    assert r.veredicto == veredicto, r.motivo
    assert r.grafico["reparo"] is reparo


def test_sfc_sin_texto_conocido_usa_cbs(ctx, monkeypatch):
    _salidas(monkeypatch, {"sfc /scannow": (0, "texto en otro idioma")})
    monkeypatch.setattr(reparar, "cbs_sfc", lambda desde: {"leido": True, "reparados": ["a.dll"], "sin_reparar": []})
    r = reparar.sfc(ctx)
    assert r.veredicto == PASA and r.grafico["reparo"] and "a.dll" in r.lineas[0]


def test_sfc_pendiente_y_cancelado(ctx, monkeypatch):
    monkeypatch.setattr(reparar, "cbs_sfc", lambda desde: {"leido": False, "reparados": [], "sin_reparar": []})
    _salidas(monkeypatch, {"sfc /scannow": (0, "There is a system repair pending which requires reboot to complete.")})
    r = reparar.sfc(ctx)
    assert r.veredicto == NO_EVALUADO and r.grafico["reinicio"]
    _salidas(monkeypatch, {"sfc /scannow": (-3, "")})
    assert reparar.sfc(ctx).motivo == "Se canceló antes de terminar"


# ============================================================================ DISM
def test_dism_sano(ctx, monkeypatch):
    _salidas(monkeypatch, {"dism /CheckHealth": (0, "No se detectaron daños en el almacén de componentes."),
                           "dism /RestoreHealth": (0, "La operación de restauración se completó correctamente.")})
    r = reparar.dism(ctx)
    assert r.veredicto == PASA and not r.grafico["reparo"]


def test_dism_reparado(ctx, monkeypatch):
    _salidas(monkeypatch, {"dism /CheckHealth": (0, "The component store is repairable."),
                           "dism /RestoreHealth": (0, "The restore operation completed successfully.")})
    r = reparar.dism(ctx)
    assert r.veredicto == PASA and r.grafico["reparo"]


def test_dism_sin_internet(ctx, monkeypatch):
    _salidas(monkeypatch, {"dism /CheckHealth": (0, "O repositório de componentes pode ser reparado."),
                           "dism /RestoreHealth": (0x800F081F, "Error: 0x800f081f")})
    r = reparar.dism(ctx)
    assert r.veredicto == FALLA and "internet" in r.motivo and r.acciones
    _salidas(monkeypatch, {"dism /RestoreHealth": (0x800F081F, "Error: 0x800f081f")})
    assert reparar.dism(ctx).veredicto == NO_EVALUADO


# ============================================================================ CHKDSK, red, arranque
@pytest.mark.parametrize("cod,veredicto,reparo", [(0, PASA, False), (1, PASA, True), (3, PASA, True), (9, NO_EVALUADO, False)])
def test_chkdsk(ctx, monkeypatch, cod, veredicto, reparo):
    llamadas = []
    _salidas(monkeypatch, {"chkdsk /scan": (cod, "")}, llamadas)
    r = reparar.chkdsk(ctx)
    assert r.veredicto == veredicto and r.grafico["reparo"] is reparo
    assert ("fsutil C:" in llamadas or "fsutil " + reparar.os.environ.get("SystemDrive", "C:") in llamadas) == (cod == 3)


def test_red_pide_reinicio(ctx, monkeypatch):
    _salidas(monkeypatch, {"netsh reset": (0, "")})
    r = reparar.red(ctx)
    assert r.veredicto == PASA and r.grafico["reinicio"]


def test_arranque_guarda_copia_antes(ctx, monkeypatch):
    llamadas = []
    _salidas(monkeypatch, {}, llamadas)
    r = reparar.arranque(ctx)
    assert r.veredicto == PASA and llamadas[0].startswith("bcdedit") and llamadas[1].startswith("bcdboot")
    llamadas.clear()
    monkeypatch.setattr(reparar, "correr_vivo", lambda cmd, ctx, timeout=0: (1, "acceso denegado"))
    r = reparar.arranque(ctx)
    assert r.veredicto == NO_EVALUADO and "no se tocó nada" in r.motivo


def test_punto_de_restauracion(ctx, monkeypatch):
    monkeypatch.setattr(reparar, "_ps", lambda s, t=0: {"antes": 2, "despues": 3})
    assert reparar.punto(ctx).veredicto == PASA
    monkeypatch.setattr(reparar, "_ps", lambda s, t=0: {"antes": 3, "despues": 3, "ultimo": time.time() - 3600})
    assert "hoy" in reparar.punto(ctx).motivo
    monkeypatch.setattr(reparar, "_ps", lambda s, t=0: {"antes": 0, "despues": 0, "error": "deshabilitado"})
    assert reparar.punto(ctx).veredicto == NO_EVALUADO


def test_windows_update(ctx, monkeypatch):
    monkeypatch.setattr(reparar, "_ps", lambda s, t=0: {"renombradas": ["SoftwareDistribution.old_1", "catroot2.old_1"], "errores": []})
    assert reparar.wu(ctx).veredicto == PASA
    monkeypatch.setattr(reparar, "_ps", lambda s, t=0: {"renombradas": [], "errores": ["SoftwareDistribution: en uso"]})
    assert reparar.wu(ctx).veredicto == NO_EVALUADO


# ============================================================================ veredicto y conclusión
def _r(clave, veredicto, reparo=False, reinicio=False):
    return reparar._info(Resultado("REP_" + clave.upper(), reparar.TITULOS[clave], veredicto), reparo, reinicio)


def test_veredicto_y_conclusion():
    assert reparar.veredicto([_r("sfc", PASA)]) == "SIN PROBLEMAS"
    assert reparar.veredicto([_r("sfc", PASA, True), _r("punto", NO_EVALUADO)]) == "REPARADO"
    assert reparar.veredicto([_r("sfc", FALLA), _r("dism", PASA, True)]) == "QUEDARON PROBLEMAS"
    assert reparar.veredicto([_r("sfc", NO_EVALUADO)]) == "NO SE PUDO REPARAR"
    texto = reporte.conclusion([_r("sfc", PASA, True, True), _r("dism", PASA), _r("wu", FALLA)])
    assert "Se reparó: Archivos del sistema (SFC)" in texto and "Estaba bien: Imagen de Windows (DISM)" in texto
    assert "No se pudo reparar: Windows Update" in texto and "Reiniciá la PC" in texto


# ============================================================================ de punta a punta
def test_reparar_es_solo_pro_y_solo_windows(cfg):
    d = Diagnostico(cfg, WindowsFalso())
    d.listar_gpu = lambda: ([], "")
    d.cargar_equipo(esperar=True)
    assert d.iniciar({"reparar": {}}) == SOLO_PRO_REPARAR
    activar_pro(d)
    d2 = Diagnostico(cfg, PlataformaFalsa())
    d2.listar_gpu = lambda: ([], "")
    d2.cargar_equipo(esperar=True)
    assert "solo funciona en Windows" in d2.iniciar({"reparar": {}})
    d3 = Diagnostico(cfg, WindowsFalso(admin=False))
    d3.listar_gpu = lambda: ([], "")
    d3.cargar_equipo(esperar=True)
    assert "administrador" in d3.iniciar({"reparar": {}})
    assert d.iniciar({"reparar": {c: False for c in reparar.TITULOS}}) == "Elegí al menos una reparación"


def test_reparacion_completa_con_pdf_e_historial(cfg, monkeypatch):
    hechas = []

    def falsa(clave, veredicto, reparo):
        def f(ctx):
            hechas.append(clave)
            return _r(clave, veredicto, reparo)
        return f
    monkeypatch.setattr(reparar, "FUNCIONES", {"punto": falsa("punto", PASA, False), "dism": falsa("dism", PASA, False),
                                                "sfc": falsa("sfc", PASA, True), "chkdsk": falsa("chkdsk", PASA, False),
                                                "wu": falsa("wu", PASA, True), "red": falsa("red", PASA, True),
                                                "arranque": falsa("arranque", PASA, True)})
    d = activar_pro(Diagnostico(cfg, WindowsFalso()))
    d.listar_gpu = lambda: ([], "")
    d.cargar_equipo(esperar=True)
    assert d.iniciar({"orden": "77", "enviar": False, "reparar": {"wu": False}}) is None
    d.esperar()
    assert hechas == ["punto", "dism", "sfc", "chkdsk"]          # red y arranque vienen desmarcadas; wu se desmarcó
    est = d.estado_publico()
    assert est["tipo"] == "reparacion" and est["veredicto"] == "REPARADO" and est["archivo"].startswith("reparacion_")
    texto = "\n".join(p.extract_text() for p in pypdf.PdfReader(d.historial.ruta_pdf(est)).pages)
    assert "Reporte de reparación" in texto and "RESULTADO: REPARADO" in texto and "Se reparó: Archivos del sistema" in texto
    fila = d.historial_visible()[0]
    assert fila["tipo"] == "reparacion" and fila["orden"] == "77"
