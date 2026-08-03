"""
Servidor MCP propio para Garmin Connect.

Expone como "tools" MCP las consultas de datos deportivos que veníamos usando
vía FitMCP: actividades, laps/splits, y estadísticas diarias (sueño, HR en
reposo, body battery, estrés). Pensado para conectarlo a Claude Desktop /
Claude Code, no a claude.ai web (que solo admite MCP remotos por URL, no
servidores locales por stdio).

Requisitos:
    pip install garminconnect mcp

Autenticación:
    La primera vez, correlo una vez a mano para generar la sesión cacheada:

        python3 garmin_mcp_server.py --login

    Te va a pedir email y password de Garmin Connect (y el código de MFA si
    lo tenés activado) UNA sola vez. Guarda los tokens de sesión en
    ~/.garminconnect/ y los reutiliza en las corridas siguientes sin volver
    a pedir credenciales, igual que hace la app oficial.
"""

import argparse
import getpass
import os
import sys
from datetime import date, datetime
from typing import Optional

from garminconnect import Garmin
from garminconnect.exceptions import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from mcp.server import MCPServer

TOKEN_DIR = os.path.expanduser("~/.garminconnect")

mcp = MCPServer("garmin-mcp")

_client: Optional[Garmin] = None


def get_client() -> Garmin:
    """Devuelve un cliente Garmin autenticado.

    Estrategia en dos pasos:
    1. Intenta reusar el token guardado (GARMIN_TOKENS o el cacheado en disco).
       Es lo más rápido y no golpea el login de Garmin en cada arranque.
    2. Si el token no existe o expiró, hace login directo con email/password
       (variables de entorno GARMIN_EMAIL / GARMIN_PASSWORD) — pensado para
       una cuenta SIN verificación en dos pasos (MFA), ya que este flujo no
       tiene forma de pedir un código interactivamente en un servidor.

    Esto evita tener que exportar el token a mano cada vez que expira: en el
    peor caso, el próximo arranque re-loguea solo con las credenciales.
    """
    global _client
    if _client is not None:
        return _client

    # En deploy remoto (sin filesystem persistente) los tokens viajan como
    # variable de entorno; en local se leen del path cacheado en disco.
    tokenstore = os.getenv("GARMIN_TOKENS") or TOKEN_DIR
    try:
        client = Garmin()
        client.login(tokenstore)  # intenta reusar tokens guardados
        _client = client
        return _client
    except (
        FileNotFoundError,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
    ):
        pass  # el token no existe o expiró, probamos login directo abajo

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "No hay sesión guardada ni credenciales configuradas.\n"
            "Corré 'python3 garmin_mcp_server.py --login' localmente, o "
            "seteá GARMIN_EMAIL y GARMIN_PASSWORD como variables de entorno "
            "en Render para login automático."
        )

    try:
        client = Garmin(email=email, password=password)
        client.login()
        _client = client
        return _client
    except GarminConnectAuthenticationError as e:
        raise RuntimeError(
            f"Login automático con GARMIN_EMAIL/GARMIN_PASSWORD falló: {e}\n"
            "Verificá que el email y password sean correctos y que la cuenta "
            "no tenga verificación en dos pasos (MFA) activada."
        )
    except GarminConnectTooManyRequestsError as e:
        raise RuntimeError(
            f"Garmin bloqueó temporalmente los intentos de login (rate limit): {e}\n"
            "Esperá unos minutos antes de reintentar."
        )
    except GarminConnectConnectionError as e:
        raise RuntimeError(
            f"No se pudo conectar a Garmin para hacer login: {e}\n"
            "Puede ser un bloqueo temporal (Cloudflare) o un problema de red "
            "del lado de Garmin. Reintentá en unos minutos."
        )


@mcp.tool()
def get_activities(fecha_inicio: str, fecha_fin: str) -> list[dict]:
    """Actividades de Garmin (running, ciclismo, etc.) en un rango de fechas.

    Args:
        fecha_inicio: YYYY-MM-DD
        fecha_fin: YYYY-MM-DD
    """
    client = get_client()
    activities = client.get_activities_by_date(fecha_inicio, fecha_fin)
    out = []
    for a in activities:
        out.append({
            "id": a.get("activityId"),
            "nombre": a.get("activityName"),
            "tipo": a.get("activityType", {}).get("typeKey"),
            "fecha": a.get("startTimeLocal"),
            "distancia_km": round((a.get("distance") or 0) / 1000, 2),
            "duracion_minutos": round((a.get("duration") or 0) / 60, 1),
            "fc_media": a.get("averageHR"),
            "fc_maxima": a.get("maxHR"),
            "calorias": a.get("calories"),
            "cadencia_media_ppm": a.get("averageRunningCadenceInStepsPerMinute"),
            "ritmo_medio_min_km": _pace(a.get("averageSpeed")),
            "vo2max": a.get("vO2MaxValue"),
        })
    return out


@mcp.tool()
def get_activity_laps(activity_id: int) -> list[dict]:
    """Parciales/laps de una actividad puntual: ritmo y FC de cada tramo.

    Args:
        activity_id: ID de la actividad (viene de get_activities)
    """
    client = get_client()
    splits = client.get_activity_splits(activity_id)
    out = []
    for i, lap in enumerate(splits.get("lapDTOs", []), start=1):
        out.append({
            "lap": i,
            "distancia_km": round((lap.get("distance") or 0) / 1000, 2),
            "duracion_min": round((lap.get("duration") or 0) / 60, 2),
            "ritmo_min_km": _pace(lap.get("averageSpeed")),
            "fc_media": lap.get("averageHR"),
            "fc_max": lap.get("maxHR"),
            "cadencia_media": lap.get("averageRunCadence"),
        })
    return out


@mcp.tool()
def get_daily_stats(fecha: str) -> dict:
    """Estadísticas diarias: pasos, FC reposo, estrés, body battery.

    Args:
        fecha: YYYY-MM-DD
    """
    client = get_client()
    return client.get_stats(fecha)


@mcp.tool()
def get_sleep(fecha: str) -> dict:
    """Desglose de sueño de una noche puntual.

    Args:
        fecha: YYYY-MM-DD (fecha de la mañana en que te despertaste)
    """
    client = get_client()
    return client.get_sleep_data(fecha)


def _pace(speed_m_s: Optional[float]) -> Optional[str]:
    if not speed_m_s:
        return None
    min_per_km = 1000 / speed_m_s / 60
    minutes = int(min_per_km)
    seconds = round((min_per_km - minutes) * 60)
    return f"{minutes}:{seconds:02d}"


def _ask_mfa_code() -> str:
    return input("Código MFA (si tenés verificación en 2 pasos activada en Garmin): ").strip()


def do_login():
    print("=== Login a Garmin Connect (solo se hace una vez) ===")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    os.makedirs(TOKEN_DIR, exist_ok=True)
    client = Garmin(email, password, prompt_mfa=_ask_mfa_code)
    try:
        client.login(TOKEN_DIR)  # autentica y persiste los tokens en TOKEN_DIR
    except Exception as e:
        print(f"Error de login: {e}")
        sys.exit(1)
    print(f"Sesión guardada en {TOKEN_DIR}. Ya podés correr el servidor MCP normalmente.")


def build_remote_app():
    """App ASGI para despliegue remoto (Railway/Fly.io/VPS propia).

    SIN autenticación propia: el flujo "Vincular" de conectores custom de
    claude.ai intenta registrar un cliente OAuth automáticamente contra el
    servidor, y no ofrece una forma simple de mandar un token estático en el
    header. Implementar OAuth completo (registro dinámico de cliente,
    authorization endpoint, token endpoint) es un desarrollo bastante más
    grande, fuera de alcance para este uso personal.

    La única barrera de seguridad, por ahora, es que la URL de Render no es
    pública ni fácil de adivinar. No compartas esta URL. Si en algún momento
    querés subir el nivel de seguridad, la vía es implementar OAuth (fase 3).

    stateless_http=True: Render (plan free) apaga el proceso tras períodos
    de inactividad y lo reinicia en frío en el próximo request. El modo
    stateful de MCP guarda el session_id en memoria del proceso, así que
    cualquier reinicio de Render invalida la sesión y el cliente (Claude)
    recibe "Bad Request: Missing session ID". stateless_http evita esto:
    cada request se procesa de forma independiente, sin depender de una
    sesión previa en memoria. Es la opción correcta para este caso de uso
    (un solo usuario, hosting con cold starts).
    """
    inner_app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True)
    return inner_app


def do_export_token():
    """Imprime los tokens locales como un string JSON de una sola línea,
    listo para pegar en la variable de entorno GARMIN_TOKENS del hosting
    remoto (Railway/Fly.io). Requiere haber corrido --login antes."""
    client = Garmin()
    client.login(TOKEN_DIR)
    print(client.client.dumps())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="Hacer login inicial y guardar sesión")
    parser.add_argument("--export-token", action="store_true", help="Exportar la sesión local como string para GARMIN_TOKENS")
    parser.add_argument("--remote", action="store_true", help="Correr como servidor remoto (streamable-http) en vez de stdio local")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()

    if args.login:
        do_login()
    elif args.export_token:
        do_export_token()
    elif args.remote:
        import uvicorn
        app = build_remote_app()
        uvicorn.run(app, host="0.0.0.0", port=args.port)
    else:
        mcp.run(transport="stdio")
