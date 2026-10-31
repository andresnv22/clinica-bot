"""El agente: conversa con el paciente y usa las herramientas.

Funciona en un ciclo (el "agentic loop"):
  1. Le mandamos al modelo la conversación + las herramientas disponibles.
  2. Si responde con texto, terminamos y se lo mostramos al paciente.
  3. Si pide usar una herramienta, la ejecutamos, le devolvemos el
     resultado y volvemos al paso 1.

El mismo ciclo está escrito dos veces: una para Claude y otra para Gemini.
Cada proveedor tiene su propio formato de mensajes, pero la idea es igual.
Se elige con PROVEEDOR en el archivo .env.

Regla de oro: el paciente nunca ve un error técnico. Si algo falla, recibe un
mensaje amable, la conversación vuelve a como estaba y el detalle queda en el log.
"""

import json
import logging
import os
import re
import threading
import time

from . import db
from .tools import FUNCIONES, TOOLS, USAN_TELEFONO

log = logging.getLogger("clinica")
PROVEEDOR = os.environ.get("PROVEEDOR", "claude").lower()

MODELO_CLAUDE = "claude-opus-5-5"
MODELO_GEMINI = os.environ.get("MODELO_GEMINI", "gemini-3.5-flash")
# Si un modelo está saturado, se prueba el siguiente de la lista
# (Los modelos "lite" quedan fuera: en pruebas inventaron precios sin consultar las herramientas)
MODELOS_GEMINI = list(dict.fromkeys([MODELO_GEMINI, "gemini-3.5-flash"]))
MAX_PASOS = 10  # tope de seguridad para que el ciclo no sea infinito
INACTIVIDAD = 6 * 60 * 60  # segundos sin mensajes para empezar una conversación nueva
MAX_LARGO_MENSAJE = 2000
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

MENSAJE_FALLA = "Uy, tuve un problema técnico y no alcancé a procesar tu mensaje 🙏 ¿Me lo escribes de nuevo en un minuto?"
MENSAJE_RECHAZO = "Disculpa, en eso no te puedo ayudar. ¿Te ayudo con una cita?"
MENSAJE_ENREDO = "Perdón, me enredé. ¿Me lo repites con otras palabras?"

SYSTEM = """Eres Sofía, la asistente virtual de Clínica Dental Sonrisa. Atiendes por WhatsApp.

Tu trabajo: agendar, consultar y cancelar citas, y responder dudas sobre servicios y precios.

Cómo trabajas:
- Escribe corto y amable, como en WhatsApp. Nada de listas largas.
- Usa el formato de WhatsApp: negrita con un solo asterisco (*así*), nunca con dos (**así**), ni títulos con #.
- Nunca inventes horarios ni precios: consulta ver_disponibilidad o listar_servicios antes de darlos. Los precios están en dólares (USD).
- Antes de agendar necesitas: servicio, fecha, hora y nombre completo.
- El teléfono del paciente ya lo tiene el sistema: no se lo pidas.
- Antes de cancelar, confirma con el paciente cuál cita.
- Si una herramienta devuelve un error, explícalo con palabras simples y ofrece una alternativa. Nunca muestres mensajes técnicos.
- Si hay dolor fuerte, sangrado o hinchazón, ofrece el servicio de urgencia para lo antes posible.
- No das diagnósticos ni consejos médicos; para eso está el dentista en la cita.
- Si te preguntan algo que no tiene que ver con la clínica, redirige con amabilidad.

Horario: lunes a viernes de 9:00 a 18:00."""


class Rechazado(Exception):
    """El modelo se negó a responder (filtro de seguridad del proveedor)."""


# Conversaciones en memoria: teléfono -> {"mensajes": [...], "ultimo": timestamp}.
# Para producción esto iría en la base de datos.
conversaciones: dict[str, dict] = {}
_candados: dict[str, threading.Lock] = {}
_candados_lock = threading.Lock()


def _candado(telefono: str) -> threading.Lock:
    """Un candado por paciente: si manda dos mensajes seguidos, se atienden en orden."""
    with _candados_lock:
        return _candados.setdefault(telefono, threading.Lock())


def falta_clave() -> str | None:
    """Devuelve el nombre de la variable que falta en .env, o None si está todo."""
    variable = "GEMINI_API_KEY" if PROVEEDOR == "gemini" else "ANTHROPIC_API_KEY"
    valor = os.environ.get(variable, "")
    return variable if not valor or valor.startswith("pega-tu") else None


def _system() -> str:
    ahora = db.ahora()
    return f"{SYSTEM}\n\nHoy es {DIAS[ahora.weekday()]} {ahora.date().isoformat()}, son las {ahora:%H:%M}."


def ejecutar_herramienta(nombre: str, entrada: dict, telefono: str) -> tuple[dict, bool]:
    funcion = FUNCIONES.get(nombre)
    if funcion is None:
        return {"error": f"No existe la herramienta '{nombre}'."}, True
    entrada = {k: v for k, v in entrada.items() if k != "telefono"}  # el teléfono lo pone el sistema
    if nombre in USAN_TELEFONO:
        entrada["telefono"] = telefono
    try:
        resultado = funcion(**entrada)
    except TypeError as e:  # parámetros que no corresponden
        return {"error": f"Parámetros inválidos: {e}"}, True
    except Exception:  # un error en una herramienta no debe tumbar al bot
        log.exception("Falló la herramienta %s con %s", nombre, entrada)
        return {"error": "Error interno al ejecutar la herramienta. Intenta de nuevo."}, True
    return resultado, "error" in resultado


def formato_whatsapp(texto: str) -> str:
    """Asegura el formato de WhatsApp aunque la IA se equivoque: **negrita** -> *negrita*, sin títulos #."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.MULTILINE)
    return texto.strip()


def _texto_final(texto: str, acciones: list) -> str:
    if texto.strip():
        return formato_whatsapp(texto)
    # El modelo terminó sin escribir nada: respondemos algo coherente con lo que pasó
    hizo_algo = any(not a["error"] for a in acciones)
    return "Listo 👍 ¿Te ayudo con algo más?" if hizo_algo else MENSAJE_ENREDO


def responder(telefono: str, texto: str) -> dict:
    """Recibe un mensaje del paciente y devuelve la respuesta del bot. Nunca lanza errores."""
    texto = texto.strip()[:MAX_LARGO_MENSAJE]
    if not texto:
        return {"respuesta": "¡Hola! ¿En qué te puedo ayudar? 😊", "acciones": []}

    with _candado(telefono):
        conv = conversaciones.get(telefono)
        if conv is None or time.time() - conv["ultimo"] > INACTIVIDAD:
            conv = conversaciones[telefono] = {"mensajes": [], "ultimo": time.time()}
        mensajes = conv["mensajes"]
        antes = len(mensajes)  # para deshacer este turno si algo falla
        acciones: list = []

        try:
            responder_con = _responder_gemini if PROVEEDOR == "gemini" else _responder_claude
            respuesta = responder_con(texto, mensajes, acciones, telefono)
        except Rechazado:
            del mensajes[antes:]
            return {"respuesta": MENSAJE_RECHAZO, "acciones": acciones}
        except Exception:
            del mensajes[antes:]
            log.exception("Falló la respuesta para %s", telefono)
            return {"respuesta": MENSAJE_FALLA, "acciones": acciones, "error": True}

        conv["ultimo"] = time.time()
        if respuesta is None:  # se agotaron los pasos sin terminar
            del mensajes[antes:]
            return {"respuesta": MENSAJE_ENREDO, "acciones": acciones}
        return {"respuesta": _texto_final(respuesta, acciones), "acciones": acciones}


# ---------- Claude ----------

_claude = None


def _responder_claude(texto: str, mensajes: list, acciones: list, telefono: str) -> str | None:
    import anthropic

    global _claude
    _claude = _claude or anthropic.Anthropic(timeout=60.0)  # un chat no puede esperar 10 minutos

    mensajes.append({"role": "user", "content": texto})
    for _ in range(MAX_PASOS):
        respuesta = _claude.beta.messages.create(
            model=MODELO_CLAUDE,
            max_tokens=16000,
            system=_system(),
            tools=TOOLS,
            messages=mensajes,
            output_config={"effort": "low"},  # chat simple: rápido y barato
            # Si el modelo rechaza un mensaje, la API reintenta sola con otro modelo
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if respuesta.stop_reason == "refusal":
            raise Rechazado()
        # Guardamos la respuesta completa (no solo el texto) para mantener el contexto
        mensajes.append({"role": "assistant", "content": respuesta.content})

        if respuesta.stop_reason != "tool_use":
            return "".join(b.text for b in respuesta.content if b.type == "text")

        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            salida, es_error = ejecutar_herramienta(bloque.name, bloque.input, telefono)
            acciones.append({"herramienta": bloque.name, "entrada": bloque.input, "error": es_error})
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,
                "content": json.dumps(salida, ensure_ascii=False),
                "is_error": es_error,
            })
        # Todos los resultados van juntos en un solo mensaje
        mensajes.append({"role": "user", "content": resultados})
    return None


# ---------- Gemini ----------

_gemini = None
ESPERA_SI_FALLA = 5 * 60  # segundos sin usar un modelo después de que falla
_caido_hasta: dict[str, float] = {}  # modelo -> hasta cuándo lo saltamos


def _generar_gemini(mensajes, config):
    """Prueba los modelos en orden; si uno está saturado o tarda mucho, pasa al siguiente."""
    import httpx
    from google.genai import errors, types

    def con_tiempo(segundos: int, intentos: int):
        return config.model_copy(update={"http_options": types.HttpOptions(
            timeout=segundos * 1000, retry_options=types.HttpRetryOptions(attempts=intentos),
        )})

    # A los primeros les damos poco tiempo y ningún reintento: si están saturados,
    # es mejor pasar rápido al siguiente que dejar al paciente esperando.
    impaciente, paciente = con_tiempo(15, 1), con_tiempo(40, 2)
    disponibles = [m for m in MODELOS_GEMINI if time.time() >= _caido_hasta.get(m, 0)]
    disponibles = disponibles or MODELOS_GEMINI[-1:]  # si todos fallaron, igual intentamos el último

    for i, modelo in enumerate(disponibles):
        ultimo = i == len(disponibles) - 1
        try:
            return _gemini.models.generate_content(
                model=modelo, contents=mensajes, config=paciente if ultimo else impaciente
            )
        except (errors.ServerError, errors.ClientError, httpx.TimeoutException) as e:
            # 5xx o tiempo agotado = saturado; 429 = se acabó la cuota de ese modelo
            if isinstance(e, errors.ClientError) and e.code != 429:
                raise
            log.warning("Gemini %s no disponible (%s), probando el siguiente", modelo, e)
            _caido_hasta[modelo] = time.time() + ESPERA_SI_FALLA
            if ultimo:
                raise


def _responder_gemini(texto: str, mensajes: list, acciones: list, telefono: str) -> str | None:
    from google import genai
    from google.genai import types

    global _gemini
    _gemini = _gemini or genai.Client()  # lee GEMINI_API_KEY

    config = types.GenerateContentConfig(
        system_instruction=_system(),
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters_json_schema=t["input_schema"],
            )
            for t in TOOLS
        ])],
        # Nosotros manejamos el ciclo, igual que con Claude
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(thinking_level="low"),  # chat simple: rápido
    )

    mensajes.append(types.Content(role="user", parts=[types.Part.from_text(text=texto)]))
    for _ in range(MAX_PASOS):
        respuesta = _generar_gemini(mensajes, config)
        if not respuesta.candidates or respuesta.candidates[0].content is None:
            raise Rechazado()  # bloqueado por el filtro de seguridad

        # Igual que con Claude: guardamos la respuesta completa para mantener el contexto
        contenido = respuesta.candidates[0].content
        mensajes.append(contenido)

        if not respuesta.function_calls:
            return "".join(p.text for p in contenido.parts or [] if p.text and not p.thought)

        partes = []
        for llamada in respuesta.function_calls:
            entrada = dict(llamada.args or {})
            salida, es_error = ejecutar_herramienta(llamada.name, entrada, telefono)
            acciones.append({"herramienta": llamada.name, "entrada": entrada, "error": es_error})
            partes.append(types.Part.from_function_response(name=llamada.name, response=salida))
        mensajes.append(types.Content(role="user", parts=partes))
    return None
