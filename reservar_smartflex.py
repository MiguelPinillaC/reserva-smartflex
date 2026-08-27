#!/usr/bin/env python3
"""
Reserva Smart Flex — el chat manda.

Le escribes al bot a cualquier hora. Si le dices una hora, anota tu intencion
y la ejecuta en la madrugada. Si le escribes cualquier otra cosa, te muestra
el menu con lo que puede hacer.

  --modo escuchar    (cada 10 min)  Atiende lo que le escribas.
  --modo preguntar   (8:00 p.m.)    Solo pregunta si no le pediste nada aun.
  --modo reservar    (12:15 a.m.)   Ejecuta lo que quedo anotado.

Nada se reserva ni se cancela sin que tu lo hayas pedido.

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

# En un repositorio publico los logs de Actions los puede leer cualquiera.
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


def cargar(path, defecto):
    if path.exists():
        try:
            return {**defecto, **json.loads(path.read_text(encoding="utf-8"))}
        except ValueError:
            log(f"{path} ilegible, uso valores por defecto.")
    return dict(defecto)


def guardar_estado(estado):
    ESTADO_FILE.write_text(json.dumps(estado, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    log("estado.json actualizado")


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


def describir(b):
    return (f"{b.get('subnivel','')} clase {b.get('clase','')} - "
            f"{b.get('fecha_clase','')} {b.get('hora_clase','')}")


def fecha_de_reserva(b):
    """La API da fecha_clase en DD/MM/YYYY y hora_clase en 12 horas."""
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
    log("No pude interpretar la fecha de la reserva")
    return None


def ya_paso(b):
    """El sistema sigue reportando como activas las reservas vencidas."""
    cuando = fecha_de_reserva(b)
    if cuando is None:
        return False
    return cuando < datetime.now(TZ) - timedelta(minutes=30)


def bloqueante(b):
    """Solo estorba una reserva que todavia no ha ocurrido."""
    return b if (b and not ya_paso(b)) else None


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
                           "fecha": msg.get("date", 0),
                           "texto": texto})
    if salida:
        log(f"<- {len(salida)} mensaje(s) tuyos"
            + (f": {[m['texto'] for m in salida]}" if DETALLE else ""))
    return salida


def interpretar(texto, base):
    """
    Devuelve (parametros, veredicto) donde veredicto es:
      "si" -> quiere reservar, con lo que haya especificado
      "no" -> dijo explicitamente que no
      "?"  -> no se entiende como una orden de reserva
    """
    cfg = dict(base)
    t = texto.lower().strip()

    if re.search(r"(no reservar|no gracias|saltar|omitir|^no\b|^nel\b)", t):
        return cfg, "no"

    reconocio = False

    m = re.search(r"\b([a-c][12]\.\d)\b", t)
    if m:
        cfg["subnivel"] = m.group(1).upper()
        reconocio = True

    m = re.search(r"clase\s*(\d+)", t)
    if m:
        cfg["clase"] = m.group(1)
        reconocio = True

    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        cfg["hora"] = f"{int(m.group(1)):02d}:{m.group(2)}"
        reconocio = True
    else:
        m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
        if m:
            h = int(m.group(1)) % 12
            if m.group(2) == "pm":
                h += 12
            cfg["hora"] = f"{h:02d}:00"
            reconocio = True

    if re.search(r"\b(si|sí|dale|listo|ok|claro|reserva|reservar|programa|programar)\b", t):
        reconocio = True

    return cfg, ("si" if reconocio else "?")


def es_comando(texto, patron):
    return bool(re.fullmatch(patron, texto.lower().strip(" .!¡¿?")))


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


# ---------------------------------------------------------- clase e intencion

def proxima_clase(cfg, estado, activa):
    """
    Prioridad: reserva en el sistema (aunque ya haya pasado) > estado.json > config.
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
        return subnivel, str(ultima), True

    return subnivel, str(siguiente), False


def objetivo_de_la_proxima_corrida(cfg):
    """
    Que dia quedaria reservado si pides algo ahora mismo. La corrida de reserva
    es a las 00:15 y programa para 'dias_adelante' dias despues de esa corrida.
    """
    dias = int(cfg.get("dias_adelante", 1))
    hh, mm = (int(x) for x in str(cfg.get("hora_corrida_reserva", "00:15")).split(":"))
    ahora = datetime.now(TZ)
    corrida = ahora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if ahora >= corrida:
        corrida += timedelta(days=1)
    return corrida + timedelta(days=dias)


def pendiente_valido(estado, cfg):
    """
    La intencion anotada, si su fecha objetivo sigue en el futuro.
    Se compara contra la fecha que ella misma guardo, nunca recalculando
    la proxima corrida: al ejecutarse a las 00:15 el calculo daria un dia mas.
    """
    p = estado.get("pendiente")
    if not p:
        return None
    hoy = datetime.now(TZ).strftime("%Y-%m-%d")
    if str(p.get("para", "")) <= hoy:
        log(f"Intencion vencida (era para {p.get('para')}), la descarto.")
        return None
    return p


def anotar(estado, cfg, subnivel, clase, hora):
    objetivo = objetivo_de_la_proxima_corrida(cfg)
    estado["pendiente"] = {"subnivel": subnivel, "clase": str(clase), "hora": hora,
                           "para": objetivo.strftime("%Y-%m-%d"),
                           "creado": int(time.time())}
    guardar_estado(estado)
    return objetivo


def olvidar(estado):
    if estado.get("pendiente"):
        estado["pendiente"] = None
        guardar_estado(estado)


# ---------------------------------------------------------- mensajes

def preguntar_hora(cfg, estado, documento, encabezado):
    """Manda el menu principal: propone clase y ofrece las horas como botones."""
    activa = reserva_activa(documento)
    if bloqueante(activa):
        enviar(f"{encabezado}\n\nYa tienes una reserva activa:\n{describir(activa)}\n\n"
               "El sistema solo permite una a la vez. Escribeme 'cancelar' para liberarla.",
               botones=[[TEXTO_CANCELAR, TEXTO_ESTADO]])
        return

    subnivel, clase, fin = proxima_clase(cfg, estado, activa)
    if fin:
        enviar(f"{encabezado}\n\nYa reservaste la clase {clase}, la ultima de {subnivel}.\n"
               "Dime cual sigue, por ejemplo 'A2.1 clase 1 19:00'.")
        return

    objetivo = objetivo_de_la_proxima_corrida(cfg)
    sugeridas = cfg.get("horas_sugeridas") or [cfg["hora"]]
    botones = [sugeridas[i:i + 3] for i in range(0, len(sugeridas), 3)]
    botones.append([TEXTO_NO, TEXTO_ESTADO])

    enviar(f"{encabezado}\n\n"
           f"Te propongo {subnivel} clase {clase} para el {fecha_bonita(objetivo)}.\n"
           "Toca una hora, o escribeme algo como 'clase 12 8pm'.",
           botones=botones)


def confirmar_anotado(estado, cfg, subnivel, clase, hora):
    objetivo = anotar(estado, cfg, subnivel, clase, hora)
    enviar(f"Anotado: {subnivel} clase {clase} a las {hora}\n"
           f"para el {fecha_bonita(objetivo)}.\n\n"
           "Lo reservo en la madrugada y te confirmo. "
           "Si cambias de idea, escribeme 'no reservar'.")


def cancelar(documento, estado):
    activa = reserva_activa(documento)
    if not activa:
        enviar("No tienes ninguna reserva activa para cancelar.")
        return

    if ya_paso(activa):
        enviar(f"No cancele nada: esa clase ya se dicto.\n{describir(activa)}\n"
               "El sistema la sigue mostrando, pero no te bloquea para reservar otra.")
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

    p = pendiente_valido(estado, cfg)
    if p:
        partes.append(f"Anotado para esta madrugada:\n"
                      f"{p['subnivel']} clase {p['clase']} a las {p['hora']}")
    else:
        partes.append("No tienes nada anotado para esta madrugada.")

    enviar("\n\n".join(partes))


# ---------------------------------------------------------- modos

def modo_escuchar(cfg, estado, documento):
    ultimo_visto = int(estado.get("ultimo_update_procesado", 0))
    limite = time.time() - float(cfg.get("ventana_comandos_horas", 2)) * 3600

    pendientes = [m for m in mensajes_mios(obtener_updates())
                  if m["id"] > ultimo_visto and m["fecha"] >= limite]
    if not pendientes:
        log("Sin mensajes nuevos.")
        return

    estado["ultimo_update_procesado"] = max(m["id"] for m in pendientes)
    guardar_estado(estado)

    texto = pendientes[-1]["texto"]   # solo el ultimo cuenta

    if es_comando(texto, r"(cancelar|cancela|cancelar clase|cancelar reserva)"):
        cancelar(documento, estado)
        olvidar(estado)
        return

    if es_comando(texto, r"(estado|ver estado|que tengo|qué tengo|mi reserva|reserva)"):
        contar_estado(cfg, estado, documento)
        return

    activa = reserva_activa(documento)
    subnivel_sug, clase_sug, _ = proxima_clase(cfg, estado, activa)
    base = dict(cfg, subnivel=subnivel_sug, clase=clase_sug)
    params, veredicto = interpretar(texto, base)

    if veredicto == "no":
        olvidar(estado)
        enviar("Listo, no reservo nada. Si cambias de idea, escribeme una hora.")
    elif veredicto == "si":
        if bloqueante(activa):
            enviar(f"No puedo anotarlo: ya tienes una reserva activa.\n{describir(activa)}\n"
                   "Escribeme 'cancelar' para liberarla.")
        else:
            confirmar_anotado(estado, cfg, params["subnivel"], params["clase"], params["hora"])
    else:
        preguntar_hora(cfg, estado, documento, "Hola. Que quieres hacer?")


def modo_preguntar(cfg, estado, documento):
    """A las 8 p.m. Solo molesta si no le has pedido nada."""
    p = pendiente_valido(estado, cfg)
    if p:
        cuando = datetime.strptime(p["para"], "%Y-%m-%d").replace(tzinfo=TZ)
        enviar(f"Recordatorio: ya tienes anotado {p['subnivel']} clase {p['clase']} "
               f"a las {p['hora']} para el {fecha_bonita(cuando)}.\n"
               "Lo reservo en la madrugada. Escribeme 'no reservar' si cambiaste de idea.")
        return

    preguntar_hora(cfg, estado, documento,
                   "No me has pedido clase para manana.")


def modo_reservar(cfg, estado, documento):
    p = pendiente_valido(estado, cfg)

    # Respaldo: si el workflow de escuchar no corrio, leo el chat directamente.
    # Solo mensajes que ninguna corrida haya procesado antes, para no repetir
    # la reserva de anoche con el mismo mensaje viejo.
    if not p:
        ultimo_visto = int(estado.get("ultimo_update_procesado", 0))
        limite = time.time() - float(cfg.get("ventana_respuesta_horas", 24)) * 3600
        nuevos = [m for m in mensajes_mios(obtener_updates())
                  if m["id"] > ultimo_visto and m["fecha"] >= limite]
        if nuevos:
            estado["ultimo_update_procesado"] = max(m["id"] for m in nuevos)
            activa = reserva_activa(documento)
            s, c, _ = proxima_clase(cfg, estado, activa)
            params, veredicto = interpretar(nuevos[-1]["texto"], dict(cfg, subnivel=s, clase=c))
            if veredicto == "si":
                log("Sin intencion anotada, pero encontre tu mensaje en el chat.")
                objetivo = datetime.now(TZ) + timedelta(days=int(cfg.get("dias_adelante", 1)))
                p = {"subnivel": params["subnivel"], "clase": str(params["clase"]),
                     "hora": params["hora"], "para": objetivo.strftime("%Y-%m-%d")}
            guardar_estado(estado)

    if not p:
        if cfg.get("avisar_si_no_hubo_respuesta", True):
            enviar("No me pediste clase, asi que no reserve nada.")
        return

    subnivel, clase, hora = p["subnivel"], str(p["clase"]), p["hora"]
    alternativa = bool(cfg.get("reservar_alternativa", False))
    fecha = p["para"]
    objetivo = datetime.strptime(fecha, "%Y-%m-%d").replace(tzinfo=TZ)

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

    activa = reserva_activa(documento)
    if bloqueante(activa):
        enviar(f"No reserve: ya tienes una reserva activa\n{describir(activa)}")
        olvidar(estado)
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
                    estado["pendiente"] = None
                    guardar_estado(estado)
                    extra = "" if hora_final == hora else f"\n(no habia a las {hora}, tome esta)"
                    enviar(f"RESERVADO\n{subnivel} clase {clase}\n"
                           f"{fecha_bonita(objetivo)} a las {hora_final}\n"
                           f"id: {res.get('bookingId','')}{extra}")
                    return
                if res.get("booking"):
                    enviar("No reserve: el sistema reporta una reserva activa tuya.")
                    olvidar(estado)
                    return
                ultimo_error = res.get("error", "rechazado sin detalle")
            else:
                ultimo_error = (f"no hay cupo a las {hora}. Disponibles: "
                                f"{', '.join(sorted(disponibles)) if disponibles else 'ninguna'}")
        except requests.RequestException as e:
            ultimo_error = f"error de conexion ({e})"

        if intento < INTENTOS:
            time.sleep(ESPERA)

    olvidar(estado)
    enviar(f"NO RESERVADO\n{subnivel} clase {clase} - {fecha_bonita(objetivo)} a las {hora}\n"
           f"Motivo: {ultimo_error}")


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
                                  "ultimo_update_procesado": 0,
                                  "pendiente": None})

    {"preguntar": modo_preguntar,
     "reservar": modo_reservar,
     "escuchar": modo_escuchar}[args.modo](cfg, estado, documento)


if __name__ == "__main__":
    main()
