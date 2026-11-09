"""El reporte PDF y el diagnóstico de punta a punta (con la plataforma simulada)."""
import email
import os

import pypdf
import pytest

from openstd import reporte
from openstd.diagnostico import Diagnostico, armar_correo
from openstd.resultado import FALLA, NO_EVALUADO, PASA, Resultado, veredicto_general
from tests.conftest import PlataformaFalsa, equipo_ejemplo


def _texto_pdf(ruta):
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(ruta).pages)


def test_veredicto_general():
    assert veredicto_general([]) == "SIN PRUEBAS"
    assert veredicto_general([Resultado("A", "a", PASA)]) == "SIN FALLAS"
    assert veredicto_general([Resultado("A", "a", PASA), Resultado("B", "b", NO_EVALUADO)]) == "SIN FALLAS (CON PRUEBAS NO EVALUADAS)"
    assert veredicto_general([Resultado("A", "a", FALLA), Resultado("B", "b", NO_EVALUADO)]) == "CON FALLAS"
    with pytest.raises(ValueError):
        Resultado("A", "a", "FAIL")


def test_conclusiones():
    hw = Resultado("RAM", "Memoria RAM", FALLA, acciones=["Cambiar la RAM"])
    so = Resultado("SISTEMA", "Sistema operativo", FALLA, acciones=["Reinstalar drivers"])
    assert "fallas de hardware (Memoria RAM)" in reporte.conclusion([hw, so])
    assert "el sistema registra errores" in reporte.conclusion([Resultado("CPU", "Procesador", PASA), so])
    assert reporte.acciones([Resultado("X", "x", PASA, acciones=["b"]), hw]) == ["Cambiar la RAM", "b"]
    assert "No se pudo evaluar: Disco" in reporte.conclusion([Resultado("DISCO_0", "Disco", NO_EVALUADO)])


def test_pdf_completo(tmp_path):
    res = [Resultado("CPU", "Procesador", PASA, "sin errores", ["línea con acentos: ñandú"]),
           Resultado("DISCO_0", "Disco 0", FALLA, "bloques ilegibles", ["  sub línea"], ["cuidado"], ["Cambiar el disco"],
                     {"tipo": "velocidad_disco", "puntos": [(i / 50, 400 - i) for i in range(50)]})]
    muestras = [{"t": 1000 + i * 2, "cpu": 90.0, "cpu_temp": 60.0 + i / 10} for i in range(30)]
    logo = tmp_path / "logo.png"
    _png_chico(logo)
    ruta = tmp_path / "rep" / "r.pdf"
    reporte.generar(str(ruta), {"orden": "42", "cliente": "Ana", "notas": "No prende a veces", "inicio": 1000, "fin": 1100},
                    res, equipo_ejemplo(), muestras, {"nombre": "Taller Ejemplo", "contacto": "099 123 456", "logo": str(logo)})
    texto = _texto_pdf(ruta)
    for esperado in ("Taller Ejemplo", "RESULTADO: CON FALLAS", "Orden", "42", "Ana", "Cambiar el disco", "ñandú", "No prende a veces"):
        assert esperado in texto, esperado


def test_pdf_sin_datos_del_taller(tmp_path):
    ruta = tmp_path / "r.pdf"
    reporte.generar(str(ruta), {}, [Resultado("CPU", "Procesador", PASA)], equipo_ejemplo())
    assert "SIN FALLAS" in _texto_pdf(ruta)


def _png_chico(ruta):
    import struct
    import zlib

    def trozo(tipo, datos):
        return struct.pack(">I", len(datos)) + tipo + datos + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
    crudo = b"".join(b"\0" + b"\x20\x60\xc0" * 4 for _ in range(2))
    ruta.write_bytes(b"\x89PNG\r\n\x1a\n" + trozo(b"IHDR", struct.pack(">IIBBBBB", 4, 2, 8, 2, 0, 0, 0))
                     + trozo(b"IDAT", zlib.compress(crudo)) + trozo(b"IEND", b""))


# ============================================================================ de punta a punta
ADAPTADORES = [{"api": "Vulkan", "nombre": "GPU X", "software": False, "indice": 0},
               {"api": "OpenGL", "nombre": "GPU X", "software": False, "indice": 0},
               {"api": "D3D12", "nombre": "Microsoft Basic Render Driver", "software": True, "indice": 0}]


def _diag(cfg, plat=None):
    d = Diagnostico(cfg, plat or PlataformaFalsa())
    d.listar_gpu = lambda: (ADAPTADORES, "")
    d.cargar_equipo(esperar=True)
    return d


def test_diagnostico_completo_con_correo(cfg_correo, smtp):
    d = _diag(cfg_correo)
    err = d.iniciar({"orden": "77", "cliente": "Juan", "correo_cliente": "juan@cliente.com", "enviar": True,
                     "pruebas": {"sistema": True, "bateria": True, "discos": []}})
    assert err is None
    d.esperar()
    est = d.estado
    assert not est["corriendo"] and est["veredicto"] == "SIN FALLAS", est
    assert [p["estado"] for p in est["pasos"]] == ["listo", "listo"]
    assert est["correo"]["estado"] == "enviado", est["correo"]
    r = smtp.esperar()[0]
    assert set(r["para"]) == {"reportes@ejemplo.com", "juan@cliente.com"}
    m = email.message_from_bytes(r["datos"])
    assert "Orden 77" in m["Subject"]
    assert [p.get_filename() for p in m.walk() if p.get_filename()][0].endswith("_orden77.pdf")
    h = d.historial.lista()
    assert h[0]["orden"] == "77" and h[0]["hay_pdf"]
    assert os.path.exists(d.historial.ruta_pdf(d.historial.leer(h[0]["id"])))


def test_si_el_correo_falla_queda_en_cola(cfg_correo, smtp):
    cfg_correo.guardar({"smtp_clave": "equivocada"})
    d = _diag(cfg_correo)
    d.iniciar({"pruebas": {"sistema": True, "bateria": False, "discos": []}})
    d.esperar()
    assert d.estado["correo"]["estado"] == "error" and "reintenta" in d.estado["correo"]["texto"]
    assert len(d.cola.pendientes()) == 1
    cfg_correo.guardar({"smtp_clave": smtp.clave})
    assert d.cola.procesar() == 1


def test_sin_correo_configurado_guarda_el_pdf(cfg):
    d = _diag(cfg)
    d.iniciar({"pruebas": {"sistema": True, "discos": []}})
    d.esperar()
    assert d.estado["archivo"] and d.estado["correo"]["estado"] == "error"
    assert "no está configurado" in d.estado["correo"]["texto"]


def test_error_del_programa_es_no_evaluado(cfg, monkeypatch):
    plat = PlataformaFalsa()
    monkeypatch.setattr(plat, "eventos", lambda desde: 1 / 0)
    d = _diag(cfg, plat)
    d.iniciar({"enviar": False, "pruebas": {"sistema": True, "bateria": False, "discos": []}})
    d.esperar()
    r = d.estado["resultados"][0]
    assert r["veredicto"] == NO_EVALUADO and "Error del programa" in r["motivo"]


def test_eleccion_de_pasos(cfg):
    d = _diag(cfg)
    pasos = d._pasos({"sistema": True, "bateria": True, "cpu": 1, "ram": 0, "gpu": 2})
    assert [p["clave"] for p in pasos] == ["sistema", "bateria", "disco", "cpu", "gpu"]   # el pendrive USB no se elige solo
    assert d._pasos({"sistema": False, "bateria": False, "discos": ["1"]})[0]["disco"] == "1"
    sin_bateria = PlataformaFalsa(equipo=dict(equipo_ejemplo(), baterias=0))
    assert "bateria" not in [p["clave"] for p in _diag(cfg, sin_bateria)._pasos({"bateria": True, "discos": []})]
    assert d.iniciar({"pruebas": {"sistema": False, "bateria": False, "discos": []}}) == "Elegí al menos una prueba"
    assert d.iniciar({"correo_cliente": "malo", "pruebas": {"sistema": True}}) == "El correo del cliente no es válido"


def test_correo_del_reporte():
    reg = {"orden": "5", "cliente": "Ana", "equipo": "Equipo X", "inicio": 0, "veredicto": "CON FALLAS",
           "resultados": [Resultado("RAM", "Memoria RAM", FALLA, "errores", acciones=["Cambiar la RAM"]).a_dict()]}
    asunto, cuerpo = armar_correo(reg, "Taller Ejemplo")
    assert asunto == "[Diagnóstico: CON FALLAS] Orden 5 - Equipo X"
    assert "- Memoria RAM: FALLA (errores)" in cuerpo and "- Cambiar la RAM" in cuerpo and cuerpo.endswith("Taller Ejemplo")


def test_opciones_de_gpu(cfg):
    d = _diag(cfg)
    ids = [o["id"] for o in d.opciones()["gpu"]]
    assert ids == ["todas", "api:OpenGL", "api:Vulkan"]         # la placa por software no se ofrece
