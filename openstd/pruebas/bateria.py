"""Salud de la batería: capacidad actual contra la de fábrica, y ciclos de carga."""
from __future__ import annotations

from ..resultado import FALLA, NO_EVALUADO, PASA, Resultado
from . import Contexto


def _cap(valor: int, unidad: str) -> str:
    if unidad in ("µWh", "µAh"):
        return f"{valor / 1e6:.1f} {unidad[1:]}".replace(".", ",")
    if unidad == "mWh":
        return f"{valor / 1000:.1f} Wh".replace(".", ",")
    return f"{valor} {unidad}"


def evaluar(b: dict, indice: int, alerta: float, falla: float) -> Resultado:
    r = Resultado(f"BATERIA_{indice}", "Batería" + (f" {indice + 1}" if indice else ""))
    desc = " ".join(x for x in (b.get("fabricante"), b.get("modelo"), b.get("quimica")) if x)
    if desc:
        r.lineas.append("Batería: " + desc)
    diseno, actual = b.get("diseno") or 0, b.get("actual") or 0
    salud = None
    if diseno > 0 and actual > 0:
        salud = 100.0 * actual / diseno
        r.lineas.append(f"Capacidad actual: {_cap(actual, b.get('unidad', ''))} de {_cap(diseno, b.get('unidad', ''))} "
                        f"de fábrica = salud {min(salud, 100):.0f} %")
    else:
        r.lineas.append("La batería no informa su capacidad de fábrica")
    if b.get("ciclos"):
        r.lineas.append(f"Ciclos de carga: {b['ciclos']}")
    if b.get("estado") or b.get("carga") is not None:
        r.lineas.append(f"Estado: {b.get('estado') or '-'}" + (f" · carga actual {b['carga']} %" if b.get("carga") is not None else ""))
    if salud is None:
        return r.poner(NO_EVALUADO, "No se pudo calcular la salud")
    if salud < falla:
        r.poner(FALLA, f"Conserva solo el {salud:.0f} % de su capacidad original")
        r.acciones.append("Reemplazar la batería")
    else:
        r.poner(PASA, f"Conserva el {min(salud, 100):.0f} % de su capacidad original")
        if salud < alerta:
            r.advertencias.append(f"Desgaste notable ({salud:.0f} % de la capacidad original): la autonomía va a ser menor")
    return r


def ejecutar(ctx: Contexto) -> list[Resultado]:
    ctx.progreso.set_fase("Batería: leyendo el estado...")
    bats = ctx.plat.baterias()
    if not bats and (ctx.equipo.get("baterias") or 0) > 0:
        return [Resultado("BATERIA_0", "Batería", NO_EVALUADO, "El sistema detecta una batería pero no informa su capacidad")]
    return [evaluar(b, i, ctx.config.get("bateria_alerta"), ctx.config.get("bateria_falla")) for i, b in enumerate(bats)]
