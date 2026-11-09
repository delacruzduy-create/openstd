"""Orquestador: lee el equipo, corre las pruebas elegidas en orden, arma el PDF, lo guarda en el historial
y lo manda por correo. Lo usan el panel web y el modo consola."""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

from . import correo, plataforma, reporte
from .config import Config, destinatarios
from .pruebas import Contexto, bateria, cpu, disco, gpu, ram, sistema
from .resultado import FALLA, NO_EVALUADO, PASA, Progreso, Resultado, peor, veredicto_general
from .sensores import Muestreador


def _limpio(texto, largo: int) -> str:
    return " ".join(str(texto or "").split())[:largo]


def _minutos(v, defecto: float) -> float:
    try:
        return max(0.05, min(float(v), 600.0))
    except (TypeError, ValueError):
        return defecto


class Historial:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def guardar(self, reg: dict) -> None:
        os.makedirs(self.cfg.carpeta_historial, exist_ok=True)
        tmp = os.path.join(self.cfg.carpeta_historial, reg["id"] + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.cfg.carpeta_historial, reg["id"] + ".json"))

    def leer(self, id_: str) -> dict | None:
        if not id_ or not all(c.isalnum() or c in "-_" for c in id_):
            return None
        try:
            with open(os.path.join(self.cfg.carpeta_historial, id_ + ".json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def lista(self, n: int = 50) -> list[dict]:
        try:
            nombres = sorted((x for x in os.listdir(self.cfg.carpeta_historial) if x.endswith(".json")), reverse=True)[:n]
        except OSError:
            return []
        r = []
        for nombre in nombres:
            reg = self.leer(nombre[:-5])
            if reg:
                r.append({k: reg.get(k) for k in ("id", "inicio", "fin", "orden", "cliente", "equipo", "veredicto", "correo")}
                         | {"hay_pdf": bool(reg.get("archivo") and os.path.exists(self.ruta_pdf(reg)))})
        return r

    def ruta_pdf(self, reg: dict) -> str:
        return os.path.join(self.cfg.carpeta_reportes, os.path.basename(reg.get("archivo") or ""))


def armar_correo(reg: dict, taller: str = "") -> tuple[str, str]:
    res = [Resultado.de_dict(r) for r in reg.get("resultados") or []]
    cab = "Reporte de diagnóstico" + (f" de la orden {reg['orden']}" if reg.get("orden") else "") + \
          (f" - {reg['cliente']}" if reg.get("cliente") else "")
    cuerpo = [cab + ".", "", f"Equipo: {reg.get('equipo')}",
              f"Fecha: {time.strftime('%d/%m/%Y %H:%M', time.localtime(reg.get('inicio') or time.time()))}", ""]
    cuerpo += [f"- {r.titulo}: {r.veredicto}" + (f" ({r.motivo})" if r.motivo else "") for r in res]
    cuerpo += ["", f"RESULTADO: {reg.get('veredicto')}", "", reporte.conclusion(res)]
    que = reporte.acciones(res)
    if que:
        cuerpo += ["", "Qué hacer:"] + [f"- {a}" for a in que]
    cuerpo += ["", "El detalle completo está en el PDF adjunto."]
    if taller:
        cuerpo += ["", taller]
    asunto = f"[Diagnóstico: {reg.get('veredicto')}]" + (f" Orden {reg['orden']}" if reg.get("orden") else "") + f" - {reg.get('equipo')}"
    return asunto, "\n".join(cuerpo)


class Diagnostico:
    def __init__(self, cfg: Config, plat=None, muestreador: Muestreador | None = None):
        self.cfg = cfg
        self._plat = plat
        self.muestreador = muestreador or Muestreador(plat=plat)
        self.historial = Historial(cfg)
        self.cola = correo.Cola(cfg)
        self._lock = threading.Lock()
        self.equipo: dict | None = None
        self.equipo_error = ""
        self._cargando = False
        self._gpu_ops: list[dict] = []
        self.gpu_motivo = ""
        self.listar_gpu = gpu.listar_adaptadores          # los tests lo reemplazan
        self.progreso = Progreso()
        self.estado: dict = {"corriendo": False}
        self._hilo: threading.Thread | None = None

    @property
    def plat(self):
        return self._plat or plataforma.actual()

    # ------------------------------------------------------------------ equipo
    def cargar_equipo(self, esperar: bool = False, fresco: bool = False) -> None:
        with self._lock:
            if self._cargando or (self.equipo and not fresco):
                return
            self._cargando = True

        def leer():
            try:
                self.equipo = self.plat.equipo()
                self.equipo_error = ""
                adaptadores, self.gpu_motivo = self.listar_gpu()
                externas = self.plat.gpu_opciones(self.cfg.carpetas_herramientas, self.cfg.get("gpu_comando"))
                for o in externas:
                    o["id"] = f"ext:{o['id']}"
                self._gpu_ops = gpu.opciones_propias(adaptadores) + externas
            except Exception as e:  # noqa: BLE001
                self.equipo_error = f"No se pudieron leer los datos del equipo: {e}"
                traceback.print_exc()
            finally:
                self._cargando = False
        if esperar:
            leer()
        else:
            threading.Thread(target=leer, daemon=True, name="equipo").start()

    def opciones(self) -> dict:
        e = self.equipo
        resumen = None
        if e:
            resumen = {"identificacion": reporte.identificacion(e), "sistema": e.get("sistema"),
                       "cpu": (e.get("cpu") or {}).get("nombre"), "ram": (e.get("ram") or {}).get("instalada"),
                       "discos": [{"id": d["id"], "modelo": d["modelo"], "tam": d["tam"], "tipo": d["tipo"], "es_usb": d["es_usb"],
                                   "es_sistema": d["es_sistema"]} for d in e.get("discos") or []],
                       "video": [v["nombre"] for v in e.get("video") or []], "bateria": bool(e.get("baterias")),
                       "es_portatil": e.get("es_portatil")}
        ref = self.cfg.referencia(self.plat.clave_equipo(e)) if e else None
        return {"admin": self.plat.es_admin(), "plataforma": self.plat.nombre, "cargando": self._cargando, "error": self.equipo_error,
                "equipo": resumen, "gpu": [{"id": o["id"], "nombre": o["nombre"]} for o in self._gpu_ops], "gpu_motivo": self.gpu_motivo,
                "minutos": {k: self.cfg.get(f"minutos_{k}") for k in ("cpu", "ram", "gpu")}, "disco_modo": self.cfg.get("disco_modo"),
                "correo_listo": self.cfg.correo_listo(),
                "enviar_al_terminar": self.cfg.get("enviar_al_terminar"), "referencia": ref}

    # ------------------------------------------------------------------ ejecución
    def iniciar(self, op: dict) -> str | None:
        """Arranca el diagnóstico en segundo plano. Devuelve un texto de error o None."""
        with self._lock:
            if self.estado.get("corriendo"):
                return "Ya hay un diagnóstico corriendo"
            if not self.equipo:
                return "Todavía se están leyendo los datos del equipo: esperá unos segundos"
            pasos = self._pasos(op.get("pruebas") or {})
            if not pasos:
                return "Elegí al menos una prueba"
            self.progreso = Progreso()
            self.estado = {"corriendo": True, "id": time.strftime("%Y%m%d-%H%M%S"), "orden": _limpio(op.get("orden"), 40),
                           "cliente": _limpio(op.get("cliente"), 120), "correo_cliente": _limpio(op.get("correo_cliente"), 200),
                           "notas": str(op.get("notas") or "").strip()[:2000], "enviar": bool(op.get("enviar", True)),
                           "pasos": pasos, "actual": -1, "inicio": time.time(), "fin": None, "resultados": [], "veredicto": "",
                           "archivo": "", "error": "", "conclusion": "", "correo": None}
            if self.estado["correo_cliente"] and not destinatarios(self.estado["correo_cliente"]):
                self.estado = {"corriendo": False}
                return "El correo del cliente no es válido"
        self._hilo = threading.Thread(target=self._correr, daemon=True, name="diagnostico")
        self._hilo.start()
        return None

    def esperar(self) -> None:
        if self._hilo:
            self._hilo.join()

    def cancelar(self) -> bool:
        if self.estado.get("corriendo"):
            self.progreso.cancelar.set()
            return True
        return False

    def _pasos(self, pr: dict) -> list[dict]:
        e = self.equipo or {}
        pasos = []
        if pr.get("sistema", True):
            pasos.append({"clave": "sistema", "titulo": "Sistema operativo", "estimado": 30})
        if pr.get("bateria", True) and (e.get("baterias") or 0) > 0:
            pasos.append({"clave": "bateria", "titulo": "Batería", "estimado": 5})
        ids = pr.get("discos")
        for d in e.get("discos") or []:
            elegido = (str(d["id"]) in [str(x) for x in ids]) if isinstance(ids, list) else (pr.get("disco", True) and not d["es_usb"])
            if elegido:
                pasos.append({"clave": "disco", "titulo": f"Disco {d['id']}", "estimado": 120, "disco": d["id"],
                              "completo": (pr.get("disco_modo") or self.cfg.get("disco_modo")) == "completo"})
        for clave, titulo in (("cpu", "Procesador"), ("ram", "Memoria RAM"), ("gpu", "Placa de video")):
            if pr.get(clave):
                minutos = _minutos(pr[clave], self.cfg.get(f"minutos_{clave}"))
                pasos.append({"clave": clave, "titulo": titulo, "minutos": minutos, "estimado": int(minutos * 60) + 5,
                              "gpu_opcion": pr.get("gpu_opcion")})
        for paso in pasos:
            paso.update(estado="pendiente", veredicto="", motivo="")
        return pasos

    def _correr(self) -> None:
        e = self.equipo
        resultados: list[Resultado] = []
        ctx = Contexto(equipo=e, config=self.cfg, plat=self.plat, muestreador=self.muestreador, progreso=self.progreso)
        self.muestreador.iniciar()
        try:
            self.plat.mantener_despierto(True)
        except Exception:  # noqa: BLE001
            pass
        self.estado["marca"] = self.muestreador.marca()
        try:
            for i, paso in enumerate(self.estado["pasos"]):
                if self.progreso.cancelado:
                    paso["estado"] = "saltado"
                    continue
                self.estado["actual"] = i
                paso.update(estado="corriendo", inicio=time.time())
                self.progreso.set_porcentaje(0)
                try:
                    nuevos = self._ejecutar(paso, ctx)
                except Exception as ex:  # noqa: BLE001 - un error del programa es NO EVALUADO, nunca FALLA
                    traceback.print_exc()
                    nuevos = [Resultado(paso["clave"].upper(), paso["titulo"], NO_EVALUADO, f"Error del programa: {ex}")]
                resultados += nuevos
                self.estado["resultados"] = [r.a_dict() for r in resultados]
                v = peor([r.veredicto for r in nuevos]) if nuevos else ""
                paso.update(estado="listo", veredicto=v, fin=time.time(),
                            motivo=("; ".join(r.motivo for r in nuevos if r.veredicto == v) if nuevos else "No aplica")[:240])
            self._cerrar(resultados)
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.estado["error"] = f"Error del programa: {ex}"
        finally:
            self.muestreador.detener()
            try:
                self.plat.mantener_despierto(False)
            except Exception:  # noqa: BLE001
                pass
            self.estado.update(corriendo=False, actual=-1, fin=self.estado.get("fin") or time.time())

    def _ejecutar(self, paso: dict, ctx: Contexto) -> list[Resultado]:
        c = paso["clave"]
        if c == "sistema":
            return [sistema.ejecutar(ctx)]
        if c == "bateria":
            return bateria.ejecutar(ctx)
        if c == "disco":
            d = next(x for x in self.equipo["discos"] if x["id"] == paso["disco"])
            return [disco.ejecutar(ctx, d, paso.get("completo"))]
        if c == "cpu":
            return [cpu.ejecutar(ctx, paso["minutos"] * 60)]
        if c == "ram":
            return [ram.ejecutar(ctx, paso["minutos"] * 60)]
        if c == "gpu":
            op = next((o for o in self._gpu_ops if str(o["id"]) == str(paso.get("gpu_opcion"))), self._gpu_ops[0] if self._gpu_ops else None)
            return gpu.ejecutar(ctx, paso["minutos"] * 60, op)
        return []

    def _cerrar(self, resultados: list[Resultado]) -> None:
        est = self.estado
        est["fin"] = time.time()
        self.progreso.set_fase("Armando el reporte PDF...")
        est["veredicto"] = veredicto_general(resultados)
        est["conclusion"] = reporte.conclusion(resultados)
        limpio = "".join(c for c in est["orden"] if c.isalnum() or c in "-_")
        nombre = f"diagnostico_{est['id']}" + (f"_orden{limpio}" if limpio else "") + ".pdf"
        ruta = os.path.join(self.cfg.carpeta_reportes, nombre)
        v = self.cfg.cargar()
        try:
            reporte.generar(ruta, est, resultados, self.equipo, self.muestreador.desde(est["marca"]),
                            {"nombre": v["taller_nombre"], "contacto": v["taller_contacto"], "logo": v["logo"]})
            est["archivo"] = nombre
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            est["error"] = f"No se pudo generar el PDF: {ex}"
        reg = {k: est.get(k) for k in ("id", "inicio", "fin", "orden", "cliente", "correo_cliente", "notas", "veredicto", "archivo")}
        reg["equipo"] = reporte.identificacion(self.equipo)
        reg["resultados"] = [r.a_dict() for r in resultados]
        if est["archivo"] and est["enviar"]:
            self.progreso.set_fase("Mandando el reporte por correo...")
            est["correo"] = self.enviar(reg)
        else:
            est["correo"] = {"estado": "no", "texto": "No se pidió mandarlo por correo" if est["archivo"] else "No hay PDF para mandar"}
        reg["correo"] = est["correo"]
        self.historial.guardar(reg)
        self.progreso.set_fase("Listo")
        self.progreso.set_porcentaje(100)

    def enviar(self, reg: dict) -> dict:
        """Manda el PDF ya. Si no sale, queda en la cola y se reintenta solo."""
        if not self.cfg.correo_listo():
            return {"estado": "error", "texto": "El correo no está configurado (Ajustes > Correo): el PDF quedó guardado"}
        asunto, cuerpo = armar_correo(reg, self.cfg.get("taller_nombre"))
        pdf = self.historial.ruta_pdf(reg)
        extra = reg.get("correo_cliente") or ""
        try:
            correo.enviar(self.cfg, correo.armar(self.cfg, asunto, cuerpo, [pdf], extra))
        except correo.ErrorCorreo as e:
            self.cola.agregar(asunto, cuerpo, pdf, extra)
            return {"estado": "error", "texto": f"{e}. Quedó en espera y se reintenta solo."}
        return {"estado": "enviado", "texto": "Enviado a " + ", ".join(destinatarios(self.cfg.get("destinatarios"), extra))}

    def reenviar(self, id_: str, extra: str = "") -> dict:
        reg = self.historial.leer(id_)
        if not reg or not reg.get("archivo"):
            return {"estado": "error", "texto": "No existe ese reporte"}
        if extra:
            reg["correo_cliente"] = extra
        r = self.enviar(reg)
        reg["correo"] = r
        self.historial.guardar(reg)
        if self.estado.get("id") == id_:
            self.estado["correo"] = r
        return r

    # ------------------------------------------------------------------ estado para el panel
    def estado_publico(self, desde: float = 0) -> dict:
        est = {k: v for k, v in self.estado.items() if k != "marca"}
        est["fase"] = self.progreso.fase
        est["porcentaje"] = self.progreso.porcentaje
        est["log"] = self.progreso.ultimas(30)
        marca = max(desde, self.estado.get("marca") or 0)
        est["muestras"] = [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in m.items()}
                           for m in self.muestreador.desde(marca)[-600:]] if self.estado.get("pasos") else []
        est["ahora"] = time.time()
        return est

    def contar_desde_hoy(self, motivo: str = "marcado a mano en el panel") -> str | None:
        if not self.equipo:
            return "Todavía se están leyendo los datos del equipo"
        self.cfg.poner_referencia(self.plat.clave_equipo(self.equipo), time.time(), motivo)
        return None


__all__ = ["Diagnostico", "Historial", "armar_correo", "PASA", "FALLA", "NO_EVALUADO"]
