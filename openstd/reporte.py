"""Reporte PDF del diagnóstico (reportlab): encabezado con los datos del taller (y su logo, si hay),
veredicto general, conclusión en palabras simples, qué hacer, datos del equipo y el detalle de cada prueba
con el gráfico de velocidad de los discos y el de temperatura y carga."""
from __future__ import annotations

import os
import time
from xml.sax.saxutils import escape

from . import __version__, reparar
from .resultado import FALLA, NO_EVALUADO, PASA, Resultado, veredicto_general
from .util import tam

VERDE, ROJO, AMBAR, GRIS, AZUL = "#1a8c26", "#cc0d0d", "#d97300", "#737373", "#264fb3"


# ============================================================================ textos (sin reportlab: se usan también en el correo)
def conclusion(resultados: list[Resultado]) -> str:
    """La frase para el cliente: ¿es el hardware o es el sistema?"""
    if reparar.es_reparacion(resultados):
        return reparar.conclusion(resultados)
    hw = [r for r in resultados if r.codigo != "SISTEMA"]
    sw = [r for r in resultados if r.codigo == "SISTEMA"]
    bateria_mal = any(r.codigo.startswith("BATERIA") and r.veredicto == FALLA for r in hw)
    hw_mal = [r.titulo for r in hw if r.veredicto == FALLA and not r.codigo.startswith("BATERIA")]
    sw_mal = any(r.veredicto == FALLA for r in sw)
    obs = sum(len(r.advertencias) for r in resultados)
    no_ev = [r.titulo for r in resultados if r.veredicto == NO_EVALUADO]
    if not resultados:
        return "No se hicieron pruebas."
    if hw_mal:
        texto = (f"Se encontraron fallas de hardware ({', '.join(hw_mal)}). Hay que resolver eso primero: con hardware "
                 "fallando, el sistema y los programas van a seguir dando problemas.")
        if sw_mal:
            texto += " El registro del sistema también muestra errores (ver el detalle)."
    elif sw_mal:
        texto = (("Las pruebas de hardware no mostraron fallas" if hw else "No se probó el hardware en esta sesión")
                 + ", pero el sistema registra errores (pantallazos, apagados o errores de hardware). Ver qué hacer más abajo.")
    else:
        texto = "No se encontraron fallas en lo que se probó."
        if obs:
            texto += f" Hay {obs} {'observaciones' if obs > 1 else 'observación'} para tener en cuenta."
    if bateria_mal:
        texto += " La batería está gastada y conviene reemplazarla."
    if no_ev:
        texto += f" No se pudo evaluar: {', '.join(no_ev)}."
    return texto


def acciones(resultados: list[Resultado]) -> list[str]:
    """Qué hacer: primero lo de las pruebas que fallaron."""
    lista = []
    for r in sorted(resultados, key=lambda r: {FALLA: 0, NO_EVALUADO: 2}.get(r.veredicto, 1)):
        for a in r.acciones:
            if a not in lista:
                lista.append(a)
    return lista


def secciones_equipo(e: dict) -> list[tuple[str, list[str]]]:
    s = []
    eq = ["Fabricante / modelo: " + (" ".join(x for x in (e.get("fabricante"), e.get("modelo")) if x) or "(no informado)"),
          "N.º de serie: " + (e.get("serie") or "(no informado)")]
    if e.get("placa"):
        eq.append("Placa base: " + e["placa"])
    if e.get("bios"):
        eq.append("BIOS: " + e["bios"])
    if e.get("sistema"):
        eq.append("Sistema: " + e["sistema"])
    s.append(("Equipo", eq))
    c = e.get("cpu") or {}
    s.append(("Procesador", [f"{c.get('nombre') or '?'} · " + (f"{c['nucleos']} núcleos · " if c.get("nucleos") else "")
                             + f"{c.get('hilos') or '?'} hilos" + (f" · hasta {c['mhz']} MHz" if c.get("mhz") else "")]))
    ram = e.get("ram") or {}
    lineas = [f"Instalada: {tam(ram.get('instalada') or 0, False)} · visible para el sistema: {tam(ram.get('visible') or 0, False)}"]
    for m in ram.get("modulos") or []:
        lineas.append(" ".join(x for x in (f"{m.get('ranura')}:", tam(m.get("tam") or 0, False), m.get("tipo"), m.get("velocidad"),
                                           m.get("fabricante"), m.get("parte")) if x))
    s.append(("Memoria RAM", lineas))
    s.append(("Video", [v["nombre"] + (f" · driver {v['driver']}" if v.get("driver") else "") for v in e.get("video") or []]
              or ["(no informado)"]))
    s.append(("Discos", [f"Disco {d['id']}: {d.get('modelo')} · {tam(d.get('tam') or 0)} · {d.get('tipo')}"
                         + (f" · {d['bus']}" if d.get("bus") else "") + (f" · S/N {d['serie']}" if d.get("serie") else "")
                         + (" · tiene el sistema" if d.get("es_sistema") else "") for d in e.get("discos") or []] or ["(ninguno)"]))
    return s


def identificacion(e: dict) -> str:
    texto = " ".join(x for x in (e.get("fabricante"), e.get("modelo")) if x) or e.get("placa") or e.get("nombre_pc") or "Equipo"
    return texto + (f" (S/N {e['serie']})" if e.get("serie") else "")


# ============================================================================ PDF
def generar(ruta: str, datos: dict, resultados: list[Resultado], equipo: dict, muestras: list[dict] | None = None,
            taller: dict | None = None) -> None:
    """datos: {"orden", "cliente", "notas", "inicio", "fin"}; taller: {"nombre", "contacto", "logo"}. Lanza excepción si falla."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    taller = taller or {}
    es_rep = datos.get("tipo") == "reparacion"
    titulo_doc = "Reporte de reparación" if es_rep else "Reporte de diagnóstico"
    col = {PASA: VERDE, FALLA: ROJO, NO_EVALUADO: AMBAR}
    t_titulo = ParagraphStyle("titulo", fontName="Helvetica-Bold", fontSize=17, leading=21)
    t_texto = ParagraphStyle("texto", fontName="Helvetica", fontSize=9.5, leading=12.5)
    t_sub = ParagraphStyle("sub", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=colors.HexColor(AZUL),
                           spaceBefore=9, spaceAfter=3, keepWithNext=1)
    t_sec = ParagraphStyle("sec", fontName="Helvetica-Bold", fontSize=9.5, leading=12.5, spaceBefore=3)
    t_linea = ParagraphStyle("linea", fontName="Helvetica", fontSize=8.5, leading=11, leftIndent=8)
    t_linea2 = ParagraphStyle("linea2", parent=t_linea, leftIndent=20)
    t_tabla = ParagraphStyle("tabla", fontName="Helvetica", fontSize=8.5, leading=10.5)
    t_punto = ParagraphStyle("punto", parent=t_texto, leftIndent=12, bulletIndent=2, spaceAfter=2)
    t_nota = ParagraphStyle("nota", fontName="Helvetica", fontSize=7.5, leading=9.5, textColor=colors.HexColor(GRIS))

    def p(texto, estilo=t_texto, color=None, negrita=False):
        t = escape(str(texto or ""))
        if negrita:
            t = f"<b>{t}</b>"
        if color:
            t = f'<font color="{color}">{t}</font>'
        return Paragraph(t, estilo)

    ancho = A4[0] - 36 * mm - 12          # el marco de la página deja 6 pt de cada lado
    historia = []

    # ---------------------------------------------------------------- encabezado
    izq = []
    if taller.get("nombre"):
        izq.append(p(taller["nombre"], ParagraphStyle("taller", fontName="Helvetica-Bold", fontSize=13, leading=16)))
    if taller.get("contacto"):
        izq.append(p(taller["contacto"], t_nota))
    logo = None
    if taller.get("logo") and os.path.exists(taller["logo"]):
        try:
            from reportlab.lib.utils import ImageReader
            iw, ih = ImageReader(taller["logo"]).getSize()
            alto = 18 * mm
            logo = Image(taller["logo"], width=min(60 * mm, alto * iw / ih), height=alto * min(1, (60 * mm) / (alto * iw / ih)))
        except Exception:  # noqa: BLE001 - un logo roto no tiene que impedir el reporte
            logo = None
    if izq or logo:
        cab = Table([[izq or "", logo or ""]], colWidths=[ancho - 62 * mm, 62 * mm])
        cab.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                 ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor(GRIS)), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        historia += [cab, Spacer(1, 6)]

    historia.append(p(titulo_doc, t_titulo))
    inicio = datos.get("inicio") or time.time()
    filas = [("Fecha", time.strftime("%d/%m/%Y %H:%M", time.localtime(inicio))), ("Equipo", identificacion(equipo))]
    if datos.get("orden"):
        filas.insert(0, ("Orden", str(datos["orden"])))
    if datos.get("cliente"):
        filas.insert(1, ("Cliente", datos["cliente"]))
    if datos.get("fin"):
        seg = int(datos["fin"] - inicio)
        filas.append(("Duración", f"{seg // 3600} h {seg % 3600 // 60:02d} min" if seg >= 3600 else f"{seg // 60} min {seg % 60:02d} s"))
    t = Table([[p(k, t_tabla, negrita=True), p(v, t_tabla)] for k, v in filas], colWidths=[28 * mm, ancho - 28 * mm])
    t.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    historia += [t, Spacer(1, 8)]

    # ---------------------------------------------------------------- veredicto
    general = reparar.veredicto(resultados) if es_rep else veredicto_general(resultados)
    color_general = (ROJO if general in ("CON FALLAS", "QUEDARON PROBLEMAS") else VERDE if general in ("SIN FALLAS", "REPARADO", "SIN PROBLEMAS")
                     else AMBAR)
    caja = Table([[p(f"RESULTADO: {general}", ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=13, leading=16,
                                                              textColor=colors.white))],
                  [p(conclusion(resultados), ParagraphStyle("c", parent=t_texto, textColor=colors.white))]], colWidths=[ancho])
    caja.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color_general)),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (0, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 8)]))
    historia += [caja, Spacer(1, 6)]

    # ---------------------------------------------------------------- resumen
    historia.append(p("Resumen", t_sub))
    filas = [[p("Reparación" if es_rep else "Prueba", t_tabla, negrita=True), p("Resultado", t_tabla, negrita=True), p("Detalle", t_tabla, negrita=True)]]
    for r in resultados:
        filas.append([p(r.titulo, t_tabla), p(r.veredicto, t_tabla, col[r.veredicto], True), p(r.motivo, t_tabla)])
    t = Table(filas, colWidths=[45 * mm, 25 * mm, ancho - 70 * mm], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8c8c8")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee"))]))
    historia.append(t)

    que_hacer = acciones(resultados)
    if que_hacer:
        historia.append(p("Qué hacer", t_sub))
        historia += [Paragraph(escape(a), t_punto, bulletText="•") for a in que_hacer]
    if datos.get("notas"):
        historia.append(p("Notas del técnico", t_sub))
        historia.append(p(datos["notas"]))

    # ---------------------------------------------------------------- equipo
    historia.append(p("Datos del equipo", t_sub))
    for titulo, lineas in secciones_equipo(equipo):
        historia.append(p(titulo, t_sec))
        historia += [p(l, t_linea) for l in lineas]

    graf = _grafico_muestras(muestras or [], ancho)
    if graf:
        historia.append(p("Temperatura y carga durante el diagnóstico", t_sub))
        historia.append(graf)

    # ---------------------------------------------------------------- detalle
    historia.append(p("Detalle de cada reparación" if es_rep else "Detalle de cada prueba", t_sub))
    for r in resultados:
        bloque = [p(f"{r.titulo}: {r.veredicto}", t_sec, col[r.veredicto])]
        bloque += [p(l.strip(), t_linea2 if l.startswith("  ") else t_linea) for l in r.lineas]
        bloque += [p("Atención: " + a, t_linea, AMBAR) for a in r.advertencias]
        if r.motivo:
            bloque.append(p("Veredicto: " + r.motivo, t_linea, negrita=True))
        historia.append(KeepTogether(bloque[:12]))
        historia += bloque[12:]
        if r.grafico and r.grafico.get("tipo") == "velocidad_disco" and r.grafico.get("puntos"):
            historia.append(_grafico_disco(r.grafico["puntos"], ancho))
        historia.append(Spacer(1, 4))
    historia.append(Spacer(1, 10))
    if es_rep:
        historia.append(p("PASA: estaba bien o quedó reparado · FALLA: encontró un problema y no lo pudo reparar · NO EVALUADO: "
                          "la reparación no se pudo hacer (sin permisos, sin internet o se canceló).", t_nota))
    else:
        historia.append(p("PASA: sin fallas · FALLA: se encontró un problema · NO EVALUADO: la prueba no se pudo hacer "
                          "(falta una herramienta, permisos o se canceló), no significa que el componente esté mal.", t_nota))

    pie = (taller.get("nombre") + " · " if taller.get("nombre") else "") + f"Generado con OpenSTD Test {__version__}"

    def _pie(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor(GRIS))
        canvas.drawString(18 * mm, 10 * mm, pie)
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Página {doc.page}")
        canvas.restoreState()

    os.makedirs(os.path.dirname(os.path.abspath(ruta)), exist_ok=True)
    tmp = ruta + ".tmp"
    doc = SimpleDocTemplate(tmp, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
                            title=titulo_doc, author=taller.get("nombre") or "OpenSTD Test", creator=f"OpenSTD Test {__version__}")
    doc.build(historia, onFirstPage=_pie, onLaterPages=_pie)
    os.replace(tmp, ruta)


def _ejes(d, x0, y0, w, h, vmax, unidad, colors, String, Line):
    for i in range(5):
        y = y0 + h * i / 4
        d.add(Line(x0, y, x0 + w, y, strokeColor=colors.HexColor("#dddddd"), strokeWidth=0.4))
        d.add(String(x0 - 3, y - 2.5, f"{vmax * i / 4:.0f}", fontSize=6.5, textAnchor="end", fillColor=colors.HexColor(GRIS)))
    d.add(String(x0 - 3, y0 + h + 5, unidad, fontSize=6.5, textAnchor="end", fillColor=colors.HexColor(GRIS)))


def _grafico_disco(puntos: list, ancho: float):
    from reportlab.graphics.shapes import Drawing, Line, PolyLine, String
    from reportlab.lib import colors
    alto, x0, y0 = 95, 30, 14
    w, h = ancho - x0 - 6, alto - y0 - 12
    d = Drawing(ancho, alto)
    vmax = max(v for _, v in puntos) * 1.15 or 1
    _ejes(d, x0, y0, w, h, vmax, "MB/s", colors, String, Line)
    d.add(PolyLine([c for x, v in puntos for c in (x0 + w * x, y0 + h * v / vmax)], strokeColor=colors.HexColor(AZUL), strokeWidth=1))
    d.add(String(x0, 2, "inicio del disco", fontSize=6.5, fillColor=colors.HexColor(GRIS)))
    d.add(String(x0 + w, 2, "final del disco", fontSize=6.5, textAnchor="end", fillColor=colors.HexColor(GRIS)))
    return d


def _grafico_muestras(muestras: list[dict], ancho: float):
    series = {"cpu_temp": ("Temp. CPU °C", ROJO), "gpu_temp": ("Temp. GPU °C", AMBAR), "cpu": ("Uso CPU %", AZUL)}
    con_datos = {k: v for k, v in series.items() if sum(1 for m in muestras if m.get(k) is not None) >= 5}
    if not con_datos or len(muestras) < 5:
        return None
    from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
    from reportlab.lib import colors
    alto, x0, y0 = 120, 30, 26
    w, h = ancho - x0 - 6, alto - y0 - 12
    d = Drawing(ancho, alto)
    t0, t1 = muestras[0]["t"], muestras[-1]["t"]
    span = max(1, t1 - t0)
    vmax = 100
    _ejes(d, x0, y0, w, h, vmax, "°C / %", colors, String, Line)
    lx = x0
    for k, (nombre, color) in con_datos.items():
        pts = [c for m in muestras if m.get(k) is not None for c in (x0 + w * (m["t"] - t0) / span, y0 + h * min(m[k], vmax) / vmax)]
        d.add(PolyLine(pts, strokeColor=colors.HexColor(color), strokeWidth=0.9))
        d.add(Rect(lx, 4, 8, 4, fillColor=colors.HexColor(color), strokeColor=None))
        d.add(String(lx + 11, 3.5, nombre, fontSize=6.5, fillColor=colors.HexColor(GRIS)))
        lx += 80
    d.add(String(x0 + w, 14, f"{int(span // 60)} min", fontSize=6.5, textAnchor="end", fillColor=colors.HexColor(GRIS)))
    return d
