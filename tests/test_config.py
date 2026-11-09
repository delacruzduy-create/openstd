import json
import os

from openstd.config import POR_DEFECTO, Config, correo_valido, destinatarios


def test_de_fabrica_no_trae_correo_ni_contrasena():
    assert POR_DEFECTO["smtp_clave"] == ""
    assert POR_DEFECTO["smtp_usuario"] == ""
    assert POR_DEFECTO["destinatarios"] == ""
    assert POR_DEFECTO["smtp_servidor"] == ""


def test_guardar_y_releer(tmp_path):
    c = Config(str(tmp_path))
    assert c.guardar({"taller_nombre": "  Mi Taller  ", "minutos_cpu": "7", "enviar_al_terminar": False}) == []
    c2 = Config(str(tmp_path))
    assert c2.get("taller_nombre") == "Mi Taller"
    assert c2.get("minutos_cpu") == 7
    assert c2.get("enviar_al_terminar") is False


def test_validacion(cfg):
    errores = cfg.guardar({"minutos_cpu": 99999, "smtp_seguridad": "rara", "destinatarios": "no-es-un-correo",
                           "enviar_al_terminar": "si", "desconocida": 1, "puerto": "abc"})
    assert len(errores) == 5
    assert cfg.get("minutos_cpu") == POR_DEFECTO["minutos_cpu"]
    assert cfg.get("smtp_seguridad") == "starttls"
    assert "desconocida" not in cfg.cargar()


def test_lo_valido_se_guarda_aunque_haya_errores(cfg):
    errores = cfg.guardar({"minutos_ram": 3, "minutos_cpu": -1})
    assert errores and cfg.get("minutos_ram") == 3


def test_la_contrasena_no_sale_al_panel(cfg):
    cfg.guardar({"smtp_clave": "secreta"})
    pub = cfg.publico()
    assert "smtp_clave" not in pub
    assert pub["smtp_clave_configurada"] is True
    assert "secreta" not in json.dumps(pub)


def test_contrasena_vacia_no_borra_y_none_si(cfg):
    cfg.guardar({"smtp_clave": "secreta"})
    cfg.guardar({"smtp_clave": ""})
    assert cfg.get("smtp_clave") == "secreta"
    cfg.guardar({"smtp_clave": None})
    assert cfg.get("smtp_clave") == ""


def test_archivo_privado_en_linux(cfg):
    cfg.guardar({"smtp_clave": "x"})
    if os.name != "nt":
        assert os.stat(cfg.ruta).st_mode & 0o077 == 0


def test_config_rota_usa_valores_de_fabrica(tmp_path):
    (tmp_path / "config.json").write_text("{esto no es json", encoding="utf-8")
    assert Config(str(tmp_path)).get("puerto") == POR_DEFECTO["puerto"]


def test_referencias(cfg):
    assert cfg.referencia("abc") is None
    cfg.poner_referencia("abc", 1000.0, "cambio de RAM")
    assert cfg.referencia("abc") == {"desde": 1000.0, "motivo": "cambio de RAM"}
    assert "referencias" not in cfg.publico()


def test_correos():
    assert correo_valido("a@b.com")
    for malo in ("", "a", "a@b", "a@@b.com", "a b@c.com", "<a@b.com>", "a@.com", "a@com."):
        assert not correo_valido(malo), malo
    assert destinatarios("a@b.com; A@B.com, c@d.org", "c@d.org, x@y.uy") == ["a@b.com", "c@d.org", "x@y.uy"]


def test_carpeta_de_datos_por_variable(monkeypatch, tmp_path):
    from openstd import config
    monkeypatch.setenv("OPENSTD_DATOS", str(tmp_path / "x"))
    assert config.carpeta_datos() == str(tmp_path / "x")
