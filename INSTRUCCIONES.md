# Reserva Smart Flex

Bot de Telegram que reserva tus clases de Smart Flex entrando por donde entras
tú: Brightspace.

```
reserva-smartflex/
├── reservar_smartflex.py
├── config.json          preferencias y mapa de clases, sin datos personales
├── estado.json          la memoria (se actualiza sola)
└── .github/workflows/
    ├── escuchar.yml     cada minuto   · sin navegador, sin contraseña
    ├── reservar.yml     por demanda   · Playwright, el único con la contraseña
    ├── recordar.yml     9:20 p.m.     · sin navegador
    └── diagnostico.yml  a mano        · no reserva nada
```

---

## Por qué está armado así

En agosto de 2026 la institución apagó el agendamiento por web directa y lo dejó
solo dentro de Brightspace. No hay que adivinarlo, la API lo dice:

```
getAccessConfig → {"webBookingEnabled": false, "iframeBookingEnabled": true}
```

Así que el bot ya no le pide horarios a la API por su cuenta. Abre Brightspace
con tu usuario, entra al módulo "Programa tu clase virtual N" y deja que el
widget de reservas corra en su iframe real. **Se automatiza el clic, no el
permiso.**

Eso obliga a usar un navegador, que es lento y necesita tu contraseña. Por eso
el trabajo va partido en dos, y esa partición es lo que mantiene esto barato y
seguro:

| | corre | usa navegador | ve la contraseña |
|---|---|---|---|
| **escuchar** | cada minuto | no | **no** |
| **reservar** | solo cuando hay algo que reservar | sí | sí |
| **recordar** | 9:20 p.m. | no | no |

`escuchar` lee tus mensajes, responde `estado`, cancela, anota citas y deja una
**orden** en `estado.json`. Cuando hay orden, dispara `reservar`, que es el
único que abre Chromium. Son un par de ejecuciones al día, no 1.440.

---

## El día a día

**9:20 p.m.** — apenas termina tu clase, el bot te escribe:

> Ya terminó tu clase. ¿Quieres programar la de mañana?
>
> A1.2 clase 12
> Reservaría para el lunes 7 de septiembre.
>
> `[ 18:00 ] [ 19:00 ] [ 20:00 ]`  `[ No reservar ]`

Propone la 12 porque la última que reservaste fue la 11.

**Tocas una hora** — en menos de un minuto `escuchar` la recoge, deja la orden y
lanza `reservar`. Un minuto después te llega `RESERVADO` con la confirmación.

Si a esa hora no había cupo, te dice cuáles sí hay y tocas otra.

**Cada minuto** — atiende `cancelar`, `estado`, o una orden como
`clase 13 viernes 7pm`.

---

## Montaje

### 1. El bot de Telegram

1. Busca **@BotFather** → `/newbot` → te da un **token**.
2. Abre tu bot y mándale `hola`. Sin ese primer mensaje no puede escribirte.
3. Entra a `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca
   `"chat":{"id":123456789` — ese número es tu **chat id**.

### 2. Secrets

**Settings → Secrets and variables → Actions**:

| Secret | Valor | Lo usa |
|---|---|---|
| `SMARTFLEX_DOC` | tu número de documento | todos |
| `SMARTFLEX_EMAIL` | tu correo | todos |
| `TELEGRAM_TOKEN` | el token de BotFather | todos |
| `TELEGRAM_CHAT_ID` | tu chat id | todos |
| `SMARTFLEX_PASS` | **tu contraseña de Brightspace** | solo `reservar` |
| `SMARTFLEX_USER` | tu usuario, si no es el documento | solo `reservar` |
| `SMARTFLEX_DEVICE_ID` | opcional, un UUID cualquiera | todos |

### 3. Permisos de escritura

**Settings → Actions → General → Workflow permissions** → **Read and write
permissions**. Sin esto el bot no puede guardar `estado.json` ni disparar
`reservar` solo.

### 4. El reloj externo

El `schedule` de GitHub se atrasa entre 5 y 20 minutos, así que los workflows
los dispara un reloj externo por `workflow_dispatch`. Está explicado en
`RELOJ-EXTERNO.md`. Los horarios ahora son:

| Trabajo | ARCHIVO | Horario (Bogotá) |
|---|---|---|
| Escuchar | `escuchar.yml` | **cada minuto** |
| Recordar | `recordar.yml` | 21:20 todos los días |

`reservar.yml` **no** va en el reloj: lo lanza `escuchar` solo cuando hace falta.

### 5. Probar

En **Actions**, corre **4. Diagnostico** a mano. Debe llegarte por Telegram algo
así:

```
Diagnostico para subnivel A1.2
getAccessConfig: web=False iframe=True
getAvailableSlots: bloqueado fuera del iframe (esperado)
verifyStudentBooking: ok
brightspace: curso 8052, 16 clases mapeadas
orden pendiente: ninguna
```

Ese `bloqueado fuera del iframe (esperado)` es la señal de que todo está como
debe: confirma que el camino directo está cerrado y por eso usamos el navegador.

Después escríbele `estado` al bot y espera un minuto.

---

## Seguridad: el repositorio es público

Se queda público a propósito — en repos públicos los minutos de Actions son
gratis e ilimitados, y correr cada minuto en uno privado costaría cientos de
dólares al mes.

Tus secrets **no** se filtran por eso: van cifrados, no los ve quien clone el
repo, y los PR desde forks no los reciben.

Lo que sí es público son **los logs y los artefactos de Actions**. Por eso
`reservar.yml` tiene reglas que no hay que relajar:

- **sin `trace` de Playwright** — un trace guarda los cuerpos de las peticiones,
  incluido el POST del login con la contraseña en claro
- **sin capturas, sin video, sin `upload-artifact`**
- **sin `ACTIONS_STEP_DEBUG`** en ese workflow
- la contraseña entra por `page.fill()`, nunca en una URL

El script tampoco vuelca el HTML de la página cuando algo falla: reporta solo el
tipo de error y un extracto corto.

Lo único que queda expuesto y vale la pena que sepas: `estado.json` deja ver tu
subnivel y en qué clase vas.

---

## La memoria

Prioridad para proponer la clase de la noche:

1. Si tienes una reserva activa, la siguiente a esa.
2. Si no, la siguiente a la última que reservó (`estado.json`).
3. Si nunca ha reservado, la de `config.json`.

`max_clase` (16) hace que al llegar al final te pregunte cuál sigue en vez de
proponer una clase que no existe. Si se desalinea, edita `estado.json` o
respóndele explícito una noche (`clase 12 19:00`).

---

## Si te cambian de curso

`config.json → brightspace` tiene el id del curso y el mapa de cada clase al
módulo de Brightspace que la agenda. Cuando pases a otro curso hay que
refrescarlo. Entra a Brightspace, abre la consola del navegador y corre:

```js
const j = await (await fetch('/d2l/api/le/1.67/<CURSO_ID>/content/toc')).json();
const out=[]; (function w(m){for(const x of m||[]){for(const t of x.Topics||[])out.push(t);w(x.Modules);}})(j.Modules);
out.filter(t=>/programa tu clase|schedule your virtual/i.test(t.Title))
   .forEach(t=>console.log(t.Title, '->', t.TopicId));
```

El `<CURSO_ID>` sale de `/d2l/api/lp/1.9/enrollments/myenrollments/`.

---

## La cancelación

Le escribes `cancelar` y libera tu cupo. Primero lo intenta por API, que es
instantáneo. Si esa vía también está cerrada, deja una orden y lo hace con el
navegador, lo que tarda un minuto más. La propia página advierte que cancelar no
se puede deshacer, así que solo reacciona si el mensaje es exactamente esa
palabra: `no quiero cancelar` no dispara nada.

---

## Otros detalles

- **Una reserva a la vez.** El sistema no deja programar si ya tienes otra
  activa; el mensaje de las 9:20 p.m. lo revisa antes de preguntarte.
- La confirmación de que una reserva quedó **no** se lee del HTML del widget,
  se verifica contra `verifyStudentBooking`, que es la misma fuente que usa el
  sistema. Si el widget dice una cosa y la API otra, gana la API.
- Silencia el chat de noche si no quieres que suene.
- GitHub desactiva las tareas programadas si el repositorio pasa 60 días sin
  actividad. Como aquí manda el reloj externo, esto no aplica.
