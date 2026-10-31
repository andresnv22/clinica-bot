"""Herramientas que el agente puede usar.

Cada herramienta tiene dos partes:
  1. La definición (TOOLS): lo que Claude ve, nombre, descripción y parámetros.
  2. La función de Python: lo que realmente se ejecuta cuando Claude la pide.

Claude nunca toca la base de datos directo. Solo pide "ejecuta agendar_cita con
estos datos" y nuestro código decide si se puede o no. Así el bot no puede
inventar horarios ni agendar dos citas a la misma hora.
"""

from datetime import date, datetime, timedelta

from . import db

FORMATO = "%Y-%m-%d %H:%M"


# ---------- Funciones ----------

def listar_servicios() -> dict:
    with db.conectar() as conn:
        filas = conn.execute("SELECT * FROM servicios").fetchall()
    return {"servicios": [dict(f) for f in filas]}


def ver_disponibilidad(fecha: str, servicio_id: str) -> dict:
    try:
        dia = date.fromisoformat(fecha)
    except ValueError:
        return {"error": "Fecha inválida, usa el formato YYYY-MM-DD."}
    if dia < date.today():
        return {"error": "Esa fecha ya pasó."}
    if dia.weekday() not in db.DIAS_ATENCION:
        return {"error": "La clínica atiende de lunes a viernes."}

    with db.conectar() as conn:
        servicio = conn.execute(
            "SELECT * FROM servicios WHERE id = ?", (servicio_id,)
        ).fetchone()
        if servicio is None:
            return {"error": f"No existe el servicio '{servicio_id}'."}
        ocupadas = conn.execute(
            "SELECT inicio, fin FROM citas WHERE estado = 'confirmada' AND inicio LIKE ?",
            (f"{fecha}%",),
        ).fetchall()

    duracion = timedelta(minutes=servicio["duracion_min"])
    ocupadas = [
        (datetime.strptime(o["inicio"], FORMATO), datetime.strptime(o["fin"], FORMATO))
        for o in ocupadas
    ]
    ahora = datetime.now()
    hora = datetime.combine(dia, datetime.min.time()).replace(hour=db.HORA_APERTURA)
    cierre = hora.replace(hour=db.HORA_CIERRE)

    libres = []
    while hora + duracion <= cierre:
        choca = any(hora < fin and hora + duracion > inicio for inicio, fin in ocupadas)
        if not choca and hora > ahora:
            libres.append(hora.strftime("%H:%M"))
        hora += timedelta(minutes=30)
    return {"fecha": fecha, "servicio": servicio["nombre"], "horarios_libres": libres}


def agendar_cita(paciente: str, telefono: str, servicio_id: str, fecha: str, hora: str) -> dict:
    disponibilidad = ver_disponibilidad(fecha, servicio_id)
    if "error" in disponibilidad:
        return disponibilidad
    if hora not in disponibilidad["horarios_libres"]:
        return {
            "error": f"Las {hora} no está disponible.",
            "horarios_libres": disponibilidad["horarios_libres"],
        }

    with db.conectar() as conn:
        duracion = conn.execute(
            "SELECT duracion_min FROM servicios WHERE id = ?", (servicio_id,)
        ).fetchone()["duracion_min"]
        inicio = datetime.strptime(f"{fecha} {hora}", FORMATO)
        fin = inicio + timedelta(minutes=duracion)
        cursor = conn.execute(
            "INSERT INTO citas (paciente, telefono, servicio_id, inicio, fin) VALUES (?, ?, ?, ?, ?)",
            (paciente, telefono, servicio_id, inicio.strftime(FORMATO), fin.strftime(FORMATO)),
        )
    return {
        "ok": True,
        "cita_id": cursor.lastrowid,
        "paciente": paciente,
        "servicio": disponibilidad["servicio"],
        "inicio": inicio.strftime(FORMATO),
    }


def buscar_citas(telefono: str) -> dict:
    with db.conectar() as conn:
        filas = conn.execute(
            """SELECT c.id, c.paciente, s.nombre AS servicio, c.inicio
               FROM citas c JOIN servicios s ON s.id = c.servicio_id
               WHERE c.telefono = ? AND c.estado = 'confirmada' AND c.inicio >= ?
               ORDER BY c.inicio""",
            (telefono, datetime.now().strftime(FORMATO)),
        ).fetchall()
    return {"citas": [dict(f) for f in filas]}


def cancelar_cita(cita_id: int, telefono: str) -> dict:
    with db.conectar() as conn:
        # Se pide el teléfono para que nadie cancele citas ajenas adivinando el número
        cursor = conn.execute(
            "UPDATE citas SET estado = 'cancelada' WHERE id = ? AND telefono = ? AND estado = 'confirmada'",
            (cita_id, telefono),
        )
    if cursor.rowcount == 0:
        return {"error": "No encontré una cita activa con ese número para este teléfono."}
    return {"ok": True, "cita_id": cita_id, "estado": "cancelada"}


FUNCIONES = {
    "listar_servicios": listar_servicios,
    "ver_disponibilidad": ver_disponibilidad,
    "agendar_cita": agendar_cita,
    "buscar_citas": buscar_citas,
    "cancelar_cita": cancelar_cita,
}


# ---------- Definiciones para Claude ----------

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
        "Agenda una cita. Solo úsala cuando el paciente ya confirmó servicio, fecha, hora y su nombre.",
        {
            "paciente": {"type": "string", "description": "Nombre completo del paciente."},
            "telefono": {"type": "string", "description": "Teléfono del paciente."},
            "servicio_id": _servicio,
            "fecha": _fecha,
            "hora": {"type": "string", "description": "Hora de inicio en formato HH:MM, ej. 10:30."},
        },
    ),
    _tool(
        "buscar_citas",
        "Busca las próximas citas confirmadas de un paciente por su teléfono.",
        {"telefono": {"type": "string", "description": "Teléfono del paciente."}},
    ),
    _tool(
        "cancelar_cita",
        "Cancela una cita. Confirma con el paciente antes de usarla.",
        {
            "cita_id": {"type": "integer", "description": "Número de la cita."},
            "telefono": {"type": "string", "description": "Teléfono del paciente."},
        },
    ),
]
