"""Tests del webhook de WhatsApp Cloud API (sin red)."""

import hashlib
import hmac

from app import whatsapp


def test_valid_signature(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret")
    body = b'{"entry": []}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert whatsapp.valid_signature(body, signature)
    assert not whatsapp.valid_signature(body, "sha256=bad")
    assert not whatsapp.valid_signature(body, None)


def test_parse_incoming_text_and_interactive():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"id": "1", "from": "57300", "type": "text", "text": {"body": "hola"}},
                                {
                                    "id": "2",
                                    "from": "57300",
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "button_reply",
                                        "button_reply": {"title": "Sí"},
                                    },
                                },
                                {"id": "3", "from": "57300", "type": "image", "image": {}},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    assert [(m["id"], m["text"]) for m in whatsapp.parse_incoming(payload)] == [
        ("1", "hola"),
        ("2", "Sí"),
    ]
    assert [m["id"] for m in whatsapp.parse_unsupported(payload)] == ["3"]


def test_chunk_text_respects_limit():
    parts = whatsapp.chunk_text("x" * 9000)
    assert len(parts) >= 3
    assert all(len(part) <= whatsapp.MAX_WHATSAPP_CHARS for part in parts)
    assert "".join(parts) == "x" * 9000
