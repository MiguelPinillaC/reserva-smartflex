# Reserva Smart Flex

Repositorio **público** (para que Actions sea gratis e ilimitado).
Nada personal vive en estos archivos: tu documento y tu correo van en secrets.

```
reserva-smartflex/
├── reservar_smartflex.py
├── config.json          preferencias, sin datos personales
├── estado.json          la memoria (se actualiza sola)
└── .github/workflows/
    ├── preguntar.yml    8:00 p.m.
    ├── reservar.yml     12:15 a.m.
    └── escuchar.yml     cada 10 minutos
```

---

## El día a día

**8:00 p.m.** — el bot te escribe:

> ¿Quieres clase el viernes 28 de agosto?
> Te propongo: A1.2 clase 8.
>
> `[ 18:00 ] [ 19:00 ] [ 20:00 ]`  `[ No reservar ]`

Propone la 8 porque la última que reservaste fue la 7.

**12:15 a.m.** — reserva y te confirma. Si no respondiste, no reserva nada.

**Cada 10 minutos** — revisa si le escribiste `cancelar` o `estado`.

---

## Montaje

### 1. Crear el repositorio público

En github.com, **New repository** → visibilidad **Public** → sube los archivos
respetando la ruta `.github/workflows/`.

### 2. El bot de Telegram

1. Busca **@BotFather** → `/newbot` → te da un **token**.
2. Abre tu bot y mándale `hola`. Sin ese primer mensaje no puede escribirte.
3. Entra a `https://api.telegram.org/bot<TOKEN>/getUpdates` y busca
   `"chat":{"id":123456789` — ese número es tu **chat id**.

### 3. Secrets

**Settings → Secrets and variables → Actions**:

| Secret | Valor |
|---|---|
| `SMARTFLEX_DOC` | tu número de documento |
| `SMARTFLEX_EMAIL` | tu correo |
| `TELEGRAM_TOKEN` | el token de BotFather |
| `TELEGRAM_CHAT_ID` | tu chat id |
| `SMARTFLEX_DEVICE_ID` | opcional, un UUID cualquiera |

Los secrets no son visibles aunque el repositorio sea público, ni siquiera para
quien lo clone. GitHub además los enmascara como `***` si aparecen en un log.

### 4. Permisos de escritura

**Settings → Actions → General → Workflow permissions** → **Read and write
permissions**. Sin esto el bot no puede guardar `estado.json`.

### 5. Probar

Pestaña **Actions**, cada workflow tiene **Run workflow**:

1. Corre **1. Preguntar** y mira llegar el mensaje.
2. Responde con una hora.
3. Corre **2. Reservar** y revisa la confirmación.
4. Escríbele `estado` y espera a que corra **3. Escuchar**.

---

## Qué es público y qué no

| Público | Privado |
|---|---|
| El código y `config.json` | Tu documento (secret) |
| `estado.json`: en qué clase vas | Tu correo (secret) |
| **Los logs de Actions** | El token del bot y el chat id (secrets) |

**Los logs de Actions de un repositorio público los puede leer cualquiera.** Por
eso el script no escribe el contenido de tus mensajes de Telegram ni el texto
completo de las confirmaciones: solo la primera línea (`RESERVADO`,
`NO RESERVADO`) y datos operativos. Si necesitas depurar algo puntual, agrega
`LOG_DETALLADO: "1"` a las variables de entorno del workflow, míralo, y quítalo
después.

**Nadie más puede darle órdenes a tu bot.** El script ignora cualquier mensaje
que no venga de tu `chat_id`, así que si un desconocido encuentra el bot y le
escribe, no pasa nada.

Lo único que queda expuesto y vale la pena que sepas: `estado.json` deja ver tu
subnivel y en qué clase vas. Si te incomoda, el repositorio puede ser privado
con el cron de `escuchar` en `*/30 * * * *`.

---

## La cancelación

Le escribes `cancelar` y libera tu cupo. **No es instantáneo**: el workflow
revisa cada 10 minutos, y GitHub encola las tareas programadas, así que a veces
tarda más. Si necesitas cancelar ya mismo, la página del curso son 20 segundos.

Ejecuta de una, sin repreguntar, porque una confirmación de ida y vuelta te
costaría otros 10 minutos. Por eso solo reacciona si el mensaje es exactamente
esa palabra: `no quiero cancelar` no dispara nada. La propia página advierte que
cancelar no se puede deshacer.

---

## La memoria

Prioridad para proponer la clase de la noche:

1. Si tienes una reserva activa, la siguiente a esa.
2. Si no, la siguiente a la última que reservó (`estado.json`).
3. Si nunca ha reservado, la de `config.json`.

Al reservar guarda el número; al cancelar lo devuelve un paso atrás. Si se
desalinea, edita `estado.json` o respóndele explícito una noche
(`clase 12 19:00`) y desde ahí sigue contando bien.

`max_clase` en `config.json` (por ejemplo `9`) hace que al llegar al final del
subnivel te pregunte cuál sigue en vez de proponer una clase inexistente.
Déjalo en `null` si no lo necesitas.

---

## Otros detalles

- El cron de GitHub puede atrasarse entre 5 y 20 minutos.
- **Una reserva a la vez.** El sistema no deja programar si ya tienes otra
  activa; el mensaje de las 8 p.m. lo revisa antes de preguntarte.
- Silencia el chat de noche si no quieres que suene a las 12:15.
- GitHub desactiva las tareas programadas si el repositorio pasa 60 días sin
  actividad. Te llega un correo avisando.
