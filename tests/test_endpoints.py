"""Endpoints del tablero: las dos fuentes dan exactamente la misma respuesta."""
import pytest
from fastapi.testclient import TestClient

from backend import datos
from conftest import RemotoFalso

TODO = {"fecha_inicio": "2021-01-01", "fecha_fin": "2026-12-31"}


@pytest.fixture(scope="module")
def cliente(m):
    # Sin "with": no corre la preparacion en segundo plano del arranque
    return TestClient(m.app)


@pytest.fixture(scope="module")
def filtros(cliente):
    datos_filtros = cliente.get("/api/filtros").json()
    campo = next(iter(datos_filtros["filtros"]))
    locacion = next(iter(datos_filtros["filtros"][campo]))
    return {
        "campo": campo,
        "locacion": locacion,
        "portico": datos_filtros["filtros"][campo][locacion][0],
        "estructura": next(e["id"] for e in datos_filtros["estructuras"] if e["campo"]),
    }


def casos(f):
    return [
        ("get", "/api/rango-fechas", None),
        ("post", "/api/procesar", {"radio_busqueda_metros": 100, "fecha_inicio": "2026-09-13", "fecha_fin": "2026-09-13"}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 500, "fecha_inicio": "2026-01-01", "fecha_fin": "2026-01-31"}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 5000, **TODO}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 2000, "fecha_inicio": "2023-03-01", "fecha_fin": "2023-03-31", "filtro_campo": f["campo"]}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 1000, **TODO, "filtro_estructura": f["estructura"]}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 1000, **TODO, "filtro_estructura": "NO-EXISTE"}),
        ("post", "/api/procesar", {"radio_busqueda_metros": 1000, "fecha_inicio": "2020-01-01", "fecha_fin": "2020-01-31"}),
        ("post", "/api/calendario", {"anio": 2023, "mes": 3, "radio_busqueda_metros": 2000}),
        ("post", "/api/calendario", {"anio": 2024, "mes": 10, "radio_busqueda_metros": 1000, "filtro_portico": f["portico"]}),
        ("post", "/api/simulador", {"radio_busqueda_metros": 2000, "fecha_inicio": "2023-03-01", "fecha_fin": "2023-03-31"}),
        ("post", "/api/simulador", {"radio_busqueda_metros": 5000, **TODO, "filtro_proteccion": "porticos"}),
        ("post", "/api/dias-radio", {"radio_busqueda_metros": 500}),
        ("post", "/api/dias-radio", {"radio_busqueda_metros": 1000, "filtro_locacion": f["locacion"]}),
    ]


def respuestas(cliente, lista):
    salida = []
    for metodo, ruta, cuerpo in lista:
        r = cliente.get(ruta) if metodo == "get" else cliente.post(ruta, data=cuerpo)
        assert r.status_code == 200, (ruta, cuerpo, r.text[:300])
        salida.append(r.content)
    return salida


def test_parquet_y_supabase_responden_igual(cliente, filtros, nuevo_gestor, parquet_viejo, df_completo, monkeypatch):
    lista = casos(filtros)

    monkeypatch.setattr(datos, "_gestor", nuevo_gestor("parquet", datos.ARCHIVO_PARQUET))
    con_parquet = respuestas(cliente, lista)

    # Supabase simulado partiendo de una copia vieja: tiene que sincronizar
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", parquet_viejo, remoto)
    monkeypatch.setattr(datos, "_gestor", gestor)
    con_supabase = respuestas(cliente, lista)

    assert "filas_zona" in remoto.llamadas
    for (metodo, ruta, cuerpo), a, b in zip(lista, con_parquet, con_supabase):
        assert a == b, f"{ruta} {cuerpo}: las respuestas no son identicas"
    assert cliente.get("/api/fuente-datos").json()["estado"] == "ok"


def test_calendario_y_simulador_cuentan_lo_mismo(cliente, nuevo_gestor, monkeypatch):
    monkeypatch.setattr(datos, "_gestor", nuevo_gestor("parquet", datos.ARCHIVO_PARQUET))
    mes = {"radio_busqueda_metros": 2000}

    calendario = cliente.post("/api/calendario", data={"anio": 2023, "mes": 3, **mes}).json()
    simulador = cliente.post("/api/simulador", data={"fecha_inicio": "2023-03-01", "fecha_fin": "2023-03-31", **mes}).json()

    assert calendario["resumen"]["total_radio"] == simulador["total_radio"] > 0
    assert calendario["resumen"]["total_rango"] == simulador["total_region"] > 0


def test_etiqueta_de_la_fuente(cliente, nuevo_gestor, monkeypatch):
    gestor = nuevo_gestor("parquet", datos.ARCHIVO_PARQUET)
    monkeypatch.setattr(datos, "_gestor", gestor)
    assert cliente.get("/api/fuente-datos").json()["estado"] == "cargando"

    cliente.get("/api/rango-fechas")
    fuente = cliente.get("/api/fuente-datos").json()

    assert fuente["fuente"] == "parquet"
    assert fuente["mensaje"].startswith("Datos de prueba (parquet)")
