"""Las pruebas. Cada una recibe un Contexto y devuelve uno o más Resultado."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..plataforma.base import Plataforma
from ..resultado import Progreso
from ..sensores import Muestreador


@dataclass
class Contexto:
    equipo: dict
    config: Config
    plat: Plataforma
    muestreador: Muestreador
    progreso: Progreso = field(default_factory=Progreso)


# Códigos de salida de un proceso que indican que el procesador o la memoria fallaron en pleno cálculo
# (Windows: excepciones NTSTATUS 0xC0000000 en adelante; Linux: señales de acceso inválido o instrucción ilegal).
SENALES_HARDWARE = {-4, -7, -8, -11}          # SIGILL, SIGBUS, SIGFPE, SIGSEGV


def cierre_por_hardware(exitcode: int | None) -> str | None:
    """Si el código de salida es de un cuelgue del procesador, devuelve la descripción; si no, None."""
    if exitcode is None or exitcode == 0:
        return None
    if exitcode < 0:            # Linux: el proceso terminó por una señal (SIGKILL del sistema por falta de memoria no cuenta)
        return {-4: "instrucción ilegal", -7: "error de bus", -8: "error de coma flotante", -11: "acceso inválido a memoria"}.get(exitcode)
    codigo = exitcode & 0xFFFFFFFF
    if codigo >= 0xC0000000 and codigo != 0xC000013A:          # 0xC000013A = cerrado con Ctrl+C
        return f"excepción 0x{codigo:08X}"
    return None
