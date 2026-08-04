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
from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RepeatGroup,
    RunningWorkout,
    SportType,
    StepType,
    TargetType,
    WorkoutSegment,
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


# ---------------------------------------------------------------------------
# Tools de escritura: creación y gestión de entrenamientos estructurados.
#
# Usa el módulo garminconnect.workout, que expone modelos tipados sobre el
# endpoint interno (no documentado oficialmente) workout-service de Garmin
# Connect. El formato de "steps" que reciben estas tools está pensado para
# ser el mismo que ya usábamos con FitMCP, para no tener que aprender una
# sintaxis nueva.
# ---------------------------------------------------------------------------

_STEP_TYPE_MAP = {
    "warmup": (StepType.WARMUP, "warmup", 1),
    "cooldown": (StepType.COOLDOWN, "cooldown", 2),
    "interval": (StepType.INTERVAL, "interval", 3),
    "recovery": (StepType.RECOVERY, "recovery", 4),
    "rest": (StepType.REST, "rest", 5),
}


def _pace_to_mps(pace_str: str) -> float:
    """Convierte un ritmo 'mm:ss' (minutos por km) a metros/segundo.

    Garmin guarda internamente los targets de ritmo como velocidad, no como
    ritmo — este es el punto donde más fácil es meter un error de signo.
    """
    minutes_str, seconds_str = pace_str.split(":")
    total_seconds_per_km = int(minutes_str) * 60 + int(seconds_str)
    return round(1000.0 / total_seconds_per_km, 4)


def _build_target(target: Optional[dict]) -> dict:
    """Traduce {'type': 'pace'|'heart_rate', 'min': .., 'max': ..} al par de
    campos (targetType + targetValueOne/Two) que espera Garmin.

    Para 'pace': 'min' es el ritmo MÁS LENTO (ej. '5:10') y 'max' el MÁS
    RÁPIDO (ej. '5:00'). Como Garmin lo guarda como velocidad (m/s), quedan
    invertidos: ritmo lento -> velocidad baja -> targetValueOne; ritmo
    rápido -> velocidad alta -> targetValueTwo.

    Para 'heart_rate': 'min'/'max' van directo en bpm, sin conversión.
    """
    if not target:
        return {
            "targetType": {
                "workoutTargetTypeId": TargetType.NO_TARGET,
                "workoutTargetTypeKey": "no.target",
                "displayOrder": 1,
            }
        }

    ttype = target.get("type")
    if ttype == "pace":
        slow_pace = target.get("min")
        fast_pace = target.get("max")
        return {
            "targetType": {
                "workoutTargetTypeId": TargetType.PACE_ZONE,
                "workoutTargetTypeKey": "pace.zone",
                "displayOrder": 6,
            },
            "targetValueOne": _pace_to_mps(slow_pace) if slow_pace else None,
            "targetValueTwo": _pace_to_mps(fast_pace) if fast_pace else None,
        }
    elif ttype == "heart_rate":
        return {
            "targetType": {
                "workoutTargetTypeId": TargetType.HEART_RATE_ZONE,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4,
            },
            "targetValueOne": target.get("min"),
            "targetValueTwo": target.get("max"),
        }
    else:
        raise ValueError(
            f"Tipo de target no soportado: {ttype!r} (usar 'pace' o 'heart_rate')"
        )


def _estimate_step_seconds(step: dict) -> float:
    """Estima la duración de un paso para el campo informativo
    estimatedDurationInSecs. No afecta el entrenamiento real (que corre por
    duración o distancia real), solo lo que Garmin muestra como estimado."""
    if step.get("type") == "repeat":
        one_iter = sum(_estimate_step_seconds(s) for s in step["steps"])
        return one_iter * step["reps"]

    if "duration_seconds" in step:
        return float(step["duration_seconds"])

    if "distance_meters" in step:
        target = step.get("target")
        pace_mps = None
        if target and target.get("type") == "pace":
            paces = [p for p in (target.get("min"), target.get("max")) if p]
            if paces:
                pace_mps = sum(_pace_to_mps(p) for p in paces) / len(paces)
        if not pace_mps:
            pace_mps = 1000.0 / 360  # default conservador: 6:00/km
        return step["distance_meters"] / pace_mps

    return 0.0


class _OrderCounter:
    """Contador simple para stepOrder, que Garmin exige incremental dentro
    de todo el workout (incluyendo los pasos anidados en repeticiones)."""

    def __init__(self):
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _build_executable_step(step: dict, counter: _OrderCounter) -> ExecutableStep:
    step_type_key = step["type"]
    if step_type_key not in _STEP_TYPE_MAP:
        raise ValueError(
            f"Tipo de paso no soportado: {step_type_key!r} "
            "(usar 'warmup', 'interval', 'recovery', 'cooldown', 'rest' o 'repeat')"
        )
    step_type_id, step_type_str, display_order = _STEP_TYPE_MAP[step_type_key]

    if "duration_seconds" in step:
        end_condition = {
            "conditionTypeId": ConditionType.TIME,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        }
        end_value = float(step["duration_seconds"])
    elif "distance_meters" in step:
        end_condition = {
            "conditionTypeId": ConditionType.DISTANCE,
            "conditionTypeKey": "distance",
            "displayOrder": 3,
            "displayable": True,
        }
        end_value = float(step["distance_meters"])
    else:
        raise ValueError(
            f"El paso {step!r} necesita 'duration_seconds' o 'distance_meters'"
        )

    target_fields = _build_target(step.get("target"))

    return ExecutableStep(
        stepOrder=counter.next(),
        stepType={
            "stepTypeId": step_type_id,
            "stepTypeKey": step_type_str,
            "displayOrder": display_order,
        },
        endCondition=end_condition,
        endConditionValue=end_value,
        **target_fields,
    )


def _build_steps(steps: list[dict], counter: _OrderCounter) -> list:
    result = []
    for step in steps:
        if step.get("type") == "repeat":
            reps = step["reps"]
            nested = _build_steps(step["steps"], counter)
            result.append(
                RepeatGroup(
                    stepOrder=counter.next(),
                    stepType={
                        "stepTypeId": StepType.REPEAT,
                        "stepTypeKey": "repeat",
                        "displayOrder": 6,
                    },
                    numberOfIterations=reps,
                    workoutSteps=nested,
                    endCondition={
                        "conditionTypeId": ConditionType.ITERATIONS,
                        "conditionTypeKey": "iterations",
                        "displayOrder": 7,
                        "displayable": True,
                    },
                    endConditionValue=float(reps),
                )
            )
        else:
            result.append(_build_executable_step(step, counter))
    return result


def _build_running_workout(title: str, steps: list[dict], description: Optional[str]) -> RunningWorkout:
    """Construye el objeto RunningWorkout a partir del formato de steps
    compartido por create_workout y update_workout."""
    counter = _OrderCounter()
    built_steps = _build_steps(steps, counter)
    total_duration = sum(_estimate_step_seconds(s) for s in steps)

    return RunningWorkout(
        workoutName=title,
        description=description,
        estimatedDurationInSecs=int(total_duration),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={
                    "sportTypeId": SportType.RUNNING,
                    "sportTypeKey": "running",
                    "displayOrder": 1,
                },
                workoutSteps=built_steps,
            )
        ],
    )


@mcp.tool()
def create_workout(
    title: str,
    steps: list[dict],
    date_str: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Crea un entrenamiento de running estructurado en Garmin Connect y,
    si se indica date_str, lo programa en el calendario para que se
    sincronice automáticamente al reloj.

    Args:
        title: Nombre del entrenamiento.
        steps: Lista de pasos. Cada paso es un dict con:
            - type: 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest' | 'repeat'
            - duration_seconds O distance_meters (no aplica si type='repeat')
            - target (opcional): {'type': 'pace'|'heart_rate', 'min': X, 'max': Y}
              Para 'pace', min/max van en formato 'mm:ss' (minutos por km),
              donde 'min' es el ritmo MÁS LENTO y 'max' el MÁS RÁPIDO.
              Para 'heart_rate', min/max van directo en bpm.
            - Para repeticiones: {'type': 'repeat', 'reps': N, 'steps': [...]}
        date_str: Fecha YYYY-MM-DD para programarlo en el calendario Garmin.
            Si se omite, el entreno queda solo en la biblioteca de workouts.
        description: Descripción del entrenamiento (opcional).
    """
    client = get_client()
    workout = _build_running_workout(title, steps, description)

    result = client.upload_running_workout(workout)
    workout_id = result.get("workoutId")

    scheduled = False
    if date_str and workout_id:
        client.schedule_workout(workout_id, date_str)
        scheduled = True

    return {
        "workout_id": workout_id,
        "titulo": title,
        "programado_para": date_str if scheduled else None,
    }


@mcp.tool()
def update_workout(
    workout_id: str,
    title: str,
    steps: list[dict],
    date_str: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Modifica EN SITIO un entrenamiento que ya existe en Garmin Connect
    (mantiene el mismo workout_id, así que cualquier programación en el
    calendario que ya apuntaba a él sigue siendo válida — no crea un
    duplicado). Usar para corregir un entreno ya subido en vez de crear
    otro y borrar el viejo.

    Hay que mandar el entreno CORREGIDO COMPLETO (reemplaza todo el
    contenido, no solo la parte que cambia) — Garmin actualiza por PUT.

    Args:
        workout_id: ID del entreno a modificar (sale de create_workout o
            get_planned_workouts).
        title: Nombre del entrenamiento (corregido).
        steps: Lista de pasos completa, mismo formato que create_workout.
        date_str: Si se indica, además MUEVE el entreno a esa fecha en el
            calendario. Si se omite, el entreno se queda en su fecha actual.
        description: Descripción del entrenamiento (opcional).
    """
    client = get_client()
    workout = _build_running_workout(title, steps, description)
    workout_dict = workout.to_dict()

    client.update_workout(workout_id, workout_dict)

    moved = False
    if date_str:
        client.schedule_workout(workout_id, date_str)
        moved = True

    return {
        "workout_id": workout_id,
        "titulo": title,
        "actualizado": True,
        "movido_a": date_str if moved else None,
    }


@mcp.tool()
def get_planned_workouts(fecha_inicio: str, fecha_fin: str) -> list[dict]:
    """Entrenamientos programados en el calendario de Garmin Connect en un
    rango de fechas.

    Args:
        fecha_inicio: YYYY-MM-DD
        fecha_fin: YYYY-MM-DD
    """
    client = get_client()
    start = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
    end = datetime.strptime(fecha_fin, "%Y-%m-%d").date()

    out = []
    seen_months = set()
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        if (year, month) not in seen_months:
            seen_months.add((year, month))
            data = client.get_scheduled_workouts(year, month)
            for w in data.get("workoutsScheduled", data.get("workouts", []) if isinstance(data, dict) else []):
                w_date = w.get("date") or w.get("calendarDate")
                if w_date and start.isoformat() <= w_date <= end.isoformat():
                    out.append({
                        "scheduled_id": w.get("id") or w.get("scheduledWorkoutId"),
                        "workout_id": w.get("workoutId"),
                        "titulo": w.get("title") or w.get("workoutName"),
                        "fecha": w_date,
                    })
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


@mcp.tool()
def delete_workout(workout_id: str) -> dict:
    """Borra un entrenamiento de la biblioteca de Garmin Connect (y todas
    sus fechas programadas en el calendario).

    Args:
        workout_id: ID del entreno (viene de create_workout o get_planned_workouts)
    """
    client = get_client()
    client.delete_workout(workout_id)
    return {"borrado": True, "workout_id": workout_id}


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
