"""La licencia Pro: activación con Gumroad (simulado), límites y revisión periódica."""
import json
import time

from openstd import licencia
from openstd.licencia import Licencia, ocultar_correo
from tests.conftest import gumroad_falso


def test_activar_y_desactivar(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso())
    assert not lic.es_pro and lic.estado()["pro"] is False
    assert lic.activar("  clave-buena-1234 ") is None          # sin espacios y en mayúsculas, como la da Gumroad
    assert lic.es_pro
    e = lic.estado()
    assert e == {"pro": True, "correo": "co***@ejemplo.com", "clave": "…1234", "disponible": True}
    assert lic.activar("CLAVE-BUENA-1234") is None              # activarla de nuevo no consulta ni suma un uso
    lic.desactivar()
    assert not lic.es_pro
    lic.desactivar()                                            # dos veces no rompe


def test_clave_invalida_o_corta(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso())
    assert "no es válida" in lic.activar("CLAVE-MALA-0000")
    assert "Pegá la clave" in lic.activar("abc")
    assert not lic.es_pro


def test_compra_devuelta_no_activa(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso(devuelta=True))
    assert "devuelta" in lic.activar("CLAVE-BUENA-1234")


def test_maximo_de_activaciones(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso(usos=licencia.MAX_ACTIVACIONES + 1))
    assert "máximo" in lic.activar("CLAVE-BUENA-1234")
    assert not lic.es_pro


def test_sin_internet_explica_que_hace_falta_una_vez(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso(sin_internet=True))
    assert "internet una sola vez" in lic.activar("CLAVE-BUENA-1234")


def test_revisar(tmp_path):
    lic = Licencia(str(tmp_path), gumroad_falso())
    lic.activar("CLAVE-BUENA-1234")
    vieja = time.time() - licencia.REVISAR_CADA - 10

    def envejecer():
        d = json.loads(open(lic.ruta, encoding="utf-8").read())
        d["revisada"] = vieja
        open(lic.ruta, "w", encoding="utf-8").write(json.dumps(d))

    envejecer()
    lic.consultar = gumroad_falso(sin_internet=True)
    lic.revisar()
    assert lic.es_pro                                           # sin internet sigue siendo Pro
    lic.consultar = gumroad_falso()
    lic.revisar()
    assert json.loads(open(lic.ruta, encoding="utf-8").read())["revisada"] > vieja
    envejecer()
    lic.consultar = gumroad_falso(devuelta=True)
    lic.revisar()
    assert not lic.es_pro                                       # devuelta: deja de ser Pro


def test_sin_producto_configurado(tmp_path, monkeypatch):
    monkeypatch.setattr(licencia, "GUMROAD_PRODUCT_ID", "")
    lic = Licencia(str(tmp_path))
    assert lic.estado()["disponible"] is False
    assert "todavía no está disponible" in lic.activar("CLAVE-BUENA-1234")


def test_ocultar_correo():
    assert ocultar_correo("juan.perez@gmail.com") == "ju***@gmail.com"
    assert ocultar_correo("") == "" and ocultar_correo("sin-arroba") == ""
