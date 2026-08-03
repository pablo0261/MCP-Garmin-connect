# garmin-mcp — tu propio conector a Garmin

## Dos formas de usarlo

**A) Local (Claude Desktop / Claude Code)** — ya lo tenés funcionando. No
necesita nada de lo que sigue.

**B) Remoto (chatear acá, en claude.ai)** — necesita estar desplegado en
internet con una URL pública. Pasos abajo.

---

## B) Desplegar remoto en Railway (gratis para este uso)

### 1. Subir el código a un repo de GitHub
Creá un repo (puede ser privado) con estos 4 archivos: `garmin_mcp_server.py`,
`requirements.txt`, `Procfile`, `README.md`.

### 2. Exportar tu sesión de Garmin como texto
En tu PC, donde ya corriste `--login`:
```
python garmin_mcp_server.py --export-token
```
Copiá el string JSON larguísimo que imprime (una sola línea). Es tu sesión
de Garmin — tratalo como una contraseña, no lo compartas ni lo subas al repo.

### 3. Crear el proyecto en Railway
- Entrá a railway.app, "New Project" → "Deploy from GitHub repo" → elegí tu repo.
- En la pestaña **Variables**, agregá:
  - `GARMIN_TOKENS` = el string que copiaste en el paso 2
  - `MCP_SHARED_SECRET` = inventate una clave larga random (ej. generá una con `python -c "import secrets; print(secrets.token_urlsafe(32))"`) — es la contraseña que va a pedir el servidor para no quedar abierto a cualquiera en internet.
- Railway detecta el `Procfile` solo y lo despliega.
- Cuando termine, te da una URL pública tipo `https://tu-proyecto.up.railway.app`.

### 4. Conectarlo en claude.ai
- Settings → Connectors → Add custom connector.
- URL: `https://tu-proyecto.up.railway.app/mcp`
- Cuando pida autenticación, usá el `MCP_SHARED_SECRET` que definiste (como Bearer token / API key, según cómo lo pida la UI).

Con eso, las tools (`get_activities`, `get_activity_laps`, `get_daily_stats`,
`get_sleep`) van a estar disponibles acá en el chat, igual que estaban con
FitMCP.

---

## Herramientas disponibles (v1 — solo lectura)

- `get_activities(fecha_inicio, fecha_fin)`
- `get_activity_laps(activity_id)`
- `get_daily_stats(fecha)`
- `get_sleep(fecha)`

**Falta (fase 2):** creación de entrenamientos estructurados en el calendario
de Garmin (endpoint interno no documentado, `workout-service`). Mientras
tanto seguimos cargando las sesiones a mano con el detalle que te doy en el
plan semanal.

## Nota de seguridad

`MCP_SHARED_SECRET` es la única barrera entre tu cuenta de Garmin (vía
`GARMIN_TOKENS`) y cualquiera que encuentre la URL. No es una solución
enterprise, es un candado razonable para uso personal. No compartas esa URL
ni el secret.

## Nota honesta

Esto usa la misma técnica no oficial que FitMCP: ingeniería inversa del
login web/app de Garmin, mantenida por la comunidad (`garminconnect`), no
una API pública documentada. Estable en la práctica, pero puede romperse si
Garmin cambia algo internamente.
