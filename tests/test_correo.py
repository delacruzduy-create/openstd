import email

import pytest

from openstd import correo
from tests.conftest import puerto_cerrado


def _pdf(tmp_path):
    p = tmp_path / "reporte.pdf"
    p.write_bytes(b"%PDF-1.4 prueba")
    return str(p)


def test_envio_con_adjunto(cfg_correo, smtp, tmp_path):
    msg = correo.armar(cfg_correo, "Asunto ñandú", "Cuerpo con acentos: áéíóú", [_pdf(tmp_path)], extra="cliente@ejemplo.com")
    correo.enviar(cfg_correo, msg)
    r = smtp.esperar()[0]
    assert r["autenticado"]
    assert set(r["para"]) == {"reportes@ejemplo.com", "cliente@ejemplo.com"}
    m = email.message_from_bytes(r["datos"])
    assert "Taller de Prueba" in str(email.header.make_header(email.header.decode_header(m["From"])))
    adjuntos = [p for p in m.walk() if p.get_filename()]
    assert adjuntos[0].get_filename() == "reporte.pdf"
    assert adjuntos[0].get_payload(decode=True) == b"%PDF-1.4 prueba"


def test_contrasena_rechazada(cfg_correo):
    cfg_correo.guardar({"smtp_clave": "equivocada"})
    with pytest.raises(correo.ErrorCorreo, match="rechazó el usuario o la contraseña"):
        correo.probar(cfg_correo)


def test_ayuda_de_gmail():
    assert "contraseña de aplicación" in correo._ayuda("smtp.gmail.com")
    assert correo._ayuda("mail.miempresa.com") == ""


def test_servidor_que_no_responde(cfg):
    cfg.guardar({"smtp_servidor": "127.0.0.1", "smtp_puerto": puerto_cerrado(), "smtp_seguridad": "ninguna",
                 "smtp_usuario": "a@b.com", "destinatarios": "c@d.com"})
    with pytest.raises(correo.ErrorCorreo, match="puerto"):
        correo.probar(cfg)


def test_sin_configurar(cfg):
    with pytest.raises(correo.ErrorCorreo, match="destinatario"):
        correo.probar(cfg)
    cfg.guardar({"destinatarios": "c@d.com", "smtp_usuario": "a@b.com"})
    with pytest.raises(correo.ErrorCorreo, match="servidor"):
        correo.probar(cfg)


def test_adjunto_que_no_existe(cfg_correo):
    with pytest.raises(correo.ErrorCorreo, match="No se encuentra"):
        correo.armar(cfg_correo, "a", "b", ["/no/existe.pdf"])


def test_cola_reintenta_cuando_vuelve_el_servidor(cfg_correo, smtp, tmp_path):
    cola = correo.Cola(cfg_correo)
    pdf = _pdf(tmp_path)
    cfg_correo.guardar({"smtp_clave": "equivocada"})
    cola.agregar("Reporte", "cuerpo", pdf)
    cola.agregar("Reporte", "cuerpo", pdf)            # el mismo PDF no se encola dos veces
    assert len(cola.pendientes()) == 1
    assert cola.procesar() == 0
    assert cola.pendientes()[0]["intentos"] == 1 and cola.ultimo_error
    cfg_correo.guardar({"smtp_clave": smtp.clave})
    assert cola.procesar() == 1
    assert cola.pendientes() == []
    assert smtp.esperar()


def test_cola_descarta_lo_muy_viejo(cfg_correo, smtp):
    cola = correo.Cola(cfg_correo)
    id_ = cola.agregar("viejo", "cuerpo")
    import json
    import os
    import time
    ruta = os.path.join(cola.carpeta, id_ + ".json")
    d = json.load(open(ruta, encoding="utf-8"))
    d["creado"] = time.time() - 30 * 86400
    json.dump(d, open(ruta, "w", encoding="utf-8"))
    assert cola.procesar() == 0
    assert cola.pendientes() == []
    assert smtp.recibidos == []
