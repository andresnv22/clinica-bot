"""El agente: conversa con el paciente y usa las herramientas.

Funciona en un ciclo (el "agentic loop"):
  1. Le mandamos al modelo la conversación + las herramientas disponibles.
  2. Si responde con texto, terminamos y se lo mostramos al paciente.
  3. Si pide usar una herramienta, la ejecutamos, le devolvemos el
     resultado y volvemos al paso 1.

El mismo ciclo está escrito dos veces: una para Claude y otra para Gemini.
Cada proveedor tiene su propio formato de mensajes, pero la idea es igual.
Se elige con PROVEEDOR en el archivo .env.
"""

import json
import os
import time
from datetime import date

from dotenv import load_dotenv

from .tools import FUNCIONES, TOOLS

load_dotenv()  # carga las claves desde el archivo .env
PROVEEDOR = os.environ.get("PROVEEDOR", "claude").lower()

MODELO_CLAUDE = "claude-opus-5-5"
MODELO_GEMINI = os.environ.get("MODELO_GEMINI", "gemini-3.5-flash")
# Si un modelo está saturado, se prueba el siguiente de la lista
# (Los modelos "lite" quedan fuera: en pruebas inventaron precios sin consultar las herramientas)
MODELOS_GEMINI = list(dict.fromkeys([MODELO_GEMINI, "gemini-3.5-flash"]))
MAX_PASOS = 10  # tope de seguridad para que el ciclo no sea infinito
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

SYSTEM = """Eres Sofía, la asistente virtual de Clínica Dental Sonrisa. Atiendes por WhatsApp.

Tu trabajo: agendar, consultar y cancelar citas, y responder dudas sobre servicios y precios.

Cómo trabajas:
- Escribe corto y amable, como en WhatsApp. Nada de listas largas.
- Usa el formato de WhatsApp: negrita con un solo asterisco (*así*), nunca con dos (**así**), ni títulos con #.
- Nunca inventes horarios ni precios: consulta ver_disponibilidad o listar_servicios antes de darlos.
- Antes de agendar necesitas: servicio, fecha, hora y nombre completo. El teléfono ya lo tienes.
- Antes de cancelar, confirma con el paciente cuál cita.
- Si hay dolor fuerte, sangrado o hinchazón, ofrece el servicio de urgencia para lo antes posible.
- No das diagnósticos ni consejos médicos; para eso está el dentista en la cita.
- Si te preguntan algo que no tiene que ver con la clínica, redirige con amabilidad.

Horario: lunes a viernes de 9:00 a 18:00."""

# Conversaciones en memoria: teléfono -> lista de mensajes.
# Para producción esto iría en la base de datos.
conversaciones: dict[str, list] = {}


def falta_clave() -> str | None:
    """Devuelve el nombre de la variable que falta en .env, o None si está todo."""
    variable = "GEMINI_API_KEY" if PROVEEDOR == "gemini" else "ANTHROPIC_API_KEY"
    valor = os.environ.get(variable, "")
    return variable if not valor or valor.startswith("pega-tu") else None


def _system(telefono: str) -> str:
    hoy = date.today()
    return (
        f"{SYSTEM}\n\nHoy es {DIAS[hoy.weekday()]} {hoy.isoformat()}. "
        f"Teléfono del paciente: {telefono}."
    )


def ejecutar_herramienta(nombre: str, entrada: dict) -> tuple[dict, bool]:
    try:
        resultado = FUNCIONES[nombre](**entrada)
    except Exception as e:  # un error en una herramienta no debe tumbar al bot
        return {"error": f"Error interno: {e}"}, True
    return resultado, "error" in resultado


def responder(telefono: str, texto: str) -> dict:
    """Recibe un mensaje del paciente y devuelve la respuesta del bot."""
    if PROVEEDOR == "gemini":
        return _responder_gemini(telefono, texto)
    return _responder_claude(telefono, texto)


# ---------- Claude ----------

_claude = None


def _responder_claude(telefono: str, texto: str) -> dict:
    import anthropic

    global _claude
    _claude = _claude or anthropic.Anthropic()

    mensajes = conversaciones.setdefault(telefono, [])
    mensajes.append({"role": "user", "content": texto})
    acciones = []  # para mostrar en el demo qué herramientas usó

    for _ in range(MAX_PASOS):
        respuesta = _claude.beta.messages.create(
            model=MODELO_CLAUDE,
            max_tokens=16000,
            system=_system(telefono),
            tools=TOOLS,
            messages=mensajes,
            output_config={"effort": "low"},  # chat simple: rápido y barato
            # Si el modelo rechaza un mensaje, la API reintenta sola con otro modelo
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        # Guardamos la respuesta completa (no solo el texto) para mantener el contexto
        mensajes.append({"role": "assistant", "content": respuesta.content})

        if respuesta.stop_reason == "refusal":
            return {"respuesta": "Disculpa, no puedo ayudarte con eso. ¿Te ayudo con una cita?", "acciones": acciones}

        if respuesta.stop_reason != "tool_use":
            texto_final = "".join(b.text for b in respuesta.content if b.type == "text")
            return {"respuesta": texto_final, "acciones": acciones}

        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            salida, es_error = ejecutar_herramienta(bloque.name, bloque.input)
            acciones.append({"herramienta": bloque.name, "entrada": bloque.input, "error": es_error})
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,
                "content": json.dumps(salida, ensure_ascii=False),
                "is_error": es_error,
            })
        # Todos los resultados van juntos en un solo mensaje
        mensajes.append({"role": "user", "content": resultados})

    return {"respuesta": "Perdón, me enredé. ¿Me lo repites?", "acciones": acciones}


# ---------- Gemini ----------

_gemini = None
ESPERA_SI_FALLA = 5 * 60  # segundos sin usar un modelo después de que falla
_caido_hasta: dict[str, float] = {}  # modelo -> hasta cuándo lo saltamos


def _generar_gemini(mensajes, config):
    """Prueba los modelos en orden; si uno está saturado o tarda mucho, pasa al siguiente."""
    import httpx
    from google.genai import errors, types

    # Poco tiempo y ningún reintento: si un modelo está saturado,
    # es mejor pasar rápido al siguiente que dejar al paciente esperando.
    impaciente = config.model_copy(update={"http_options": types.HttpOptions(
        timeout=15_000,  # milisegundos
        retry_options=types.HttpRetryOptions(attempts=1),
    )})
    disponibles = [m for m in MODELOS_GEMINI if time.time() >= _caido_hasta.get(m, 0)]
    disponibles = disponibles or MODELOS_GEMINI[-1:]  # si todos fallaron, igual intentamos el último

    for i, modelo in enumerate(disponibles):
        ultimo = i == len(disponibles) - 1
        try:
            # El último usa la configuración normal, con reintentos
            return _gemini.models.generate_content(
                model=modelo, contents=mensajes, config=config if ultimo else impaciente
            )
        except (errors.ServerError, errors.ClientError, httpx.TimeoutException) as e:
            # 5xx o tiempo agotado = saturado; 429 = se acabó la cuota de ese modelo
            if isinstance(e, errors.ClientError) and e.code != 429:
                raise
            _caido_hasta[modelo] = time.time() + ESPERA_SI_FALLA
            if ultimo:
                raise


def _responder_gemini(telefono: str, texto: str) -> dict:
    from google import genai
    from google.genai import types

    global _gemini
    _gemini = _gemini or genai.Client()  # lee GEMINI_API_KEY

    config = types.GenerateContentConfig(
        system_instruction=_system(telefono),
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

    mensajes = conversaciones.setdefault(telefono, [])
    mensajes.append(types.Content(role="user", parts=[types.Part.from_text(text=texto)]))
    acciones = []

    for _ in range(MAX_PASOS):
        respuesta = _generar_gemini(mensajes, config)
        if not respuesta.candidates or respuesta.candidates[0].content is None:
            mensajes.pop()  # el mensaje fue bloqueado; no lo dejamos en el historial
            return {"respuesta": "Disculpa, no puedo ayudarte con eso. ¿Te ayudo con una cita?", "acciones": acciones}

        # Igual que con Claude: guardamos la respuesta completa para mantener el contexto
        mensajes.append(respuesta.candidates[0].content)

        if not respuesta.function_calls:
            return {"respuesta": respuesta.text or "", "acciones": acciones}

        partes = []
        for llamada in respuesta.function_calls:
            entrada = dict(llamada.args or {})
            salida, es_error = ejecutar_herramienta(llamada.name, entrada)
            acciones.append({"herramienta": llamada.name, "entrada": entrada, "error": es_error})
            partes.append(types.Part.from_function_response(name=llamada.name, response=salida))
        mensajes.append(types.Content(role="user", parts=partes))

    return {"respuesta": "Perdón, me enredé. ¿Me lo repites?", "acciones": acciones}
