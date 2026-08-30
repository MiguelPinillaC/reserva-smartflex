#!/usr/bin/env python3
"""
Reserva Smart Flex — reserva inmediata por chat.

El sistema del curso no deja programar una clase nueva mientras tengas otra
sin ver. Como las clases son de 7 a 9 p.m., a medianoche siempre hay una
pendiente: por eso reservar de madrugada nunca fue posible. El momento util
es justo despues de terminar la clase.

  --modo escuchar   (cada minuto)  Atiende tus mensajes y reserva de una.
                                   Tambien ejecuta las citas que dejaste puestas.
  --modo recordar   (9:20 p.m.)    Si no has programado la de manana, pregunta.

Que le puedes escribir:

  19:00                      reserva la clase siguiente manana a esa hora
  clase 12 7pm               clase y hora especificas
  lunes 7pm                  para un dia concreto
  entra el domingo 00:15     deja una cita: entra ese dia y reserva
  cancelar / estado / hola

Secrets: SMARTFLEX_DOC, SMARTFLEX_EMAIL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
Opcional: SMARTFLEX_DEVICE_ID.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = ("https://script.google.com/macros/s/"
       "AKfycbyj3pz-obEH1YJYmFASTwlLtZK_Qv5mkLFNFI5FGrsCivLbBndcxcIPcwHqFNO7I3DX/exec")

TZ = ZoneInfo("America/Bogota")
TIMEOUT = 20
INTENTOS = 3
ESPERA = 15

CONFIG_FILE = Path("config.json")
ESTADO_FILE = Path("estado.json")
DEVICE_FILE = Path(".device_id")

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DETALLE = os.environ.get("LOG_DETALLADO") == "1"

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]

TEXTO_NO = "No reservar"
TEXTO_CANCELAR = "Cancelar reserva"
TEXTO_ESTADO = "Ver estado"


# ---------------------------------------------------------- utilidades

def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def fecha_bonita(d):
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def enviar(texto, botones=None):
    log("-> " + (texto.replace("\n", " | ") if DETALLE else texto.split("\n")[0]))
    if not TG_TOKEN or not TG_CHAT:
        return
    cuerpo = {"chat_id": TG_CHAT, "text": texto}
    if botones:
        cuerpo["reply_markup"] = {
            "keyboard": [[{"text": b} for b in fila] for fila in botones],
            "one_time_keyboard": True, "resize_keyboard": True,
        }
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json=cuerpo, timeout=TIMEOUT)
    except requests.RequestException as e:
        log(f"No pude enviar el mensaje: {e}")


def guardar_estado(estado):
    ESTADO_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    log("estado.json actualizado")


def device_id():
    env = os.environ.get("SMARTFLEX_DEVICE_ID")
    if env:
        return env
    if DEVICE_FILE.exists():
        return DEVICE_FILE.read_text().strip()
    nuevo = str(uuid.uuid4())
    DEVICE_FILE.write_text(nuevo)
    return nuevo


# ---------------------------------------------------------- festivos

def _pascua(anio):
    a = anio % 19; b = anio // 100; c = anio % 100
    d = b // 4; e = b % 4; f = (b + 8) // 25
    g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(anio, mes, dia)


def _lunes_siguiente(d):
    """Ley Emiliani: si no cae lunes, se traslada al lunes siguiente."""
    return d + timedelta(days=(7 - d.weekday()) % 7)


def festivos_colombia(anio):
    p = _pascua(anio)
    fijos = [date(anio, 1, 1), date(anio, 5, 1), date(anio, 7, 20),
             date(anio, 8, 7), date(anio, 12, 8), date(anio, 12, 25)]
    emiliani = [date(anio, 1, 6), date(anio, 3, 19), date(anio, 6, 29),
                date(anio, 8, 15), date(anio, 10, 12), date(anio, 11, 1),
                date(anio, 11, 11)]
    santos = [p - timedelta(days=3), p - timedelta(days=2)]
    moviles = [p + timedelta(days=43), p + timedelta(days=64), p + timedelta(days=71)]
    return set(fijos + santos
               + [_lunes_siguiente(d) for d in emiliani]
               + [_lunes_siguiente(d) for d in moviles])


def sin_clases(d, cfg):
    """Domingos y festivos colombianos. Devuelve el motivo, o None."""
    dia = d.date() if isinstance(d, datetime) else d
    if dia.weekday() == 6 and cfg.get("sin_clases_domingo", True):
        return "es domingo"
    if cfg.get("sin_clases_festivos", True) and dia in festivos_colombia(dia.year):
        return "es festivo en Colombia"
    return None


def proximo_dia_habil(desde, cfg):
    d = desde
    for _ in range(14):
        if not sin_clases(d, cfg):
            return d
        d += timedelta(days=1)
    return desde


# ---------------------------------------------------------- API de reservas

def api_get(action, **params):
    r = requests.get(API, params={"api": "1", "action": action, **params}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def api_post(action, **payload):
    r = requests.post(API, params={"api": "1"},
                      headers={"Content-Type": "text/plain;charset=utf-8"},
                      data=json.dumps({"action": action, **payload}), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def reserva_activa(documento):
    try:
        return api_get("verifyStudentBooking", documento=documento).get("booking")
    except (requests.RequestException, ValueError) as e:
        log(f"No pude consultar la reserva activa: {e}")
        return None


def describir(b):
    return (f"{b.get('subnivel','')} clase {b.get('clase','')} - "
            f"{b.get('fecha_clase','')} {b.get('hora_clase','')}")


def fecha_de_reserva(b):
    if not b:
        return None
    iso = b.get("isoBogota")
    if iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        except ValueError:
            pass
    f = str(b.get("fecha_clase", "")).strip()
    h = str(b.get("hora_clase", "")).strip().upper()
    for fmt in ("%d/%m/%Y %I:%M %p", "%Y-%m-%d %I:%M %p",
                "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{f} {h}", fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    log("No pude interpretar la fecha de la reserva")
    return None


def ya_paso(b):
    """El sistema sigue reportando como activas las clases ya dictadas."""
    cuando = fecha_de_reserva(b)
    return False if cuando is None else cuando < datetime.now(TZ) - timedelta(minutes=30)


def bloqueante(b):
    return b if (b and not ya_paso(b)) else None


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
    return f"{int(p[0]):02d}:{p[1][:2]}" if len(p) >= 2 and p[0].strip().isdigit() else bruto


def obtener_slots(subnivel, fecha):
    slots = api_get("getSlotsForDate", subnivel=subnivel, dateStr=fecha)
    if isinstance(slots, dict):
        slots = slots.get("slots") or slots.get("result") or []
    return slots or []


# ---------------------------------------------------------- Telegram entrante

def obtener_updates(limite=60):
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
    salida = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        texto = (msg.get("text") or "").strip()
        if texto:
            salida.append({"id": u.get("update_id", 0),
                           "fecha": msg.get("date", 0), "texto": texto})
    if salida:
        log(f"<- {len(salida)} mensaje(s)"
            + (f": {[m['texto'] for m in salida]}" if DETALLE else ""))
    return salida


# ---------------------------------------------------------- interpretacion

def interpretar_hora(t):
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12
        return f"{h + (12 if m.group(2) == 'pm' else 0):02d}:00"
    return None


def interpretar_dia(t, ahora=None):
    """'manana', 'lunes', '29/08', '29'. Devuelve date o None."""
    ahora = ahora or datetime.now(TZ)
    hoy = ahora.date()

    if re.search(r"pasado\s*ma[nñ]ana", t):
        return hoy + timedelta(days=2)
    if re.search(r"\bma[nñ]ana\b", t):
        return hoy + timedelta(days=1)
    if re.search(r"\bhoy\b", t):
        return hoy

    for i, nombre in enumerate(DIAS):
        patron = nombre.replace("miercoles", "mi[eé]rcoles").replace("sabado", "s[aá]bado")
        if re.search(rf"\b{patron}\b", t):
            delta = (i - hoy.weekday()) % 7
            return hoy + timedelta(days=delta or 7)

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        d, mes = int(m.group(1)), int(m.group(2))
        try:
            cand = date(hoy.year, mes, d)
            return cand if cand >= hoy else date(hoy.year + 1, mes, d)
        except ValueError:
            return None
    return None


def interpretar(texto, base):
    """
    Devuelve (parametros, veredicto):
      'si'   trae datos concretos -> reservar de una
      'menu' dijo que si, pero sin decir que -> hay que preguntarle
      'no'   dijo que no
      '?'    no se entiende
    """
    cfg = dict(base)
    t = texto.lower().strip()

    if re.search(r"(no reservar|no gracias|saltar|omitir|^no\b|^nel\b)", t):
        return cfg, "no"

    concreto = False
    m = re.search(r"\b([a-c][12]\.\d)\b", t)
    if m:
        cfg["subnivel"] = m.group(1).upper(); concreto = True
    m = re.search(r"clase\s*(\d+)", t)
    if m:
        cfg["clase"] = m.group(1); concreto = True
    h = interpretar_hora(t)
    if h:
        cfg["hora"] = h; concreto = True
    d = interpretar_dia(t)
    if d:
        cfg["fecha"] = d.strftime("%Y-%m-%d"); concreto = True

    if concreto:
        return cfg, "si"
    if re.search(r"\b(si|sí|dale|listo|ok|claro|reserva|reservar|programa|programar)\b", t):
        return cfg, "menu"
    return cfg, "?"


def es_comando(texto, patron):
    return bool(re.fullmatch(patron, texto.lower().strip(" .!¡¿?")))


# ---------------------------------------------------------- clase sugerida

def proxima_clase(cfg, estado, activa):
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
        return subnivel, str(ultima), True
    return subnivel, str(siguiente), False


# ---------------------------------------------------------- reserva inmediata

def reservar_ahora(cfg, estado, documento, subnivel, clase, hora, fecha_obj):
    """Reserva ya mismo. Devuelve True si quedo."""
    motivo = sin_clases(fecha_obj, cfg)
    if motivo:
        alterno = proximo_dia_habil(fecha_obj + timedelta(days=1), cfg)
        enviar(f"El {fecha_bonita(fecha_obj)} no hay clases porque {motivo}.\n"
               f"El siguiente dia habil es el {fecha_bonita(alterno)}. "
               "Escribeme la hora si quieres que lo intente ahi.")
        return False

    activa = reserva_activa(documento)
    if bloqueante(activa):
        cuando = fecha_de_reserva(activa)
        enviar("No puedo reservar todavia: tienes una clase pendiente sin ver.\n"
               f"{describir(activa)}\n\n"
               + (f"Escribeme despues de las {(cuando + timedelta(hours=2)):%H:%M} "
                  "y la programo de una." if cuando else
                  "Escribeme cuando ya la hayas visto."))
        return False

    email = os.environ.get("SMARTFLEX_EMAIL", "").strip()
    if not email:
        enviar("No reserve: falta el secret SMARTFLEX_EMAIL.")
        return False

    sesion = api_get("login", documento=documento)
    if not sesion.get("ok"):
        enviar(f"El sistema rechazo el ingreso: {sesion.get('error','sin detalle')}")
        return False
    nombre = sesion.get("nombreCompleto", "")

    fecha = fecha_obj.strftime("%Y-%m-%d")
    alternativa = bool(cfg.get("reservar_alternativa", False))
    ultimo_error = None

    for intento in range(1, INTENTOS + 1):
        try:
            disponibles = {hora_del_slot(s): s for s in obtener_slots(subnivel, fecha)}
            log(f"Intento {intento} para {fecha}: {sorted(disponibles) or 'sin horas'}")

            elegido, hora_final = disponibles.get(hora), hora
            if not elegido and alternativa and disponibles:
                hora_final = sorted(disponibles)[0]
                elegido = disponibles[hora_final]

            if elegido:
                res = api_post("book", subnivel=subnivel, slotId=elegido.get("slotId"),
                               email=email, clase=clase,
                               slotIso=elegido.get("isoBogota", ""),
                               userTz="America/Bogota", dispositivo_id=device_id(),
                               documento=documento, name=nombre)
                if res.get("ok"):
                    if str(clase).isdigit():
                        estado["ultima_clase_reservada"] = int(clase)
                        estado["subnivel"] = subnivel
                    guardar_estado(estado)
                    extra = "" if hora_final == hora else f"\n(no habia a las {hora}, tome esta)"
                    enviar(f"RESERVADO\n{subnivel} clase {clase}\n"
                           f"{fecha_bonita(fecha_obj)} a las {hora_final}\n"
                           f"id: {res.get('bookingId','')}{extra}")
                    return True
                if res.get("booking"):
                    enviar("No reserve: el sistema reporta una reserva activa tuya.")
                    return False
                ultimo_error = res.get("error", "rechazado sin detalle")
            else:
                ultimo_error = (f"no hay cupo a las {hora}. Disponibles: "
                                f"{', '.join(sorted(disponibles)) if disponibles else 'ninguna'}")
        except requests.RequestException as e:
            ultimo_error = f"error de conexion ({e})"

        if intento < INTENTOS:
            time.sleep(ESPERA)

    enviar(f"NO RESERVADO\n{subnivel} clase {clase} - {fecha_bonita(fecha_obj)} a las {hora}\n"
           f"Motivo: {ultimo_error}\n(ejecutado {datetime.now(TZ):%H:%M})")
    return False


# ---------------------------------------------------------- citas programadas

def poner_cita(estado, cuando, subnivel=None, clase=None, hora=None, fecha=None):
    estado["cita"] = {"cuando": cuando.strftime("%Y-%m-%d %H:%M"),
                      "subnivel": subnivel, "clase": clase,
                      "hora": hora, "para": fecha.strftime("%Y-%m-%d") if fecha else None}
    guardar_estado(estado)


def cita_pendiente(estado):
    c = estado.get("cita")
    if not c or not c.get("cuando"):
        return None
    try:
        cuando = datetime.strptime(c["cuando"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return None
    ahora = datetime.now(TZ)
    if cuando > ahora:
        return None
    if ahora - cuando > timedelta(hours=12):
        log("Cita demasiado vieja, la descarto.")
        estado["cita"] = None
        guardar_estado(estado)
        return None
    return c


def ejecutar_cita(cfg, estado, documento, c):
    log(f"Ejecutando cita de las {c['cuando']}")
    activa = reserva_activa(documento)
    s, cl, _ = proxima_clase(cfg, estado, activa)
    subnivel = c.get("subnivel") or s
    clase = c.get("clase") or cl
    hora = c.get("hora") or cfg["hora"]
    fecha = (datetime.strptime(c["para"], "%Y-%m-%d").date() if c.get("para")
             else datetime.now(TZ).date() + timedelta(days=1))

    estado["cita"] = None
    guardar_estado(estado)
    reservar_ahora(cfg, estado, documento, subnivel, clase, hora, fecha)


# ---------------------------------------------------------- mensajes

def menu(cfg, estado, documento, encabezado):
    activa = reserva_activa(documento)
    if bloqueante(activa):
        cuando = fecha_de_reserva(activa)
        enviar(f"{encabezado}\n\nTienes una clase pendiente sin ver:\n{describir(activa)}\n\n"
               "El sistema no deja programar otra hasta que la veas."
               + (f"\nEscribeme despues de las {(cuando + timedelta(hours=2)):%H:%M}."
                  if cuando else ""),
               botones=[[TEXTO_CANCELAR, TEXTO_ESTADO]])
        return

    subnivel, clase, fin = proxima_clase(cfg, estado, activa)
    if fin:
        enviar(f"{encabezado}\n\nYa reservaste la clase {clase}, la ultima de {subnivel}.\n"
               "Dime cual sigue, por ejemplo 'A2.1 clase 1 19:00'.")
        return

    manana = datetime.now(TZ).date() + timedelta(days=1)
    objetivo = proximo_dia_habil(manana, cfg)
    nota = ""
    if objetivo != manana:
        nota = f"\n(manana no hay clases porque {sin_clases(manana, cfg)})"

    cabeza = (f"{encabezado}\n\n{subnivel} clase {clase}\n"
              f"Reservaria para el {fecha_bonita(objetivo)}.{nota}")

    # Horas reales del sistema, no una lista fija.
    try:
        reales = sorted({hora_del_slot(s)
                         for s in obtener_slots(subnivel, objetivo.strftime("%Y-%m-%d"))})
    except (requests.RequestException, ValueError) as e:
        log(f"No pude consultar horarios: {e}")
        reales = None

    if reales is None:
        sugeridas = cfg.get("horas_sugeridas") or [cfg["hora"]]
        botones = [sugeridas[i:i + 3] for i in range(0, len(sugeridas), 3)]
        botones.append([TEXTO_NO, TEXTO_ESTADO])
        enviar(f"{cabeza}\n\nNo pude consultar los horarios ahora. "
               "Dime una hora e igual lo intento.", botones=botones)
        return

    if not reales:
        vispera = objetivo - timedelta(days=1)
        enviar(f"{cabeza}\n\nTodavia no hay ningun horario publicado para ese dia. "
               "Los cupos abren la madrugada anterior.\n\n"
               f"Escribeme 'entra el {DIAS[vispera.weekday()]} 00:15' "
               "y lo busco apenas los abran.",
               botones=[[f"entra el {DIAS[vispera.weekday()]} 00:15"], [TEXTO_ESTADO]])
        return

    visibles = reales[:9]
    botones = [visibles[i:i + 3] for i in range(0, len(visibles), 3)]
    botones.append([TEXTO_NO, TEXTO_ESTADO])
    enviar(f"{cabeza}\n\nHoras libres: {', '.join(reales)}\n"
           "Toca una y la reservo de una.", botones=botones)


def cancelar(documento, estado):
    activa = reserva_activa(documento)
    if not activa:
        enviar("No tienes ninguna reserva activa para cancelar.")
        return
    if ya_paso(activa):
        enviar(f"No cancele nada: esa clase ya se dicto.\n{describir(activa)}\n"
               "El sistema la sigue mostrando, pero no te bloquea.")
        return
    res = api_post("cancel", bookingId=activa.get("bookingId"),
                   token=activa.get("cancelToken", ""), source="telegram_bot")
    if res.get("ok"):
        clase = str(activa.get("clase", "")).strip()
        if clase.isdigit() and estado.get("ultima_clase_reservada") == int(clase):
            estado["ultima_clase_reservada"] = int(clase) - 1 or None
            guardar_estado(estado)
        enviar(f"CANCELADO\n{describir(activa)}\nTu cupo quedo liberado.")
    else:
        enviar(f"No pude cancelar: {res.get('error','sin detalle')}\n"
               "Puedes hacerlo desde la pagina del curso.")


def contar_estado(cfg, estado, documento):
    activa = reserva_activa(documento)
    partes = []
    if activa and not ya_paso(activa):
        partes.append(f"Reserva activa:\n{describir(activa)}")
    elif activa:
        partes.append(f"Tu ultima clase fue:\n{describir(activa)}")
    else:
        partes.append("No tienes ninguna reserva en el sistema.")

    c = estado.get("cita")
    if c and c.get("cuando"):
        detalle = f"{c.get('subnivel') or ''} clase {c.get('clase') or '?'} a las {c.get('hora') or '?'}"
        partes.append(f"Cita puesta para el {c['cuando']}:\n{detalle.strip()}")
    else:
        partes.append("No tienes ninguna cita puesta.")
    enviar("\n\n".join(partes))


# ---------------------------------------------------------- modos

def atender(cfg, estado, documento, texto):
    if es_comando(texto, r"(cancelar|cancela|cancelar clase|cancelar reserva)"):
        cancelar(documento, estado)
        return
    if es_comando(texto, r"(estado|ver estado|que tengo|qué tengo|mi reserva|reserva)"):
        contar_estado(cfg, estado, documento)
        return

    t = texto.lower()
    activa = reserva_activa(documento)
    s_sug, c_sug, _ = proxima_clase(cfg, estado, activa)

    # "entra el domingo 00:15 ..." -> deja una cita
    if re.search(r"\bentra\b|\bentrar\b", t):
        dia = interpretar_dia(t) or (datetime.now(TZ).date() + timedelta(days=1))
        horas = re.findall(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", t)
        h_entrada = interpretar_hora(horas[0]) if horas else "00:15"
        h_clase = interpretar_hora(horas[1]) if len(horas) > 1 else None
        cuando = datetime.strptime(f"{dia} {h_entrada}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)

        m = re.search(r"clase\s*(\d+)", t)
        clase = m.group(1) if m else c_sug
        destino = dia + timedelta(days=1)

        poner_cita(estado, cuando, s_sug, clase, h_clase, destino)
        falta = "" if h_clase else "\nDime a que hora quieres la clase y la anoto."
        enviar(f"Cita puesta: entro el {fecha_bonita(dia)} a las {h_entrada}\n"
               f"y busco {s_sug} clase {clase} para el {fecha_bonita(destino)}"
               + (f" a las {h_clase}." if h_clase else ".") + falta)
        return

    params, veredicto = interpretar(texto, dict(cfg, subnivel=s_sug, clase=c_sug))

    if veredicto == "no":
        if estado.get("cita"):
            estado["cita"] = None
            guardar_estado(estado)
        enviar("Listo, no reservo nada.")
        return

    if veredicto == "menu":
        menu(cfg, estado, documento, "Que clase quieres reservar?")
        return

    if veredicto == "?":
        menu(cfg, estado, documento, "Hola. Que quieres hacer?")
        return

    # Si hay una cita a medias esperando la hora, la completa.
    c = estado.get("cita")
    if c and c.get("cuando") and not c.get("hora") and params.get("hora"):
        c["hora"] = params["hora"]
        guardar_estado(estado)
        enviar(f"Anotado. En la cita del {c['cuando']} busco a las {params['hora']}.")
        return

    if params.get("fecha"):
        objetivo = datetime.strptime(params["fecha"], "%Y-%m-%d").date()
    else:
        objetivo = proximo_dia_habil(datetime.now(TZ).date() + timedelta(days=1), cfg)

    reservar_ahora(cfg, estado, documento, params["subnivel"], str(params["clase"]),
                   params["hora"], objetivo)


def modo_escuchar(cfg, estado, documento):
    c = cita_pendiente(estado)
    if c:
        ejecutar_cita(cfg, estado, documento, c)

    ultimo_visto = int(estado.get("ultimo_update_procesado", 0))
    limite = time.time() - float(cfg.get("ventana_comandos_horas", 2)) * 3600
    nuevos = [m for m in mensajes_mios(obtener_updates())
              if m["id"] > ultimo_visto and m["fecha"] >= limite]
    if not nuevos:
        log("Sin mensajes nuevos.")
        return

    estado["ultimo_update_procesado"] = max(m["id"] for m in nuevos)
    guardar_estado(estado)
    atender(cfg, estado, documento, nuevos[-1]["texto"])


def modo_recordar(cfg, estado, documento):
    """9:20 p.m. Justo despues de la clase, cuando el cupo queda libre."""
    manana = datetime.now(TZ).date() + timedelta(days=1)
    motivo = sin_clases(manana, cfg)
    if motivo:
        habil = proximo_dia_habil(manana, cfg)
        enviar(f"Manana no hay clases porque {motivo}.\n"
               f"El siguiente dia habil es el {fecha_bonita(habil)}. "
               "Si quieres, escribeme 'entra el "
               f"{DIAS[(habil - timedelta(days=1)).weekday()]} 00:15' y la busco apenas abran.")
        return

    activa = reserva_activa(documento)
    if activa and not ya_paso(activa):
        cuando = fecha_de_reserva(activa)
        if cuando and cuando.date() == manana:
            log("Ya tiene reservada la de manana, no molesto.")
            return

    menu(cfg, estado, documento, "Ya termino tu clase. Quieres programar la de manana?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo", choices=["escuchar", "recordar"], required=True)
    args = ap.parse_args()

    documento = os.environ.get("SMARTFLEX_DOC")
    if not documento:
        enviar("Falta la variable SMARTFLEX_DOC.")
        sys.exit(1)

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    base = {"ultima_clase_reservada": None, "subnivel": cfg["subnivel"],
            "ultimo_update_procesado": 0, "cita": None}
    estado = base
    if ESTADO_FILE.exists():
        try:
            estado = {**base, **json.loads(ESTADO_FILE.read_text(encoding="utf-8"))}
        except ValueError:
            log("estado.json ilegible, uso valores por defecto.")

    (modo_escuchar if args.modo == "escuchar" else modo_recordar)(cfg, estado, documento)


if __name__ == "__main__":
    main()
