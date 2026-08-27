#!/usr/bin/env bash
#
# Instalador de la automatización Smart Flex.
#
# Crea el repositorio público, sube los archivos, activa los permisos de
# escritura y te va pidiendo los secrets uno por uno.
#
# Los valores de los secrets los escribes tú directamente en la terminal:
# no quedan en este archivo, ni en el historial, ni en el repositorio.
#
#   Uso:  bash instalar.sh
#

set -euo pipefail

NOMBRE_REPO="${1:-reserva-smartflex}"

echo
echo "════════════════════════════════════════════════"
echo "  Instalación de la automatización Smart Flex"
echo "════════════════════════════════════════════════"
echo

# ---------------------------------------------------------------- 1. gh

if ! command -v gh >/dev/null 2>&1; then
  echo "✗ Falta GitHub CLI."
  echo
  echo "  Instálalo con:   brew install gh"
  echo "  Y vuelve a correr este script."
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "→ No has iniciado sesión en GitHub. Abriendo el login..."
  echo "  (elige GitHub.com → HTTPS → autenticar en el navegador)"
  echo
  gh auth login
fi

echo "✓ GitHub CLI listo: $(gh api user --jq .login)"

# ---------------------------------------------------------------- 2. archivos

FALTAN=0
for f in reservar_smartflex.py config.json estado.json \
         .github/workflows/preguntar.yml \
         .github/workflows/reservar.yml \
         .github/workflows/escuchar.yml; do
  if [[ ! -f "$f" ]]; then
    echo "✗ Falta el archivo: $f"
    FALTAN=1
  fi
done
if [[ $FALTAN -eq 1 ]]; then
  echo
  echo "  Corre este script desde adentro de la carpeta del proyecto."
  exit 1
fi

echo "✓ Archivos del proyecto completos"

cat > .gitignore <<'EOF'
.device_id
__pycache__/
*.pyc
EOF

# ---------------------------------------------------------------- 3. repo

if [[ -d .git ]]; then
  echo "→ Ya hay un repositorio git aquí, lo reutilizo."
else
  git init -q
  git branch -M main
fi

git add -A
git commit -qm "Automatización de reservas Smart Flex" 2>/dev/null || echo "  (nada nuevo que confirmar)"

USUARIO=$(gh api user --jq .login)

if gh repo view "$USUARIO/$NOMBRE_REPO" >/dev/null 2>&1; then
  echo "→ El repositorio $NOMBRE_REPO ya existe, subo los cambios."
  git remote get-url origin >/dev/null 2>&1 || \
    git remote add origin "https://github.com/$USUARIO/$NOMBRE_REPO.git"
  git push -u origin main
else
  echo "→ Creando el repositorio público $NOMBRE_REPO..."
  gh repo create "$NOMBRE_REPO" --public --source=. --remote=origin --push
fi

echo "✓ Repositorio: https://github.com/$USUARIO/$NOMBRE_REPO"

# ---------------------------------------------------------------- 4. permisos

echo "→ Activando permisos de escritura para Actions..."
gh api -X PUT "repos/$USUARIO/$NOMBRE_REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=write \
  -F can_approve_pull_request_reviews=false >/dev/null

echo "✓ Actions puede guardar estado.json"

# ---------------------------------------------------------------- 5. secrets

echo
echo "────────────────────────────────────────────────"
echo "  Ahora los secrets. Los escribes tú."
echo "  No se ven en pantalla ni quedan en el historial."
echo "────────────────────────────────────────────────"
echo

pedir_secret () {
  local nombre="$1" descripcion="$2" valor
  echo
  echo "  $nombre — $descripcion"
  read -rsp "  valor: " valor
  echo
  if [[ -z "$valor" ]]; then
    echo "  ⚠ vacío, lo salto. Puedes ponerlo después con: gh secret set $nombre"
    return
  fi
  printf '%s' "$valor" | gh secret set "$nombre" --repo "$USUARIO/$NOMBRE_REPO"
  echo "  ✓ guardado"
}

pedir_secret SMARTFLEX_DOC    "tu número de documento"
pedir_secret SMARTFLEX_EMAIL  "el correo que usas al confirmar la reserva"
pedir_secret TELEGRAM_TOKEN   "el token que te dio BotFather"
pedir_secret TELEGRAM_CHAT_ID "tu chat id (el número de getUpdates)"

# Este no es sensible: es un identificador aleatorio de dispositivo.
DEVICE=$(python3 -c "import uuid;print(uuid.uuid4())" 2>/dev/null || uuidgen)
printf '%s' "$DEVICE" | gh secret set SMARTFLEX_DEVICE_ID --repo "$USUARIO/$NOMBRE_REPO"
echo
echo "  ✓ SMARTFLEX_DEVICE_ID generado automáticamente"

# ---------------------------------------------------------------- 6. fin

echo
echo "════════════════════════════════════════════════"
echo "  Listo."
echo "════════════════════════════════════════════════"
echo
echo "  Repositorio:  https://github.com/$USUARIO/$NOMBRE_REPO"
echo "  Actions:      https://github.com/$USUARIO/$NOMBRE_REPO/actions"
echo
echo "  Para probar ahora mismo:"
echo
echo "    gh workflow run preguntar.yml --repo $USUARIO/$NOMBRE_REPO"
echo
echo "  Te debería llegar el mensaje al Telegram en unos segundos."
echo "  Respóndele con una hora y luego corre:"
echo
echo "    gh workflow run reservar.yml --repo $USUARIO/$NOMBRE_REPO"
echo
echo "  Para ver qué pasó en la última corrida:"
echo
echo "    gh run list --repo $USUARIO/$NOMBRE_REPO --limit 3"
echo "    gh run view --log --repo $USUARIO/$NOMBRE_REPO"
echo
