"""Pruebas de las reglas del negocio. No usan la IA: corren gratis y en segundos.

Correr con:  uv run pytest
"""

import threading
from datetime import datetime

import pytest

from app import db, tools
from app.agent import ejecutar_herramienta

LUNES = "2026-11-02"
MARTES = "2026-11-03"
SABADO = "2026-11-07"
TEL = "+57 300 111 2222"
OTRO_TEL = "+57 300 999 8888"


@pytest.fixture(autouse=True)
def bd_de_prueba(tmp_path, monkeypatch):
    """Cada prueba usa una base de datos nueva y un 'ahora' fijo: lunes 2 de nov, 8:00 a. m."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(db, "ahora", lambda: datetime(2026, 11, 2, 8, 0))
    monkeypatch.setattr(db, "DIAS_CERRADOS", set())
    db.inicializar()


def agendar(hora="10:00", fecha=LUNES, servicio="limpieza", telefono=TEL, paciente="Ana Pérez"):
    return tools.agendar_cita(paciente, servicio, fecha, hora, telefono=telefono)


# ---------- Horas y fechas como las escribe la gente ----------

@pytest.mark.parametrize("entrada,esperado", [
    ("10:00", "10:00"), ("9:00", "09:00"), ("9", "09:00"), ("15:00:00", "15:00"),
    ("3pm", "15:00"), ("3 pm", "15:00"), ("3:30 p.m.", "15:30"), ("3:30PM", "15:30"),
    ("12pm", "12:00"), ("11 am", "11:00"),
])
def test_normaliza_horas(entrada, esperado):
    assert tools.normalizar_hora(entrada) == esperado


@pytest.mark.parametrize("entrada", ["", "mañana", "25:00", "10:75", "tres de la tarde"])
def test_rechaza_horas_invalidas(entrada):
    assert tools.normalizar_hora(entrada) is None


def test_acepta_fecha_sin_ceros():
    assert "horarios_libres" in tools.ver_disponibilidad("2026-11-2", "limpieza")


@pytest.mark.parametrize("fecha,palabra", [
    ("2026-10-30", "ya pasó"),
    (SABADO, "fin de semana"),
    ("2027-06-01", "días adelante"),
    ("el lunes", "inválida"),
])
def test_dias_no_agendables(fecha, palabra):
    assert palabra in tools.ver_disponibilidad(fecha, "limpieza")["error"]


def test_festivo(monkeypatch):
    monkeypatch.setattr(db, "DIAS_CERRADOS", {MARTES})
    assert "festivo" in tools.ver_disponibilidad(MARTES, "limpieza")["error"]


def test_servicio_inexistente():
    assert "error" in tools.ver_disponibilidad(LUNES, "ortodoncia")
    assert "error" in agendar(servicio="ortodoncia")


def test_no_ofrece_horas_que_ya_pasaron(monkeypatch):
    monkeypatch.setattr(db, "ahora", lambda: datetime(2026, 11, 2, 14, 10))
    libres = tools.ver_disponibilidad(LUNES, "limpieza")["horarios_libres"]
    assert libres[0] == "14:30"


# ---------- Agendar ----------

def test_agenda_y_ocupa_el_horario():
    assert agendar("10:00")["ok"]
    libres = tools.ver_disponibilidad(LUNES, "limpieza")["horarios_libres"]
    assert "10:00" not in libres and "10:30" in libres


def test_servicio_largo_ocupa_dos_bloques():
    assert agendar("10:00", servicio="blanqueamiento")["ok"]  # 60 minutos
    libres = tools.ver_disponibilidad(LUNES, "limpieza")["horarios_libres"]
    assert "10:00" not in libres and "10:30" not in libres and "11:00" in libres


def test_no_cabe_un_servicio_largo_al_cierre():
    assert "17:30" not in tools.ver_disponibilidad(LUNES, "blanqueamiento")["horarios_libres"]
    assert "error" in agendar("17:30", servicio="blanqueamiento")


def test_acepta_hora_en_formato_humano():
    resultado = agendar("3pm")
    assert resultado["ok"] and resultado["inicio"] == f"{LUNES} 15:00"


def test_no_permite_doble_reserva_entre_pacientes():
    assert agendar("10:00", telefono=TEL)["ok"]
    resultado = agendar("10:00", telefono=OTRO_TEL, paciente="Otro Paciente")
    assert "error" in resultado and "horarios_libres" in resultado


def test_reintento_no_duplica_la_cita():
    primera = agendar("10:00")
    segunda = agendar("10:00")
    assert segunda["ok"] and segunda["cita_id"] == primera["cita_id"]
    assert len(tools.buscar_citas(telefono=TEL)["citas"]) == 1


def test_pide_nombre_completo():
    assert "nombre" in agendar(paciente="  ")["error"]
    assert "nombre" in agendar(paciente="A")["error"]


def test_veinte_pacientes_al_mismo_tiempo_mismo_horario():
    """El caso que más daño hace: dos reservas simultáneas del mismo horario."""
    resultados = []
    barrera = threading.Barrier(20)

    def reservar(i):
        barrera.wait()
        resultados.append(agendar("11:00", telefono=f"+57 300 000 00{i:02d}", paciente=f"Paciente {i}"))

    hilos = [threading.Thread(target=reservar, args=(i,)) for i in range(20)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sum(1 for r in resultados if r.get("ok")) == 1
    with db.conectar() as conn:
        assert conn.execute("SELECT COUNT(*) FROM citas").fetchone()[0] == 1


# ---------- Buscar y cancelar: cada quien solo lo suyo ----------

def test_cada_paciente_ve_solo_sus_citas():
    agendar("10:00", telefono=TEL)
    agendar("11:00", telefono=OTRO_TEL, paciente="Otro Paciente")
    citas = tools.buscar_citas(telefono=TEL)["citas"]
    assert len(citas) == 1 and citas[0]["paciente"] == "Ana Pérez"


def test_no_cancela_citas_ajenas():
    cita = agendar("10:00", telefono=OTRO_TEL, paciente="Otro Paciente")
    assert "error" in tools.cancelar_cita(cita["cita_id"], telefono=TEL)


def test_cancela_la_propia_y_libera_el_horario():
    cita = agendar("10:00")
    assert tools.cancelar_cita(cita["cita_id"], telefono=TEL)["ok"]
    assert "error" in tools.cancelar_cita(cita["cita_id"], telefono=TEL)  # ya estaba cancelada
    assert "10:00" in tools.ver_disponibilidad(LUNES, "limpieza")["horarios_libres"]


def test_cancelar_acepta_numero_como_texto_o_decimal():
    cita = agendar("10:00")
    assert tools.cancelar_cita(float(cita["cita_id"]), telefono=TEL)["ok"]
    assert "error" in tools.cancelar_cita("abc", telefono=TEL)


# ---------- La IA no controla el teléfono ----------

def test_las_herramientas_no_piden_telefono_a_la_ia():
    for t in tools.TOOLS:
        assert "telefono" not in t["input_schema"]["properties"], t["name"]


def test_ignora_un_telefono_inventado_por_la_ia():
    salida, error = ejecutar_herramienta(
        "agendar_cita",
        {"paciente": "Ana Pérez", "servicio_id": "limpieza", "fecha": LUNES, "hora": "10:00", "telefono": OTRO_TEL},
        TEL,
    )
    assert not error
    assert tools.buscar_citas(telefono=TEL)["citas"] and not tools.buscar_citas(telefono=OTRO_TEL)["citas"]


def test_herramienta_inexistente_o_parametros_raros():
    assert ejecutar_herramienta("borrar_todo", {}, TEL)[1]
    assert ejecutar_herramienta("ver_disponibilidad", {"fecha": LUNES}, TEL)[1]
    assert ejecutar_herramienta("listar_servicios", {"extra": 1}, TEL)[1]
