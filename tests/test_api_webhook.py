"""Pruebas de API HTTP y webhook de WhatsApp Cloud API para clinica-bot."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "clinica_test.db")
    db.inicializar()
    return TestClient(main.app)


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_verificacion(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")
    r = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "mi_token_secreto",
        "hub.challenge": "reto123",
    })
    assert r.status_code == 200
    assert r.text == "reto123"

    r_bad = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "otro",
        "hub.challenge": "reto123",
    })
    assert r_bad.status_code == 403


def test_webhook_rechaza_sin_firma_o_firma_invalida(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secreto_meta")
    body = json.dumps({"entry": []}).encode()
    r = client.post("/webhook", content=body)
    assert r.status_code == 403

    r2 = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=invalido"})
    assert r2.status_code == 403


def test_webhook_deduplica_mensajes_y_procesa_en_background(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secreto_meta")
    procesados = []
    monkeypatch.setattr(main, "_procesar_whatsapp", lambda p, t, mid: procesados.append((p, t, mid)))

    payload = {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "id": "wamid.abc123",
                        "from": "573001112233",
                        "type": "text",
                        "text": {"body": "Hola quiero cita"},
                    }]
                }
            }]
        }]
    }
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body, "secreto_meta")}

    r1 = client.post("/webhook", content=body, headers=headers)
    r2 = client.post("/webhook", content=body, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert procesados == [("573001112233", "Hola quiero cita", "wamid.abc123")]


def test_citas_protegida_cuando_hay_admin_token(client, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert client.get("/citas").status_code == 200

    monkeypatch.setenv("ADMIN_TOKEN", "clave_admin")
    assert client.get("/citas").status_code == 401
    assert client.get("/citas", headers={"Authorization": "Bearer clave_admin"}).status_code == 200


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
