"""Servidor web: expone el webhook de WhatsApp Cloud API, el chat web y las citas."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

load_dotenv()

from . import db, whatsapp  # noqa: E402
from .agent import conversaciones, falta_clave, responder  # noqa: E402

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


@app.get("/health")
def health():
    return {"ok": True, "whatsapp_configured": whatsapp.configured()}


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


UNSUPPORTED_WA_MSG = (
    "Por ahora solo puedo leer mensajes de texto. "
    "¿Me escribes por texto qué servicio necesitas y para qué día?"
)


def _procesar_whatsapp(phone: str, text: str, message_id: str) -> None:
    whatsapp.mark_read(message_id)
    resultado = responder(phone, text)
    whatsapp.send_text(phone, resultado["respuesta"])


def _responder_no_soportado(phone: str, message_id: str) -> None:
    whatsapp.mark_read(message_id)
    whatsapp.send_text(phone, UNSUPPORTED_WA_MSG)


@app.get("/webhook")
def verificar_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
):
    """Verificación única de Meta al registrar el webhook."""
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge)
    raise HTTPException(403, "Verificación fallida")


@app.post("/webhook")
async def recibir_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if not whatsapp.valid_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(403, "Firma inválida")

    payload = await request.json()
    for msg in whatsapp.parse_incoming(payload):
        if not msg["phone"] or whatsapp.is_duplicate(msg["id"]):
            continue
        background.add_task(_procesar_whatsapp, msg["phone"], msg["text"], msg["id"])
    for msg in whatsapp.parse_unsupported(payload):
        if not msg["phone"] or whatsapp.is_duplicate(msg["id"]):
            continue
        background.add_task(_responder_no_soportado, msg["phone"], msg["id"])
    return {"status": "ok"}


@app.get("/citas")
def citas(authorization: str | None = Header(default=None)):
    """Vista de la recepción: si ADMIN_TOKEN está definido en .env, exige Bearer token."""
    expected = os.environ.get("ADMIN_TOKEN", "")
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(401, "No autorizado")
    with db.conectar() as conn:
        filas = conn.execute(
            """SELECT c.id, c.paciente, c.telefono, s.nombre AS servicio, c.inicio, c.estado
               FROM citas c JOIN servicios s ON s.id = c.servicio_id
               ORDER BY c.inicio"""
        ).fetchall()
    return [dict(f) for f in filas]
