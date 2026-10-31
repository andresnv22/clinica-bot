"""Herramientas que el agente puede usar.

Cada herramienta tiene dos partes:
  1. La definición (TOOLS): lo que la IA ve, nombre, descripción y parámetros.
  2. La función de Python: lo que realmente se ejecuta cuando la IA la pide.

La IA nunca toca la base de datos directo. Solo pide "ejecuta agendar_cita con
estos datos" y nuestro código decide si se puede o no. Así el bot no puede
inventar horarios ni agendar dos citas a la misma hora.

El teléfono del paciente nunca lo escribe la IA: lo pone el sistema (viene de
WhatsApp). Así nadie puede ver ni cancelar citas ajenas, y no hay errores de
formato en el número.
"""

import re
from datetime import date, datetime, timedelta

from . import db

FORMATO = "%Y-%m-%d %H:%M"


# ---------- Validaciones ----------
# Los mensajes de error están escritos para que la IA entienda qué corregir.

def normalizar_hora(hora: str) -> str | None:
    """Convierte '3pm', '3:30 p.m.', '9:00', '15:00:00' en 'HH:MM'. None si no se entiende."""
    texto = hora.strip().lower().replace(".", "").replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?(am|pm)?", texto)
    if not m:
        return None
    h, minutos, sufijo = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if sufijo == "pm" and h < 12:
        h += 12
    elif sufijo == "am" and h == 12:
        h = 0
    if h > 23 or minutos > 59:
        return None
    return f"{h:02d}:{minutos:02d}"


def _validar_dia(fecha: str) -> date | dict:
    """Devuelve la fecha si se puede agendar ese día, o un dict con el error."""
    try:
        dia = datetime.strptime(fecha.strip(), "%Y-%m-%d").date()
    except ValueError:
        return {"error": f"Fecha '{fecha}' inválida. Usa el formato YYYY-MM-DD."}
    hoy = db.ahora().date()
    if dia < hoy:
        return {"error": f"La fecha {dia} ya pasó. Hoy es {hoy}."}
    if dia > hoy + timedelta(days=db.MAX_DIAS_ADELANTE):
        return {"error": f"Solo se agenda hasta {db.MAX_DIAS_ADELANTE} días adelante."}
    if dia.weekday() not in db.DIAS_ATENCION:
        return {"error": f"El {dia} es fin de semana. La clínica atiende de lunes a viernes."}
    if dia.isoformat() in db.DIAS_CERRADOS:
        return {"error": f"El {dia} la clínica está cerrada (festivo)."}
    return dia


def _horarios_libres(conn, dia: date, duracion_min: int) -> list[str]:
    ocupadas = [
        (datetime.strptime(o["inicio"], FORMATO), datetime.strptime(o["fin"], FORMATO))
        for o in conn.execute(
            "SELECT inicio, fin FROM citas WHERE estado = 'confirmada' AND inicio LIKE ?",
            (f"{dia.isoformat()}%",),
        )
    ]
    duracion = timedelta(minutes=duracion_min)
    ahora = db.ahora()
    hora = datetime.combine(dia, datetime.min.time()).replace(hour=db.HORA_APERTURA)
    cierre = hora.replace(hour=db.HORA_CIERRE)

    libres = []
    while hora + duracion <= cierre:
        choca = any(hora < fin and hora + duracion > inicio for inicio, fin in ocupadas)
        if not choca and hora > ahora:
            libres.append(hora.strftime("%H:%M"))
        hora += timedelta(minutes=30)
    return libres


# ---------- Funciones ----------

def listar_servicios() -> dict:
    with db.conectar() as conn:
        filas = conn.execute("SELECT * FROM servicios").fetchall()
    return {"servicios": [dict(f) for f in filas]}


def ver_disponibilidad(fecha: str, servicio_id: str) -> dict:
    dia = _validar_dia(fecha)
    if isinstance(dia, dict):
        return dia
    with db.conectar() as conn:
        servicio = conn.execute("SELECT * FROM servicios WHERE id = ?", (servicio_id,)).fetchone()
        if servicio is None:
            return {"error": f"No existe el servicio '{servicio_id}'."}
        libres = _horarios_libres(conn, dia, servicio["duracion_min"])
    respuesta = {"fecha": dia.isoformat(), "servicio": servicio["nombre"], "horarios_libres": libres}
    if not libres:
        respuesta["nota"] = "No quedan horarios ese día. Ofrece otro día."
    return respuesta


def agendar_cita(paciente: str, servicio_id: str, fecha: str, hora: str, *, telefono: str) -> dict:
    paciente = " ".join(paciente.split())
    if len(paciente) < 3 or len(paciente) > 80:
        return {"error": "Falta el nombre completo del paciente. Pídeselo."}
    hora_ok = normalizar_hora(hora)
    if hora_ok is None:
        return {"error": f"Hora '{hora}' inválida. Usa el formato HH:MM, ej. 15:30."}
    dia = _validar_dia(fecha)
    if isinstance(dia, dict):
        return dia

    inicio_txt = f"{dia.isoformat()} {hora_ok}"
    conn = db.conectar()
    conn.isolation_level = None  # manejamos la transacción a mano
    try:
        # BEGIN IMMEDIATE bloquea la escritura mientras revisamos y guardamos:
        # si dos pacientes piden el mismo horario al mismo tiempo, solo uno lo obtiene.
        conn.execute("BEGIN IMMEDIATE")
        servicio = conn.execute("SELECT * FROM servicios WHERE id = ?", (servicio_id,)).fetchone()
        if servicio is None:
            conn.execute("ROLLBACK")
            return {"error": f"No existe el servicio '{servicio_id}'."}

        # Si esta misma cita ya existe (por ejemplo, el mensaje se reintentó), no la duplicamos
        existente = conn.execute(
            "SELECT id FROM citas WHERE telefono = ? AND servicio_id = ? AND inicio = ? AND estado = 'confirmada'",
            (telefono, servicio_id, inicio_txt),
        ).fetchone()
        if existente:
            conn.execute("ROLLBACK")
            return {"ok": True, "cita_id": existente["id"], "paciente": paciente,
                    "servicio": servicio["nombre"], "inicio": inicio_txt, "nota": "Ya estaba agendada."}

        libres = _horarios_libres(conn, dia, servicio["duracion_min"])
        if hora_ok not in libres:
            conn.execute("ROLLBACK")
            return {"error": f"Las {hora_ok} del {dia} no está disponible.", "horarios_libres": libres}

        fin = datetime.strptime(inicio_txt, FORMATO) + timedelta(minutes=servicio["duracion_min"])
        cursor = conn.execute(
            "INSERT INTO citas (paciente, telefono, servicio_id, inicio, fin) VALUES (?, ?, ?, ?, ?)",
            (paciente, telefono, servicio_id, inicio_txt, fin.strftime(FORMATO)),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"ok": True, "cita_id": cursor.lastrowid, "paciente": paciente,
            "servicio": servicio["nombre"], "inicio": inicio_txt}


def buscar_citas(*, telefono: str) -> dict:
    with db.conectar() as conn:
        filas = conn.execute(
            """SELECT c.id, c.paciente, s.nombre AS servicio, c.inicio
               FROM citas c JOIN servicios s ON s.id = c.servicio_id
               WHERE c.telefono = ? AND c.estado = 'confirmada' AND c.inicio >= ?
               ORDER BY c.inicio""",
            (telefono, db.ahora().strftime(FORMATO)),
        ).fetchall()
    return {"citas": [dict(f) for f in filas]}


def cancelar_cita(cita_id: int, *, telefono: str) -> dict:
    try:
        cita_id = int(cita_id)
    except (TypeError, ValueError):
        return {"error": "Número de cita inválido. Usa buscar_citas para ver los números."}
    with db.conectar() as conn:
        # Solo se cancelan citas de este mismo teléfono
        cursor = conn.execute(
            "UPDATE citas SET estado = 'cancelada' WHERE id = ? AND telefono = ? AND estado = 'confirmada'",
            (cita_id, telefono),
        )
    if cursor.rowcount == 0:
        return {"error": "Este paciente no tiene una cita activa con ese número. Usa buscar_citas."}
    return {"ok": True, "cita_id": cita_id, "estado": "cancelada"}


FUNCIONES = {
    "listar_servicios": listar_servicios,
    "ver_disponibilidad": ver_disponibilidad,
    "agendar_cita": agendar_cita,
    "buscar_citas": buscar_citas,
    "cancelar_cita": cancelar_cita,
}
# Estas reciben el teléfono del paciente desde el sistema, nunca desde la IA
USAN_TELEFONO = {"agendar_cita", "buscar_citas", "cancelar_cita"}


# ---------- Definiciones para la IA ----------

def _tool(name: str, description: str, properties: dict) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,  # garantiza que Claude mande exactamente estos parámetros
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


_servicio = {
    "type": "string",
    "enum": [s[0] for s in db.SERVICIOS],
    "description": "Id del servicio.",
}
_fecha = {"type": "string", "description": "Fecha en formato YYYY-MM-DD."}

TOOLS = [
    _tool("listar_servicios", "Lista los servicios de la clínica con duración y precio.", {}),
    _tool(
        "ver_disponibilidad",
        "Devuelve los horarios libres de un día para un servicio. Úsala siempre antes de ofrecer horarios.",
        {"fecha": _fecha, "servicio_id": _servicio},
    ),
    _tool(
        "agendar_cita",
        "Agenda una cita para el paciente de esta conversación. Solo úsala cuando ya confirmó servicio, fecha, hora y su nombre.",
        {
            "paciente": {"type": "string", "description": "Nombre completo del paciente."},
            "servicio_id": _servicio,
            "fecha": _fecha,
            "hora": {"type": "string", "description": "Hora de inicio en formato HH:MM (24 horas), ej. 15:30."},
        },
    ),
    _tool("buscar_citas", "Busca las próximas citas confirmadas del paciente de esta conversación.", {}),
    _tool(
        "cancelar_cita",
        "Cancela una cita del paciente de esta conversación. Confirma con el paciente antes de usarla.",
        {"cita_id": {"type": "integer", "description": "Número de la cita (de buscar_citas)."}},
    ),
]
