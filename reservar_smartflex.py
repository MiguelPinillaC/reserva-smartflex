#!/usr/bin/env python3
"""
Reserva Smart Flex — confirmación diaria, memoria de clase y cancelación por chat.

Tres modos:

  --modo preguntar   (8:00 p.m.)   Te pregunta si quieres clase, proponiendo
                                   la clase siguiente a la última que reservaste.

  --modo reservar    (12:15 a.m.)  Lee tu respuesta y reserva si dijiste que sí.

  --modo escuchar    (cada 10 min) Revisa si le escribiste 'cancelar' o 'estado'
                                   y actúa.

Nada se reserva ni se cancela sin que tú lo hayas pedido.

Secrets obligatorios: SMARTFLEX_DOC, SMARTFLEX_EMAIL, TELEGRAM_TOKEN,
TELEGRAM_CHAT_ID. Opcional: SMARTFLEX_DEVICE_ID.

El repositorio es publico, asi que NADA personal va en los archivos:
documento y correo viven solo en los secrets de GitHub.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = ("https://script.google.com/macros/s/"
       "AKfycbyj3pz-obEH1YJYmFASTwlLtZK_Qv5mkLFNFI5FGrsCivLbBndcxcIPcwHqFNO7I3DX/exec")

TZ = ZoneInfo("America/Bogota")
TIMEOUT = 20
INTENTOS = 3
ESPERA = 20

CONFIG_FILE = Path("config.json")
ESTADO_FILE = Path("estado.json")
DEVICE_FILE = Path(".device_id")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# En un repositorio publico los logs de Actions los puede leer cualquiera,
# asi que por defecto no se escribe el contenido de los mensajes.
# Pon LOG_DETALLADO=1 solo si estas depurando algo puntual.
DETALLE = os.environ.get("LOG_DETALLADO") == "1"

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

TEXTO_NO = "No reservar"


# ---------------------------------------------------------- utilidades

def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def fecha_bonita(d):
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def cargar(path, defecto):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log(f"{path} ilegible, uso valores por defecto.")
    return dict(defecto)


def guardar_estado(estado):
    ESTADO_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    log(f"estado.json actualizado: {estado}")


def enviar(texto, botones=None):
    log("-> " + (texto.replace("\n", " | ") if DETALLE else texto.split("\n")[0]))
    if not TG_TOKEN or not TG_CHAT:
        return
    cuerpo = {"chat_id": TG_CHAT, "text": texto}
    if botones:
        cuerpo["reply_markup"] = {
            "keyboard": [[{"text": b} for b in fila] for fila in botones],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        }
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json=cuerpo, timeout=TIMEOUT)
    except requests.RequestException as e:
        log(f"No pude enviar el mensaje: {e}")


def device_id():
    env = os.environ.get("SMARTFLEX_DEVICE_ID")
    if env:
        return env
    if DEVICE_FILE.exists():
        return DEVICE_FILE.read_text().strip()
    nuevo = str(uuid.uuid4())
    DEVICE_FILE.write_text(nuevo)
    return nuevo


# ---------------------------------------------------------- API de reservas

def api_get(action, **params):
    r = requests.get(API, params={"api": "1", "action": action, **params}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_post(action, **payload):
    r = requests.post(
        API,
        params={"api": "1"},
        headers={"Content-Type": "text/plain;charset=utf-8"},
        data=json.dumps({"action": action, **payload}),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def reserva_activa(documento):
    try:
        r = api_get("verifyStudentBooking", documento=documento)
        return r.get("booking")
    except (requests.RequestException, ValueError) as e:
        log(f"No pude consultar la reserva activa: {e}")
        return None


def fecha_de_reserva(b):
    """
    Fecha y hora de una reserva. La API devuelve fecha_clase en DD/MM/YYYY y
    hora_clase en 12 horas; isoBogota es mas confiable cuando viene.
    Devuelve None si no se puede interpretar.
    """
    if not b:
        return None

    iso = b.get("isoBogota")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        except ValueError:
            pass

    fecha = str(b.get("fecha_clase", "")).strip()
    hora = str(b.get("hora_clase", "")).strip().upper()
    for fmt in ("%d/%m/%Y %I:%M %p", "%Y-%m-%d %I:%M %p",
                "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{fecha} {hora}", fmt).replace(tzinfo=TZ)
        except ValueError:
            continue

    log(f"No pude interpretar la fecha de la reserva: {fecha!r} {hora!r}")
    return None


def ya_paso(b):
    """
    True si la clase ya ocurrio. El sistema sigue reportando como 'activas'
    las reservas vencidas, asi que estas no deben bloquear una nueva.
    Ante la duda (fecha ilegible) devuelve False y deja que decida el servidor.
    """
    cuando = fecha_de_reserva(b)
    if cuando is None:
        return False
    # Media hora de gracia por si la clase esta empezando justo ahora.
    return cuando < datetime.now(TZ) - timedelta(minutes=30)


def bloqueante(b):
    """La reserva solo estorba si todavia no ha ocurrido."""
    return b if (b and not ya_paso(b)) else None


def describir(b):
    return (f"{b.get('subnivel','')} clase {b.get('clase','')} - "
            f"{b.get('fecha_clase','')} {b.get('hora_clase','')}")


# ---------------------------------------------------------- Telegram entrante

def obtener_updates(limite=40):
    if not TG_TOKEN or not TG_CHAT:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"limit": limite}, timeout=TIMEOUT)
        return r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        log(f"No pude leer Telegram: {e}")
        return []


def mensajes_mios(updates):
    """Solo mensajes de texto de tu chat, en orden cronológico."""
    salida = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        texto = (msg.get("text") or "").strip()
        if texto:
            salida.append({"id": u.get("update_id", 0),
                           "fecha": msg.get("date", 0),
                           "texto": texto})
    if salida:
        log(f"<- {len(salida)} mensaje(s) tuyos"
            + (f": {[m['texto'] for m in salida]}" if DETALLE else ""))
    return salida


def ultimo_en_ventana(horas):
    limite = time.time() - horas * 3600
    for m in reversed(mensajes_mios(obtener_updates())):
        if m["fecha"] < limite:
            break
        return m["texto"]
    return None


def interpretar(texto, base):
    """Devuelve (parametros, quiere_reservar)."""
    cfg = dict(base)
    t = texto.lower().strip()

    if re.search(r"(no reservar|no gracias|saltar|omitir|^no\b|^nel\b)", t):
        return cfg, False

    ok = False

    m = re.search(r"\b([a-c][12]\.\d)\b", t)
    if m:
        cfg["subnivel"] = m.group(1).upper()
        ok = True

    m = re.search(r"clase\s*(\d+)", t)
    if m:
        cfg["clase"] = m.group(1)
        ok = True

    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        cfg["hora"] = f"{int(m.group(1)):02d}:{m.group(2)}"
        ok = True
    else:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1)) % 12
            if m.group(2) == "pm":
                h += 12
            cfg["hora"] = f"{h:02d}:00"
            ok = True

    if re.search(r"\b(si|sí|dale|listo|ok|claro|reserva|reservar)\b", t):
        ok = True

    return cfg, ok


# ---------------------------------------------------------- horarios

def hora_del_slot(slot):
    iso = slot.get("isoBogota")
    if iso:
        try:
            return (datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    .astimezone(TZ).strftime("%H:%M"))
        except ValueError:
            pass
    bruto = (slot.get("timeLabel") or slot.get("timeStr") or "").strip()
    p = bruto.split(":")
    if len(p) >= 2 and p[0].strip().isdigit():
        return f"{int(p[0]):02d}:{p[1][:2]}"
    return bruto


def obtener_slots(subnivel, fecha):
    slots = api_get("getSlotsForDate", subnivel=subnivel, dateStr=fecha)
    if isinstance(slots, dict):
        slots = slots.get("slots") or slots.get("result") or []
    return slots or []


# ---------------------------------------------------------- sugerencia de clase

def proxima_clase(cfg, estado, activa):
    """
    La clase que se propone hoy. Prioridad:
      1. Si hay reserva activa, la siguiente a esa.
      2. La siguiente a la ultima que reservaste (estado.json).
      3. Lo que diga config.json.
    """
    ultima, subnivel = None, cfg["subnivel"]

    if activa and str(activa.get("clase", "")).strip().isdigit():
        ultima = int(activa["clase"])
        subnivel = activa.get("subnivel") or subnivel
    elif estado.get("ultima_clase_reservada") is not None:
        ultima = int(estado["ultima_clase_reservada"])
        subnivel = estado.get("subnivel") or subnivel

    if ultima is None:
        return subnivel, str(cfg["clase"]), False

    siguiente = ultima + 1
    tope = cfg.get("max_clase")
    if tope and siguiente > int(tope):
        return subnivel, str(ultima), True   # llegaste al final del subnivel

    return subnivel, str(siguiente), False


# ---------------------------------------------------------- modos

def modo_preguntar(cfg, estado, documento):
    dias = int(cfg.get("dias_adelante", 1))
    objetivo = datetime.now(TZ) + timedelta(days=dias + 1)

    activa = reserva_activa(documento)
    if bloqueante(activa):
        enviar("Recordatorio: ya tienes una reserva activa\n"
               f"{describir(activa)}\n\n"
               "El sistema solo permite una a la vez, asi que esta noche no puedo "
               "programar otra. Escribeme 'cancelar' si quieres liberarla.")
        return

    if activa:
        log(f"Reserva vencida ignorada: {describir(activa)}")

    # Aunque ya haya pasado, sirve para saber en que clase vas.
    subnivel, clase, fin_subnivel = proxima_clase(cfg, estado, activa)

    if fin_subnivel:
        enviar(f"Ya reservaste la clase {clase}, que es la ultima de {subnivel}.\n"
               "Dime cual sigue, por ejemplo 'A2.1 clase 1 19:00'.")
        return

    sugeridas = cfg.get("horas_sugeridas") or [cfg["hora"]]
    botones = [sugeridas[i:i + 3] for i in range(0, len(sugeridas), 3)] + [[TEXTO_NO]]

    enviar(f"Quieres clase el {fecha_bonita(objetivo)}?\n\n"
           f"Te propongo: {subnivel} clase {clase}.\n"
           "Toca una hora, o escribeme algo como 'clase 4 8pm'.\n"
           "Si no respondes, no reservo nada.",
           botones=botones)


def modo_reservar(cfg, estado, documento):
    respuesta = ultimo_en_ventana(float(cfg.get("ventana_respuesta_horas", 5)))

    if not respuesta:
        if cfg.get("avisar_si_no_hubo_respuesta", True):
            enviar("No me respondiste anoche, asi que no reserve nada.")
        return

    activa_previa = reserva_activa(documento)
    subnivel_sug, clase_sug, _ = proxima_clase(cfg, estado, activa_previa)
    base = dict(cfg, subnivel=subnivel_sug, clase=clase_sug)

    params, quiere = interpretar(respuesta, base)
    if not quiere:
        enviar("Entendido, no reserve nada.")
        return

    subnivel, clase, hora = params["subnivel"], str(params["clase"]), params["hora"]
    dias = int(params.get("dias_adelante", 1))
    alternativa = bool(params.get("reservar_alternativa", False))
    objetivo = datetime.now(TZ) + timedelta(days=dias)
    fecha = objetivo.strftime("%Y-%m-%d")

    email = os.environ.get("SMARTFLEX_EMAIL", "").strip()
    if not email:
        enviar("No reserve: falta el secret SMARTFLEX_EMAIL con tu correo.")
        sys.exit(1)

    log(f"Objetivo: {subnivel} clase {clase}, {fecha} a las {hora}")

    sesion = api_get("login", documento=documento)
    if not sesion.get("ok"):
        enviar(f"El sistema rechazo el ingreso: {sesion.get('error','sin detalle')}")
        sys.exit(1)
    nombre = sesion.get("nombreCompleto", "")

    if bloqueante(activa_previa):
        enviar(f"No reserve: ya tienes una reserva activa\n{describir(activa_previa)}")
        return

    ultimo_error = None
    for intento in range(1, INTENTOS + 1):
        try:
            disponibles = {hora_del_slot(s): s for s in obtener_slots(subnivel, fecha)}
            log(f"Intento {intento}: {sorted(disponibles) or 'sin horas'}")

            elegido, hora_final = disponibles.get(hora), hora
            if not elegido and alternativa and disponibles:
                hora_final = sorted(disponibles)[0]
                elegido = disponibles[hora_final]

            if elegido:
                res = api_post("book",
                               subnivel=subnivel, slotId=elegido.get("slotId"), email=email,
                               clase=clase, slotIso=elegido.get("isoBogota", ""),
                               userTz="America/Bogota", dispositivo_id=device_id(),
                               documento=documento, name=nombre)
                if res.get("ok"):
                    if clase.isdigit():
                        estado["ultima_clase_reservada"] = int(clase)
                        estado["subnivel"] = subnivel
                        guardar_estado(estado)
                    extra = "" if hora_final == hora else f"\n(no habia a las {hora}, tome esta)"
                    enviar(f"RESERVADO\n{subnivel} clase {clase}\n"
                           f"{fecha_bonita(objetivo)} a las {hora_final}\n"
                           f"id: {res.get('bookingId','')}{extra}")
                    return
                if res.get("booking"):
                    enviar("No reserve: el sistema reporta una reserva activa tuya.")
                    return
                ultimo_error = res.get("error", "rechazado sin detalle")
            else:
                ultimo_error = (f"no hay cupo a las {hora}. Disponibles: "
                                f"{', '.join(sorted(disponibles)) if disponibles else 'ninguna'}")
        except requests.RequestException as e:
            ultimo_error = f"error de conexion ({e})"

        if intento < INTENTOS:
            time.sleep(ESPERA)

    enviar(f"NO RESERVADO\n{subnivel} clase {clase} - {fecha_bonita(objetivo)} a las {hora}\n"
           f"Motivo: {ultimo_error}")


def cancelar(documento, estado):
    activa = reserva_activa(documento)
    if not activa:
        enviar("No tienes ninguna reserva activa para cancelar.")
        return

    if ya_paso(activa):
        enviar(f"No cancele nada: esa clase ya se dicto.\n{describir(activa)}\n"
               "El sistema la sigue mostrando, pero no te bloquea para reservar otra.")
        return

    res = api_post("cancel",
                   bookingId=activa.get("bookingId"),
                   token=activa.get("cancelToken", ""),
                   source="telegram_bot")

    if res.get("ok"):
        clase = str(activa.get("clase", "")).strip()
        if clase.isdigit() and estado.get("ultima_clase_reservada") == int(clase):
            estado["ultima_clase_reservada"] = int(clase) - 1 or None
            guardar_estado(estado)
        enviar(f"CANCELADO\n{describir(activa)}\nTu cupo quedo liberado.")
    else:
        enviar(f"No pude cancelar: {res.get('error','sin detalle')}\n"
               "Puedes hacerlo desde la pagina del curso.")


def modo_escuchar(cfg, estado, documento):
    """Revisa mensajes nuevos y atiende 'cancelar' o 'estado'."""
    ultimo_visto = int(estado.get("ultimo_update_procesado", 0))
    limite = time.time() - float(cfg.get("ventana_comandos_horas", 2)) * 3600

    pendientes = [m for m in mensajes_mios(obtener_updates())
                  if m["id"] > ultimo_visto and m["fecha"] >= limite]
    if not pendientes:
        log("Sin mensajes nuevos.")
        return

    mayor = max(m["id"] for m in pendientes)
    accion = None
    for m in pendientes:
        t = m["texto"].lower().strip(" .!¡¿?")
        if re.fullmatch(r"(cancelar|cancela|cancelar clase|cancelar reserva)", t):
            accion = "cancelar"
        elif re.fullmatch(r"(estado|que tengo|qué tengo|mi reserva|reserva)", t):
            accion = "estado"

    estado["ultimo_update_procesado"] = mayor
    guardar_estado(estado)

    if accion == "cancelar":
        cancelar(documento, estado)
    elif accion == "estado":
        activa = reserva_activa(documento)
        enviar(f"Tu reserva activa:\n{describir(activa)}" if activa
               else "No tienes ninguna reserva activa.")
    else:
        log("Mensajes nuevos, ninguno es un comando.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["preguntar", "reservar", "escuchar"], required=True)
    args = ap.parse_args()

    documento = os.environ.get("SMARTFLEX_DOC")
    if not documento:
        enviar("Falta la variable SMARTFLEX_DOC.")
        sys.exit(1)

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    estado = cargar(ESTADO_FILE, {"ultima_clase_reservada": None,
                                  "subnivel": cfg["subnivel"],
                                  "ultimo_update_procesado": 0})

    {"preguntar": modo_preguntar,
     "reservar": modo_reservar,
     "escuchar": modo_escuchar}[args.modo](cfg, estado, documento)


if __name__ == "__main__":
    main()
