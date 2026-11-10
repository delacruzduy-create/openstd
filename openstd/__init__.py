"""OpenSTD Test: diagnóstico de hardware y sistema para Windows y Linux."""

import os

__version__ = "0.1.0"
NOMBRE = "OpenSTD Test"

# Página donde se compra la versión Pro (Gumroad, Mercado Pago, etc.).
# Mientras no empiece con http, el panel muestra "Próximamente" en vez del botón de compra.
LINK_PRO = "https://delacruz200.gumroad.com/l/openstd-pro"

# La copia que se vende trae un archivo PRO.txt junto a INICIAR.bat: el panel deja de ofrecer la compra.
EDICION_PRO = os.path.exists(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "PRO.txt"))
