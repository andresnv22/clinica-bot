"""Servidor web: expone el agente por HTTP y sirve la página del demo."""

from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from google.genai import errors as genai_errors
from pydantic import BaseModel

from . import db
from .agent import conversaciones, falta_clave, responder

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Clínica Bot")
db.inicializar()


class Mensaje(BaseModel):
    telefono: str
    texto: str


@app.get("/")
def inicio():
    return FileResponse(STATIC / "index.html")


@app.post("/chat")
def chat(msg: Mensaje):
    variable = falta_clave()
    if variable:
        raise HTTPException(401, f"Falta {variable} en el archivo .env")
    try:
        return responder(msg.telefono, msg.texto)
    except genai_errors.APIError as e:
        if e.code == 429:
            raise HTTPException(429, "Llegaste al límite del plan gratis de Gemini, espera un minuto.")
        raise HTTPException(502, f"Error de la API de Gemini: {e.message}")
    except httpx.HTTPError:
        raise HTTPException(503, "Falló la conexión con Gemini, intenta de nuevo.")
    except anthropic.AuthenticationError:
        raise HTTPException(401, "Falta la API key de Anthropic o no es válida (ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Demasiadas solicitudes, espera unos segundos.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Error de la API de Claude: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "No hay conexión con la API de Claude.")


@app.post("/reiniciar/{telefono}")
def reiniciar(telefono: str):
    conversaciones.pop(telefono, None)
    return {"ok": True}


@app.get("/citas")
def citas():
    """Vista de la recepción: todas las citas próximas."""
    with db.conectar() as conn:
        filas = conn.execute(
            """SELECT c.id, c.paciente, c.telefono, s.nombre AS servicio, c.inicio, c.estado
               FROM citas c JOIN servicios s ON s.id = c.servicio_id
               ORDER BY c.inicio"""
        ).fetchall()
    return [dict(f) for f in filas]
