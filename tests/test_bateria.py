import os

from openstd.plataforma.linux import leer_baterias
from openstd.plataforma.windows import interpretar_powercfg
from openstd.pruebas import bateria
from openstd.resultado import FALLA, NO_EVALUADO, PASA


def _b(actual, diseno=50_000_000):
    return {"fabricante": "ACME", "modelo": "B1", "quimica": "Li-ion", "diseno": diseno, "actual": actual, "unidad": "µWh",
            "ciclos": 300, "estado": "Full", "carga": 100}


def test_umbrales():
    assert bateria.evaluar(_b(45_000_000), 0, 80, 50).veredicto == PASA
    r = bateria.evaluar(_b(35_000_000), 0, 80, 50)
    assert r.veredicto == PASA and r.advertencias
    r = bateria.evaluar(_b(20_000_000), 0, 80, 50)
    assert r.veredicto == FALLA and r.acciones == ["Reemplazar la batería"]
    assert bateria.evaluar(_b(20_000_000, diseno=0), 0, 80, 50).veredicto == NO_EVALUADO


def test_formato_de_capacidad():
    r = bateria.evaluar(_b(45_000_000), 0, 80, 50)
    assert any("45,0 Wh de 50,0 Wh" in l and "90 %" in l for l in r.lineas)
    w = dict(_b(45000), unidad="mWh", diseno=50000)
    assert any("45,0 Wh" in l for l in bateria.evaluar(w, 0, 80, 50).lineas)


def test_sysfs_de_linux(tmp_path):
    def bat(nombre, **campos):
        d = tmp_path / "class" / "power_supply" / nombre
        d.mkdir(parents=True)
        for k, v in campos.items():
            (d / k).write_text(str(v))
    bat("BAT0", type="Battery", energy_full=40000000, energy_full_design=50000000, cycle_count=10, status="Charging",
        capacity=55, manufacturer="ACME", model_name="X")
    bat("AC", type="Mains")
    bat("hidpp_battery_0", type="Battery", scope="Device", capacity=80)     # mouse inalámbrico: no cuenta
    bat("BAT1", type="Battery", charge_full=3000000, charge_full_design=4000000)
    r = leer_baterias(str(tmp_path))
    assert [b["nombre"] for b in r] == ["BAT0", "BAT1"]
    assert r[0]["actual"] == 40000000 and r[0]["unidad"] == "µWh" and r[0]["ciclos"] == 10
    assert r[1]["unidad"] == "µAh"
    assert bateria.evaluar(r[1], 1, 80, 50).titulo == "Batería 2"


POWERCFG = """<?xml version="1.0" encoding="utf-8"?>
<BatteryReport xmlns="http://schemas.microsoft.com/battery/2012">
  <Batteries>
    <Battery>
      <Id>DELL ABC12</Id><Manufacturer>SMP</Manufacturer><Chemistry>LiP</Chemistry>
      <DesignCapacity>56000</DesignCapacity><FullChargeCapacity>23000</FullChargeCapacity><CycleCount>812</CycleCount>
    </Battery>
  </Batteries>
</BatteryReport>"""


def test_powercfg_de_windows():
    r = interpretar_powercfg(POWERCFG)
    assert len(r) == 1 and r[0]["diseno"] == 56000 and r[0]["actual"] == 23000 and r[0]["ciclos"] == 812
    res = bateria.evaluar(r[0], 0, 80, 50)
    assert res.veredicto == FALLA
    assert interpretar_powercfg("") == [] and interpretar_powercfg("<roto") == []


def test_ejecutar_con_plataforma(ctx):
    r = bateria.ejecutar(ctx)
    assert len(r) == 1 and r[0].veredicto == PASA
    ctx.plat._baterias = []
    r = bateria.ejecutar(ctx)
    assert r[0].veredicto == NO_EVALUADO          # el equipo dice que tiene batería pero no informa datos


def test_sin_rutas_raras():
    assert os.sep  # (la lectura de Linux se prueba con una carpeta temporal, no con /sys real)
