#!/usr/bin/env bash
# OpenSTD Test en Linux. La primera vez prepara las librerías; después pide sudo (hace falta para leer los
# discos y el registro del kernel) y abre el panel.
set -e
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p datos

if ! command -v python3 >/dev/null; then
    echo "Falta Python 3: sudo apt install python3 python3-venv"
    exit 1
fi
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
if ! "$PY" -c "import psutil, reportlab, wgpu, glfw" 2>/dev/null; then
    echo "Preparando las librerías (una sola vez, necesita internet)..."
    if ! python3 -m venv --system-site-packages .venv; then
        echo "Falta el paquete venv: sudo apt install python3-venv"
        exit 1
    fi
    .venv/bin/pip install -q -r requirements.txt
    PY=.venv/bin/python
fi
command -v smartctl >/dev/null || [ -x /usr/sbin/smartctl ] || \
    echo "Aviso: sin smartmontools no se lee la salud SMART de los discos (sudo apt install smartmontools)"

if [ "$(id -u)" -ne 0 ]; then
    echo "Pidiendo permisos de administrador (sudo)..."
    # se conserva la sesión gráfica (para las herramientas externas de GPU)
    exec sudo --preserve-env=DISPLAY,WAYLAND_DISPLAY,XAUTHORITY,XDG_RUNTIME_DIR,DBUS_SESSION_BUS_ADDRESS \
        "$PY" -m openstd "$@"
fi
exec "$PY" -m openstd "$@"
