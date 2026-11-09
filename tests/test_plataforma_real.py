"""Corre contra el sistema real (Windows o Linux) para confirmar que la lectura no se rompe.
No juzga el resultado (cada máquina es distinta): solo que los datos tengan el formato esperado."""
import time

import pytest

from openstd import plataforma
from openstd.util import ES_LINUX, ES_WINDOWS

pytestmark = pytest.mark.skipif(not (ES_WINDOWS or ES_LINUX), reason="solo Windows y Linux")


def test_equipo_real():
    e = plataforma.actual().equipo()
    for k in ("fabricante", "modelo", "cpu", "ram", "video", "discos", "baterias", "sistema"):
        assert k in e
    assert e["cpu"]["hilos"] >= 1
    assert e["ram"]["visible"] > 0
    for d in e["discos"]:
        assert {"id", "ruta", "smart_ruta", "tam", "tipo", "es_usb"} <= set(d)


def test_eventos_reales():
    ev = plataforma.actual().eventos(time.time() - 3 * 86400)
    assert isinstance(ev["disponible"], bool)
    for k in ("pantallazos", "apagados", "hardware", "disco", "video", "drivers"):
        assert isinstance(ev[k], list)


def test_volumenes_y_baterias_reales():
    p = plataforma.actual()
    assert isinstance(p.volumenes(), list)
    assert isinstance(p.baterias(), list)
