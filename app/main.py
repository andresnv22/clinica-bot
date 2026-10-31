"""Servidor web: expone el agente por HTTP y sirve la página del demo."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .agent import conversaciones, falta_clave, responder

RAIZ = Path(__file__).resolve().parent.parent
STATIC = RAIZ / "static"

# Los errores técnicos van a clinica.log (máximo 3 archivos de 1 MB), nunca al paciente
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(RAIZ / "clinica.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)

app = FastAPI(title="Clínica Bot")
db.inicializar()


class Mensaje(BaseModel):
    telefono: str = Field(min_length=5, max_length=30)
    texto: str = Field(max_length=5000)


@app.get("/")
def inicio():
    return FileResponse(STATIC / "index.html")


@app.post("/chat")
def chat(msg: Mensaje):
    # Esto es un error de configuración del dueño, no del paciente
    variable = falta_clave()
    if variable:
        raise HTTPException(500, f"Falta {variable} en el archivo .env")
    # responder() nunca lanza errores: si la IA falla, devuelve un mensaje amable
    return responder(msg.telefono.strip(), msg.texto)


@app.post("/reiniciar/{telefono}")
def reiniciar(telefono: str):
    conversaciones.pop(telefono, None)
    return {"ok": True}


@app.get("/citas")
def citas():
    """Vista de la recepción: todas las citas. (En producción esto necesita contraseña.)"""
    with db.conectar() as conn:
        filas = conn.execute(
            """SELECT c.id, c.paciente, c.telefono, s.nombre AS servicio, c.inicio, c.estado
               FROM citas c JOIN servicios s ON s.id = c.servicio_id
               ORDER BY c.inicio"""
        ).fetchall()
    return [dict(f) for f in filas]
