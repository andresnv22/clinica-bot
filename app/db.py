"""Base de datos de la clínica (SQLite). Guarda servicios, citas y la configuración del negocio."""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().parent.parent / "clinica.db"

SERVICIOS = [
    ("consulta", "Consulta general", 30, 25),
    ("limpieza", "Limpieza dental", 30, 40),
    ("blanqueamiento", "Blanqueamiento", 60, 120),
    ("urgencia", "Urgencia (dolor)", 30, 50),
]

# Horario de atención: lunes a viernes, 9:00 a 18:00
HORA_APERTURA = 9
HORA_CIERRE = 18
DIAS_ATENCION = {0, 1, 2, 3, 4}  # 0 = lunes
# Festivos o días en que la clínica no abre, en formato YYYY-MM-DD
DIAS_CERRADOS: set[str] = set()
MAX_DIAS_ADELANTE = 90  # no se agenda más allá de esto

# La hora del negocio, no la del servidor: si se aloja en otro país, las
# citas "de hoy" y "las horas que ya pasaron" siguen siendo las de Colombia.
ZONA_HORARIA = ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Bogota"))


def ahora() -> datetime:
    """Fecha y hora actual en la zona del negocio (sin tzinfo, para comparar con la BD)."""
    return datetime.now(ZONA_HORARIA).replace(tzinfo=None)


def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def inicializar() -> None:
    with conectar() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS servicios (
                id TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                duracion_min INTEGER NOT NULL,
                precio_usd INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS citas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paciente TEXT NOT NULL,
                telefono TEXT NOT NULL,
                servicio_id TEXT NOT NULL REFERENCES servicios(id),
                inicio TEXT NOT NULL,  -- 'YYYY-MM-DD HH:MM'
                fin TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'confirmada',  -- confirmada | cancelada
                creada TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            """
        )
        conn.executemany(
            "INSERT OR IGNORE INTO servicios VALUES (?, ?, ?, ?)", SERVICIOS
        )
