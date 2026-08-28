# Reloj externo: disparar los workflows a la hora real

El `schedule` de GitHub no sirve para esto. Su propio equipo lo describe como una
cola que se drena en modo "mejor esfuerzo": bajo carga se atrasa o se descarta.
Tu corrida de las 00:15 se ejecutó a las 11:29.

`workflow_dispatch`, en cambio, se atiende casi al instante. Así que el arreglo
es dejar que un reloj externo llame a esa API en el minuto exacto. Todo tu código
Python se queda igual: solo cambia quién aprieta el botón.

---

## 1. Crear el token de GitHub

Este token deja lanzar workflows en un solo repositorio, nada más.

1. Entra a **github.com → Settings → Developer settings → Personal access tokens
   → Fine-grained tokens → Generate new token**.
2. Configúralo así:
   - **Token name**: `reloj-smartflex`
   - **Expiration**: la máxima que te deje
   - **Repository access**: *Only select repositories* → `reserva-smartflex`
   - **Permissions → Repository permissions → Actions**: `Read and write`
3. Genera y **copia el token**. Solo se muestra una vez.

Guárdalo en tu gestor de contraseñas. Si se filtra, lo único que alguien podría
hacer es lanzar estos workflows: no da acceso a tu código ni a tus secrets.

---

## 2. Crear la cuenta del reloj

Ve a **cron-job.org** y regístrate (gratis, sin tarjeta). Necesitas que el
servicio permita método POST, encabezados personalizados y cuerpo de la petición.
Si el que elijas no lo permite, sirve cualquier otro con esas tres capacidades.

**Ponle la zona horaria America/Bogota en tu perfil** antes de crear nada, o
tendrás que hacer la conversión a UTC a mano otra vez.

---

## 3. Los tres trabajos

Los tres apuntan al mismo tipo de dirección, cambiando solo el nombre del archivo:

```
https://api.github.com/repos/MiguelPinillaC/reserva-smartflex/actions/workflows/ARCHIVO/dispatches
```

Configuración idéntica en los tres:

- **Método**: `POST`
- **Encabezados**:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer TU_TOKEN`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Cuerpo**: `{"ref":"main"}`

| Trabajo | ARCHIVO | Horario (hora Bogotá) |
|---|---|---|
| Escuchar el chat | `escuchar.yml` | cada 10 minutos |
| Reservar | `reservar.yml` | 00:15 todos los días |
| Preguntar | `preguntar.yml` | 20:00 todos los días |

Una respuesta **204** significa éxito. GitHub no devuelve contenido en este
endpoint, así que 204 es lo correcto, no un error.

Si te devuelve **404**, casi siempre es el token sin el permiso de Actions o mal
copiado. Si devuelve **422**, el nombre del archivo no coincide.

---

## 4. Dejar el schedule como respaldo

No borres los `schedule:` de los YAML. Quedan como red de seguridad por si el
reloj externo falla algún día. Ahora que el código solo reserva la fecha que
abre ese día, una corrida atrasada ya no puede hacer daño: si le toca otro día,
no toca la reserva.

---

## 5. Comprobar que quedó fino

Al día siguiente:

```bash
gh run list --limit 20
```

Las corridas del reloj externo aparecen como `workflow_dispatch`, no como
`schedule`. Y en Telegram, cada mensaje de reserva ahora trae al final la hora
real de ejecución entre paréntesis. Si dice `(ejecutado 00:15)`, quedó perfecto.
Si dice otra hora, el desfase te queda a la vista sin tener que investigar.
