"""El panel web: seguridad básica y el flujo completo por la API."""
import base64
import http.client
import json
import time

import pytest

from openstd.diagnostico import Diagnostico
from openstd.web.servidor import servir
from tests.conftest import PlataformaFalsa


@pytest.fixture
def panel(cfg):
    d = Diagnostico(cfg, PlataformaFalsa())
    d.listar_gpu = lambda: ([], "")
    d.cargar_equipo(esperar=True)
    srv = servir(d, 18765, intentos=200)
    yield srv
    srv.shutdown()
    srv.server_close()


def pedir(srv, metodo, ruta, datos=None, host=None, cabeceras=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = {"Host": host or f"localhost:{srv.server_port}"}
    cuerpo = None
    if datos is not None:
        cuerpo = json.dumps(datos).encode()
        h.update({"Content-Type": "application/json", "X-OpenSTD": "1"})
    h.update(cabeceras or {})
    c.request(metodo, ruta, body=cuerpo, headers=h)
    r = c.getresponse()
    contenido = r.read()
    c.close()
    try:
        return r.status, json.loads(contenido)
    except ValueError:
        return r.status, contenido


def test_panel_se_sirve(panel):
    st, html = pedir(panel, "GET", "/")
    assert st == 200 and b"OpenSTD Test" in html and b"{{VERSION}}" not in html


def test_host_ajeno_rechazado(panel):
    st, _ = pedir(panel, "GET", "/api/opciones", host="atacante.com")
    assert st == 403


def test_post_sin_cabecera_rechazado(panel):
    st, _ = pedir(panel, "POST", "/api/cancelar", cabeceras={"Content-Type": "application/json"})
    assert st == 403
    c = http.client.HTTPConnection("127.0.0.1", panel.server_port, timeout=10)
    c.request("POST", "/api/config", body=b'{"taller_nombre":"x"}',
              headers={"Host": f"localhost:{panel.server_port}", "Content-Type": "text/plain", "X-OpenSTD": "1"})
    assert c.getresponse().status == 403


def test_config_por_api_sin_mostrar_la_clave(panel):
    st, r = pedir(panel, "POST", "/api/config", {"smtp_clave": "super-secreta", "taller_nombre": "Taller Uno"})
    assert st == 200 and r["config"]["taller_nombre"] == "Taller Uno"
    st, r = pedir(panel, "GET", "/api/config")
    assert "super-secreta" not in json.dumps(r) and r["smtp_clave_configurada"]
    st, r = pedir(panel, "POST", "/api/config", {"destinatarios": "mal"})
    assert st == 400 and r["errores"]


def test_logo(panel):
    st, r = pedir(panel, "POST", "/api/logo", {"datos": base64.b64encode(b"no es una imagen").decode()})
    assert st == 400
    st, r = pedir(panel, "POST", "/api/logo", {"datos": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()})
    assert st == 200
    st, r = pedir(panel, "GET", "/api/config")
    assert r["logo"].endswith("logo.png")


def test_diagnostico_por_api(panel):
    st, op = pedir(panel, "GET", "/api/opciones")
    assert st == 200 and op["equipo"]["identificacion"].startswith("Fabricante SA Modelo X1")
    st, r = pedir(panel, "POST", "/api/iniciar", {"orden": "9", "enviar": False, "pruebas": {"sistema": True, "bateria": True, "discos": []}})
    assert st == 200, r
    for _ in range(100):
        st, est = pedir(panel, "GET", "/api/estado")
        if not est["corriendo"]:
            break
        time.sleep(0.1)
    assert est["veredicto"] == "SIN FALLAS"
    st, pdf = pedir(panel, "GET", f"/pdf/{est['id']}")
    assert st == 200 and pdf.startswith(b"%PDF")
    st, h = pedir(panel, "GET", "/api/historial")
    assert h[0]["id"] == est["id"]
    st, _ = pedir(panel, "GET", "/pdf/..%2F..%2Fconfig")
    assert st == 404


def test_contar_desde_hoy(panel):
    st, _ = pedir(panel, "POST", "/api/contar_desde_hoy", {})
    assert st == 200
    st, op = pedir(panel, "GET", "/api/opciones")
    assert op["referencia"]["motivo"]


def test_logo_del_panel(panel):
    st, png = pedir(panel, "GET", "/logo.png")
    assert st == 200 and png[1:4] == b"PNG"


def test_panel_tiene_pestana_pro(panel):
    st, html = pedir(panel, "GET", "/")
    assert st == 200 and b"tab-pro" in html and b"{{LINK_PRO}}" not in html and b"{{PRO}}" not in html
