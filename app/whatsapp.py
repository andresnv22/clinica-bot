"""Integración con WhatsApp Cloud API (Meta): verificación HMAC, parsing y envío."""

import hashlib
import hmac
import logging
import os

import httpx

from . import db

log = logging.getLogger("clinica-bot.whatsapp")

GRAPH_VERSION = "v21.0"
MAX_WHATSAPP_CHARS = 4096
SEEN_TTL = 10 * 60


def _token() -> str:
    return os.environ.get("WHATSAPP_TOKEN", "")


def _phone_number_id() -> str:
    return os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")


def configured() -> bool:
    """True si están configuradas las credenciales de WhatsApp Cloud API."""
    return bool(_token() and _phone_number_id())


def valid_signature(body: bytes, header: str | None) -> bool:
    """Valida la firma X-Hub-Signature-256 enviada por Meta."""
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        log.error("WHATSAPP_APP_SECRET no configurado: rechazando webhook")
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def is_duplicate(message_id: str) -> bool:
    return db.claim_message(message_id, SEEN_TTL)


def _extract_text(msg: dict) -> str:
    msg_type = msg.get("type")
    if msg_type == "text":
        return msg.get("text", {}).get("body", "")
    if msg_type == "interactive":
        inter = msg.get("interactive", {})
        itype = inter.get("type")
        if itype == "button_reply":
            return inter.get("button_reply", {}).get("title", "")
        if itype == "list_reply":
            return inter.get("list_reply", {}).get("title", "")
    if msg_type == "button":
        return msg.get("button", {}).get("text", "")
    return ""


def parse_incoming(payload: dict) -> list[dict]:
    """Extrae mensajes de texto o respuestas interactivas del webhook de Meta."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                text = _extract_text(msg)
                if not text:
                    continue
                out.append({
                    "id": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "text": text,
                })
    return out


def parse_unsupported(payload: dict) -> list[dict]:
    """Detecta audios, imágenes o documentos para responder amablemente."""
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if _extract_text(msg):
                    continue
                msg_type = msg.get("type", "unknown")
                if msg_type in {"text", "interactive", "button"}:
                    continue
                out.append({
                    "id": msg.get("id", ""),
                    "phone": msg.get("from", ""),
                    "type": msg_type,
                })
    return out


def chunk_text(text: str, limit: int = MAX_WHATSAPP_CHARS) -> list[str]:
    """Divide respuestas largas respetando el límite de 4096 caracteres de WhatsApp."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [c for c in chunks if c]


def send_text(phone: str, text: str) -> bool:
    """Envía un mensaje de texto por WhatsApp Cloud API."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"
    chunks = chunk_text(text)
    if not chunks:
        return True
    for part in chunks:
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {_token()}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "text",
                    "text": {"body": part},
                },
                timeout=15,
            )
            if r.status_code >= 400:
                log.error("Error enviando WhatsApp (%s): %s", r.status_code, r.text[:500])
                return False
        except httpx.HTTPError as e:
            log.error("Error de red enviando WhatsApp: %s", e)
            return False
    return True


def mark_read(message_id: str) -> None:
    """Marca el mensaje como leído (doble check azul)."""
    if not message_id:
        return
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"
    try:
        httpx.post(
            url,
            headers={"Authorization": f"Bearer {_token()}"},
            json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
            timeout=10,
        )
    except httpx.HTTPError:
        pass
