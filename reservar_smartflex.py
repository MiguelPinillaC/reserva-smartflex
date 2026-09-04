#!/usr/bin/env python3
"""
Reserva Smart Flex — reserva por chat, entrando por donde entra un estudiante.

Desde agosto de 2026 la institucion apago el agendamiento por web directa y lo
dejo solo dentro de Brightspace. La API lo dice ella misma:

    getAccessConfig -> {"webBookingEnabled": false, "iframeBookingEnabled": true}

Asi que el bot ya no le habla a la API por su cuenta para ver horarios ni para
reservar: abre Brightspace con tu usuario, entra al modulo de la clase que
sigue, y el widget de reservas corre en su iframe real. Se automatiza el clic,
no el permiso.

Eso parte el trabajo en dos, y esa division es la que mantiene esto barato:

  --modo escuchar     (cada minuto)   requests puro, ~10 s, SIN contrasena.
                                      Lee Telegram, responde estado, cancela,
                                      anota citas y deja ordenes de reserva.
  --modo recordar     (9:20 p.m.)     requests puro. Pregunta por la de manana.
  --modo reservar     (por demanda)   Playwright. Ejecuta una orden pendiente.
  --modo diagnostico  (manual)        requests puro. No reserva nada.

El modo escuchar nunca ve la contrasena. Solo 'reservar' la usa, y solo corre
cuando hay algo concreto que reservar: un par de veces al dia, no 1.440.

Secrets siempre: SMARTFLEX_DOC, SMARTFLEX_EMAIL, TELEGRAM_TOKEN,
                 TELEGRAM_CHAT_ID
Secrets solo en 'reservar': SMARTFLEX_PASS, y SMARTFLEX_USER si tu usuario de
                 Brightspace no es el documento.
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
ESPERA_UI = 45_000          # ms que le damos a cada paso del widget

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

# La API contesta esto cuando le pides horarios por fuera del iframe.
MARCA_BLOQUEO = "deshabilitado"


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


def avisar_al_workflow(clave, valor="1"):
    """
    Deja una senal para el YAML. El paso siguiente la lee y decide si dispara
    el workflow de reservar. Asi el bucle rapido no necesita el navegador.
    """
    salida = os.environ.get("GITHUB_OUTPUT")
    if not salida:
        log(f"(senal {clave}={valor}; no estoy en Actions)")
        return
    with open(salida, "a", encoding="utf-8") as fh:
        fh.write(f"{clave}={valor}\n")
    log(f"senal para el workflow: {clave}={valor}")


def normalizar_hora(texto):
    """'7:00 PM' / '19:00' / '7 pm' -> '19:00'. Devuelve None si no se entiende."""
    t = str(texto).strip().lower().replace(".", "")
    m = re.search(r"(\d{1,2}):(\d{2})\s*(a m|p m|am|pm)?", t)
    if m:
        h, mi, suf = int(m.group(1)), m.group(2), (m.group(3) or "").replace(" ", "")
        if suf == "pm" and h != 12:
            h += 12
        if suf == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mi}"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)) % 12
        return f"{h + (12 if m.group(2) == 'pm' else 0):02d}:00"
    return None


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


# ---------------------------------------------------------- API ligera

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


def bloqueado_por_entorno(resp):
    """True si la API nos rechazo por no venir del iframe de Brightspace."""
    if not isinstance(resp, dict) or resp.get("ok") is not False:
        return False
    return MARCA_BLOQUEO in str(resp.get("error", "")).lower()


def reserva_activa(documento):
    """Sigue funcionando sin navegador: no depende del modo de entrada."""
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


# ---------------------------------------------------------- interpretacion

def interpretar_hora(t):
    return normalizar_hora(t)


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
      'si'   trae datos concretos -> dejar orden de reserva
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


# ---------------------------------------------------------- ordenes

def poner_orden(estado, subnivel, clase, hora, fecha, accion="reservar"):
    """
    Una orden es lo unico que el bucle rapido le deja al de navegador.
    Se guarda en estado.json y el workflow dispara 'reservar' al verla.
    """
    estado["orden"] = {
        "accion": accion,
        "subnivel": subnivel,
        "clase": str(clase),
        "hora": hora,
        "fecha": fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else fecha,
        "creada": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
    }
    guardar_estado(estado)
    avisar_al_workflow("reservar", "1")


def orden_vigente(estado):
    o = estado.get("orden")
    if not o:
        return None
    try:
        creada = datetime.strptime(o["creada"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except (ValueError, KeyError, TypeError):
        return o
    if datetime.now(TZ) - creada > timedelta(hours=6):
        log("Orden vieja, la descarto.")
        estado["orden"] = None
        guardar_estado(estado)
        return None
    return o


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


def describir_cita(c):
    try:
        cuando = datetime.strptime(c["cuando"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        entrada = f"{fecha_bonita(cuando)} a las {cuando:%H:%M}"
    except (ValueError, KeyError, TypeError):
        entrada = str(c.get("cuando", "?"))

    que = f"{c.get('subnivel') or ''} clase {c.get('clase') or '?'}".strip()

    if c.get("para"):
        try:
            destino = datetime.strptime(c["para"], "%Y-%m-%d").date()
            que += f" para el {fecha_bonita(destino)}"
        except ValueError:
            pass

    que += f" a las {c['hora']}" if c.get("hora") else " (falta la hora)"
    return f"Entro el {entrada}\ny busco {que}."


def etiqueta_dia(d):
    hoy = datetime.now(TZ).date()
    if d == hoy:
        return "hoy"
    if d == hoy + timedelta(days=1):
        return "manana"
    return DIAS[d.weekday()]


def apertura_de(fecha, cfg):
    """Cuando abren los cupos de esa fecha: la madrugada anterior."""
    vispera = fecha - timedelta(days=1)
    hhmm = str(cfg.get("hora_apertura_cupos", "00:15"))
    try:
        return datetime.strptime(f"{vispera} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except ValueError:
        return datetime.strptime(f"{vispera} 00:15", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


# ---------------------------------------------------------- el navegador

def _buscar_frame(pagina, fragmento, segundos=40):
    """
    El widget vive en un iframe anidado dentro del visor de contenido de D2L.
    Buscarlo por URL en pagina.frames aguanta cualquier profundidad.
    """
    limite = time.time() + segundos
    while time.time() < limite:
        for f in pagina.frames:
            if fragmento in (f.url or ""):
                return f
        pagina.wait_for_timeout(500)
    return None


def _entrar_a_brightspace(pagina, bs):
    usuario = os.environ.get("SMARTFLEX_USER") or os.environ.get("SMARTFLEX_DOC", "")
    clave = os.environ.get("SMARTFLEX_PASS", "")
    if not clave:
        raise RuntimeError("falta el secret SMARTFLEX_PASS")

    pagina.goto(f'{bs["base"]}/d2l/login', wait_until="domcontentloaded", timeout=ESPERA_UI)
    pagina.fill("#userName", usuario)
    pagina.fill("#password", clave)          # nunca por URL, nunca a los logs
    pagina.click("button:has-text('Iniciar sesión')")
    pagina.wait_for_load_state("networkidle", timeout=ESPERA_UI)

    if "/d2l/login" in pagina.url:
        raise RuntimeError("Brightspace no acepto el usuario o la contrasena")
    log("Dentro de Brightspace")


def _abrir_widget(pagina, bs, clase):
    topics = bs.get("topics") or {}
    topic = topics.get(str(clase))
    if not topic:
        raise RuntimeError(f"no tengo el id del modulo de la clase {clase} "
                           f"(agregalo en config.json -> brightspace.topics)")

    url = f'{bs["base"]}/d2l/le/content/{bs["curso_id"]}/viewContent/{topic}/View'
    pagina.goto(url, wait_until="domcontentloaded", timeout=ESPERA_UI)

    w = _buscar_frame(pagina, "reservas-smartflex")
    if w is None:
        raise RuntimeError("el modulo cargo pero no aparecio el widget de reservas")

    documento = os.environ.get("SMARTFLEX_DOC", "")
    w.wait_for_selector("#loginDoc", timeout=ESPERA_UI)
    w.fill("#loginDoc", documento)
    w.click("#loginBtn")

    try:
        w.wait_for_selector("#appContainer", state="visible", timeout=ESPERA_UI)
    except Exception:
        error = w.locator("#launchErrorText")
        if error.count() and error.is_visible():
            raise RuntimeError("el widget rechazo la apertura: "
                               + error.inner_text()[:150])
        raise

    # Dentro de Brightspace la clase la fija el modulo, no un menu: el widget
    # bloquea la seleccion manual. Y si tienes trabajo autonomo pendiente, la
    # plataforma te redirige a otro modulo. Las dos cosas juntas significan que
    # podemos acabar parados en una clase distinta a la que pedimos, y reservar
    # la equivocada sin que nadie se entere. Se verifica antes de tocar nada.
    abierta = ""
    try:
        abierta = (w.locator("#valClase").inner_text(timeout=10_000) or "").strip()
    except Exception:
        log("No pude leer en que clase abrio el widget; sigo con cuidado.")

    if abierta and abierta != str(clase):
        raise RuntimeError(
            f"pedi la clase {clase} y el modulo abrio en la {abierta}. "
            "Brightspace redirige asi cuando tienes trabajo autonomo pendiente: "
            "completalo y vuelve a intentar.")

    log(f"Widget abierto en la clase {abierta or clase}")
    return w


def _horas_ofrecidas(w):
    """Lee las tarjetas de horario y las devuelve como {'19:00': indice}."""
    tarjetas = w.locator("#slotsList div[data-slotid]")
    salida = {}
    for i in range(tarjetas.count()):
        try:
            crudo = tarjetas.nth(i).locator(".slot-time-main").inner_text(timeout=5000)
        except Exception:
            continue
        h = normalizar_hora(crudo)
        if h:
            salida[h] = i
    return salida


def ejecutar_orden(cfg, estado, documento, orden):
    """Abre el navegador y hace UNA cosa: la orden pendiente."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        enviar("No reserve: falta instalar Playwright en el workflow.")
        return False

    bs = cfg.get("brightspace") or {}
    if not bs.get("base") or not bs.get("curso_id"):
        enviar("No reserve: falta la seccion 'brightspace' en config.json.")
        return False

    subnivel = orden.get("subnivel") or cfg["subnivel"]
    clase = str(orden.get("clase") or cfg["clase"])
    hora = orden.get("hora") or cfg["hora"]
    fecha = orden.get("fecha")
    email = os.environ.get("SMARTFLEX_EMAIL", "").strip()

    if not email:
        enviar("No reserve: falta el secret SMARTFLEX_EMAIL.")
        return False

    with sync_playwright() as p:
        nav = p.chromium.launch(args=["--no-sandbox"])
        ctx = nav.new_context(locale="es-CO", timezone_id="America/Bogota",
                              viewport={"width": 1280, "height": 900})
        pagina = ctx.new_page()
        try:
            _entrar_a_brightspace(pagina, bs)
            w = _abrir_widget(pagina, bs, clase)

            if orden.get("accion") == "cancelar":
                return _cancelar_en_widget(w, estado, documento)

            w.click("#btnProgramar")
            w.wait_for_selector("#datesList .card", timeout=ESPERA_UI)

            boton_fecha = w.locator(f'#datesList button[onclick*="{fecha}"]')
            if boton_fecha.count() == 0:
                disponibles = re.findall(r"loadSlots\('([\d-]+)'\)",
                                         w.locator("#datesList").inner_html())
                _avisar_sin_fecha(fecha, sorted(set(disponibles)))
                return False

            boton_fecha.first.click()
            w.wait_for_selector("#slotsList div[data-slotid]", timeout=ESPERA_UI)

            opciones = _horas_ofrecidas(w)
            log(f"Horas para el {fecha}: {sorted(opciones) or 'ninguna'}")

            elegida = hora
            if hora not in opciones:
                if not cfg.get("reservar_alternativa", False) or not opciones:
                    _avisar_sin_hora(fecha, hora, sorted(opciones))
                    return False
                elegida = sorted(opciones)[0]

            tarjeta = w.locator("#slotsList div[data-slotid]").nth(opciones[elegida])
            tarjeta.locator(".btn-reserve").click()

            w.wait_for_selector("#bookingForm", timeout=ESPERA_UI)
            campo = w.locator("#email")
            if not (campo.input_value() or "").strip():
                campo.fill(email)
            w.click("#confirmBtn")
            w.wait_for_timeout(6000)

            return _confirmar_resultado(estado, documento, subnivel, clase,
                                        fecha, elegida, hora)
        except Exception as e:
            # Sin volcados de pagina: los logs de un repo publico los lee cualquiera.
            enviar(f"NO RESERVADO\n{subnivel} clase {clase} - {fecha} a las {hora}\n"
                   f"Motivo: {type(e).__name__}: {str(e)[:200]}\n"
                   f"(ejecutado {datetime.now(TZ):%H:%M})")
            return False
        finally:
            ctx.close()
            nav.close()


def _avisar_sin_fecha(fecha, disponibles):
    if disponibles:
        lineas = []
        for d in disponibles[:6]:
            try:
                lineas.append("  " + fecha_bonita(datetime.strptime(d, "%Y-%m-%d").date()))
            except ValueError:
                lineas.append("  " + d)
        enviar(f"NO RESERVADO\nNo hay cupo para el {fecha}, pero si para:\n"
               + "\n".join(lineas)
               + "\n\nEscribeme por ejemplo 'viernes 7pm' y lo tomo.")
    else:
        enviar(f"NO RESERVADO\nNo hay ninguna fecha con cupo todavia.\n"
               f"(revisado {datetime.now(TZ):%H:%M})")


def _avisar_sin_hora(fecha, hora, disponibles):
    if disponibles:
        enviar(f"NO RESERVADO\nNo hay cupo a las {hora} el {fecha}.\n"
               f"Libres: {', '.join(disponibles)}\n\n"
               "Toca una y la reservo.",
               botones=[disponibles[i:i + 3] for i in range(0, len(disponibles[:9]), 3)]
                       + [[TEXTO_NO, TEXTO_ESTADO]])
    else:
        enviar(f"NO RESERVADO\nNo quedan horas libres el {fecha}.")


def _confirmar_resultado(estado, documento, subnivel, clase, fecha, elegida, pedida):
    """
    La verdad no la da el HTML del modal, la da la API: verifyStudentBooking
    sigue respondiendo sin navegador y es la misma fuente que usa el sistema.
    """
    time.sleep(3)
    activa = reserva_activa(documento)
    ok = bool(activa and str(activa.get("clase", "")).strip() == str(clase))

    estado["orden"] = None
    if ok:
        if str(clase).isdigit():
            estado["ultima_clase_reservada"] = int(clase)
            estado["subnivel"] = subnivel
        guardar_estado(estado)
        extra = "" if elegida == pedida else f"\n(no habia a las {pedida}, tome esta)"
        enviar(f"RESERVADO\n{subnivel} clase {clase}\n{fecha} a las {elegida}{extra}\n"
               f"({describir(activa)})")
        return True

    guardar_estado(estado)
    enviar(f"NO RESERVADO\n{subnivel} clase {clase} - {fecha} a las {elegida}\n"
           "El widget no confirmo la reserva. Revisa desde Brightspace.")
    return False


def _cancelar_en_widget(w, estado, documento):
    w.click("#btnCancelar")
    w.wait_for_selector("#confirmCancelBtn", timeout=ESPERA_UI)
    w.click("#confirmCancelBtn")
    w.wait_for_timeout(5000)

    estado["orden"] = None
    if reserva_activa(documento) is None:
        ultima = estado.get("ultima_clase_reservada")
        if isinstance(ultima, int):
            estado["ultima_clase_reservada"] = (ultima - 1) or None
        guardar_estado(estado)
        enviar("CANCELADO\nTu cupo quedo liberado.")
        return True
    guardar_estado(estado)
    enviar("No pude confirmar la cancelacion. Revisa desde Brightspace.")
    return False


# ---------------------------------------------------------- mensajes

def menu(cfg, estado, documento, encabezado):
    """
    Sin navegador no podemos listar horas reales, asi que ofrecemos las
    sugeridas y la confirmacion real llega cuando corre 'reservar'.
    """
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

    sugeridas = cfg.get("horas_sugeridas") or [cfg["hora"]]
    botones = [sugeridas[i:i + 3] for i in range(0, len(sugeridas), 3)]
    botones.append([TEXTO_NO, TEXTO_ESTADO])
    enviar(f"{encabezado}\n\n{subnivel} clase {clase}\n"
           f"Reservaria para el {fecha_bonita(objetivo)}.{nota}\n\n"
           "Toca una hora y entro a Brightspace a reservarla.", botones=botones)


PATRON_ESTADO = r"(estado|ver estado|que tengo|qué tengo|mi reserva|mis reservas)"
PATRON_CANCELAR = r"(cancelar|cancela|cancelar clase|cancelar reserva)"


def cancelar(documento, estado):
    """Primero por API. Si la bloquean, se lo dejamos al navegador."""
    activa = reserva_activa(documento)
    if not activa:
        enviar("No tienes ninguna reserva activa para cancelar.")
        return
    if ya_paso(activa):
        enviar(f"No cancele nada: esa clase ya se dicto.\n{describir(activa)}\n"
               "El sistema la sigue mostrando, pero no te bloquea.")
        return

    try:
        res = api_post("cancel", bookingId=activa.get("bookingId"),
                       token=activa.get("cancelToken", ""), source="telegram_bot")
    except (requests.RequestException, ValueError) as e:
        log(f"cancel fallo: {e}")
        res = {"ok": False, "error": str(e)}

    if res.get("ok"):
        clase = str(activa.get("clase", "")).strip()
        if clase.isdigit() and estado.get("ultima_clase_reservada") == int(clase):
            estado["ultima_clase_reservada"] = int(clase) - 1 or None
            guardar_estado(estado)
        enviar(f"CANCELADO\n{describir(activa)}\nTu cupo quedo liberado.")
        return

    if bloqueado_por_entorno(res):
        poner_orden(estado, activa.get("subnivel"), activa.get("clase"),
                    None, None, accion="cancelar")
        enviar("Cancelando desde Brightspace, dame un minuto.")
        return

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

    o = estado.get("orden")
    if o:
        partes.append(f"Orden en curso:\n{o.get('accion')} {o.get('subnivel','')} "
                      f"clase {o.get('clase','?')} el {o.get('fecha','?')} "
                      f"a las {o.get('hora','?')}")

    c = estado.get("cita")
    if c and c.get("cuando"):
        partes.append("Cita puesta:\n" + describir_cita(c))
    else:
        partes.append("No tienes ninguna cita puesta.")
    enviar("\n\n".join(partes))


# ---------------------------------------------------------- modos

def atender(cfg, estado, documento, texto):
    if es_comando(texto, PATRON_CANCELAR):
        cancelar(documento, estado)
        return
    if es_comando(texto, PATRON_ESTADO):
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
        falta = "" if h_clase else "\n\nDime a que hora quieres la clase y la anoto."
        enviar("Cita puesta.\n" + describir_cita(estado["cita"]) + falta)
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
        enviar("Anotado.\n" + describir_cita(c))
        return

    if params.get("fecha"):
        objetivo = datetime.strptime(params["fecha"], "%Y-%m-%d").date()
    else:
        objetivo = proximo_dia_habil(datetime.now(TZ).date() + timedelta(days=1), cfg)

    motivo = sin_clases(objetivo, cfg)
    if motivo:
        alterno = proximo_dia_habil(objetivo + timedelta(days=1), cfg)
        enviar(f"El {fecha_bonita(objetivo)} no hay clases porque {motivo}.\n"
               f"El siguiente dia habil es el {fecha_bonita(alterno)}. "
               "Escribeme la hora si quieres que lo intente ahi.")
        return

    if bloqueante(activa):
        cuando = fecha_de_reserva(activa)
        enviar("No puedo reservar todavia: tienes una clase pendiente sin ver.\n"
               f"{describir(activa)}\n\n"
               + (f"Escribeme despues de las {(cuando + timedelta(hours=2)):%H:%M} "
                  "y la programo de una." if cuando else
                  "Escribeme cuando ya la hayas visto."))
        return

    poner_orden(estado, params["subnivel"], str(params["clase"]),
                params["hora"], objetivo)
    enviar(f"Voy por {params['subnivel']} clase {params['clase']} el "
           f"{fecha_bonita(objetivo)} a las {params['hora']}.\n"
           "Entro a Brightspace y te confirmo en un minuto.")


def modo_escuchar(cfg, estado, documento):
    """Cada minuto. Nunca abre el navegador y nunca ve la contrasena."""
    c = cita_pendiente(estado)
    if c:
        log(f"Convierto la cita de las {c['cuando']} en orden")
        activa = reserva_activa(documento)
        s, cl, _ = proxima_clase(cfg, estado, activa)
        fecha = (datetime.strptime(c["para"], "%Y-%m-%d").date() if c.get("para")
                 else datetime.now(TZ).date() + timedelta(days=1))
        estado["cita"] = None
        poner_orden(estado, c.get("subnivel") or s, c.get("clase") or cl,
                    c.get("hora") or cfg["hora"], fecha)

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


def modo_reservar(cfg, estado, documento):
    """Lo dispara el workflow de escuchar cuando hay una orden pendiente."""
    orden = orden_vigente(estado)
    if not orden:
        log("No hay ninguna orden pendiente. Nada que hacer.")
        return
    log(f"Ejecutando orden: {orden}")
    ejecutar_orden(cfg, estado, documento, orden)


def modo_recordar(cfg, estado, documento):
    """9:20 p.m. Justo despues de la clase, cuando el cupo queda libre."""
    manana = datetime.now(TZ).date() + timedelta(days=1)
    motivo = sin_clases(manana, cfg)
    if motivo:
        habil = proximo_dia_habil(manana, cfg)
        abren = apertura_de(habil, cfg)
        comando = f"entra {etiqueta_dia(abren.date())} {abren:%H:%M}"
        enviar(f"Manana no hay clases porque {motivo}.\n"
               f"El siguiente dia habil es el {fecha_bonita(habil)}, "
               f"y sus cupos abren el {fecha_bonita(abren)} a las {abren:%H:%M}.\n\n"
               f"Escribeme '{comando}' y la busco apenas salgan.",
               botones=[[comando], [TEXTO_ESTADO]])
        return

    activa = reserva_activa(documento)
    if activa and not ya_paso(activa):
        cuando = fecha_de_reserva(activa)
        if cuando and cuando.date() == manana:
            log("Ya tiene reservada la de manana, no molesto.")
            return

    menu(cfg, estado, documento, "Ya termino tu clase. Quieres programar la de manana?")


def modo_diagnostico(cfg, estado, documento):
    """Que responde de verdad la API hoy. No reserva nada."""
    subnivel = cfg["subnivel"]
    activa = reserva_activa(documento)
    if activa:
        subnivel = activa.get("subnivel") or subnivel
    lineas = [f"Diagnostico para subnivel {subnivel}"]

    try:
        acc = api_get("getAccessConfig")
        lineas.append(f"getAccessConfig: web={acc.get('webBookingEnabled')} "
                      f"iframe={acc.get('iframeBookingEnabled')}")
    except (requests.RequestException, ValueError) as e:
        lineas.append(f"getAccessConfig: fallo ({e})")

    for accion, params in [("getAvailableSlots", {"subnivel": subnivel}),
                           ("verifyStudentBooking", {"documento": documento})]:
        try:
            resp = api_get(accion, **params)
        except (requests.RequestException, ValueError) as e:
            lineas.append(f"{accion}: fallo ({e})")
            continue
        if bloqueado_por_entorno(resp):
            lineas.append(f"{accion}: bloqueado fuera del iframe (esperado)")
        elif isinstance(resp, dict) and resp.get("ok") is False:
            lineas.append(f"{accion}: error - {resp.get('error','sin detalle')[:80]}")
        else:
            slots = resp.get("slots") or resp.get("result") or []
            lineas.append(f"{accion}: ok ({len(slots)} elementos)" if slots
                          else f"{accion}: ok")

    bs = cfg.get("brightspace") or {}
    lineas.append(f"brightspace: curso {bs.get('curso_id','?')}, "
                  f"{len(bs.get('topics') or {})} clases mapeadas")
    o = estado.get("orden")
    lineas.append(f"orden pendiente: {o.get('accion')} clase {o.get('clase')}" if o
                  else "orden pendiente: ninguna")

    texto = "\n".join(lineas)
    log(texto.replace("\n", " | "))
    enviar(texto)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modo",
                    choices=["escuchar", "reservar", "recordar", "diagnostico"],
                    required=True)
    args = ap.parse_args()

    documento = os.environ.get("SMARTFLEX_DOC")
    if not documento:
        enviar("Falta la variable SMARTFLEX_DOC.")
        sys.exit(1)

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    base = {"ultima_clase_reservada": None, "subnivel": cfg["subnivel"],
            "ultimo_update_procesado": 0, "cita": None, "orden": None}
    estado = base
    if ESTADO_FILE.exists():
        try:
            estado = {**base, **json.loads(ESTADO_FILE.read_text(encoding="utf-8"))}
        except ValueError:
            log("estado.json ilegible, uso valores por defecto.")

    {"escuchar": modo_escuchar, "reservar": modo_reservar,
     "recordar": modo_recordar, "diagnostico": modo_diagnostico}[args.modo](
        cfg, estado, documento)


if __name__ == "__main__":
    main()
