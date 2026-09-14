"""Consultas sobre los datos en memoria, con datos chicos armados a mano.

Cubren casos que el parquet real no tiene, como rayos sin coordenadas.
"""
from datetime import date
from decimal import Decimal

import polars as pl

from backend import datos
from backend.lectura_parquet import COLUMNAS


def rayo(fecha, lat, lon, hora="10:00:00", corriente=-10.0):
    return (fecha, hora, lat, lon, corriente, "Negativo", 0.1)


ZONA = pl.DataFrame([
    rayo(date(2024, 1, 5), 4.40, -72.70),
    rayo(date(2024, 1, 5), 4.45, -72.65, hora="09:00:00"),
    rayo(date(2024, 1, 20), 4.90, -72.70),   # fuera de la caja de abajo
    rayo(date(2025, 1, 3), 4.41, -72.69),
    rayo(date(2025, 2, 1), 4.42, -72.68),
], schema=COLUMNAS, orient="row")

# n = todos los rayos de la region ese dia; nc = los que tienen coordenadas
HUELLAS = {
    date(2024, 1, 5): (10, 8, 0, Decimal(0), Decimal(0)),
    date(2024, 1, 20): (4, 4, 0, Decimal(0), Decimal(0)),
    date(2025, 1, 3): (6, 5, 0, Decimal(0), Decimal(0)),
    date(2025, 2, 1): (3, 3, 0, Decimal(0), Decimal(0)),
}
CAJA = {"la0": 4.3, "la1": 4.5, "lo0": -72.8, "lo1": -72.6}


def inst():
    return datos.Instantanea(ZONA, HUELLAS, ("prueba",), sincronizada=False)


def test_total_distingue_rayos_sin_coordenadas():
    i = inst()
    assert i.total(None, None, con_coordenadas=False) == 23
    assert i.total(None, None, con_coordenadas=True) == 20
    assert i.total(date(2024, 1, 1), date(2024, 12, 31), con_coordenadas=True) == 12


def test_descargas_filtra_por_fecha_y_caja():
    i = inst()
    todas = i.descargas(None, None, CAJA)
    assert len(todas) == 4
    enero_2024 = i.descargas(date(2024, 1, 1), date(2024, 1, 31), CAJA)
    assert enero_2024["Fecha"].to_list() == [date(2024, 1, 5), date(2024, 1, 5)]
    assert len(i.descargas(None, None, None)) == 0


def test_orden_fijo_sin_importar_el_orden_de_llegada():
    al_reves = datos.Instantanea(ZONA.reverse(), HUELLAS, ("prueba",), sincronizada=False)
    assert al_reves.zona.equals(inst().zona)
    # Mismo dia: primero la hora mas temprana
    assert inst().zona["Hora"].to_list()[:2] == ["09:00:00", "10:00:00"]


def test_conteos_para_el_calendario():
    i = inst()
    assert i.conteo_por_dia(date(2024, 1, 1), date(2024, 1, 31)) == {"2024-01-05": 10, "2024-01-20": 4}
    assert i.comparacion_anios(1) == [{"anio": 2024, "total": 14}, {"anio": 2025, "total": 6}]
    assert i.dias_con_datos() == ["2024-01-05", "2024-01-20", "2025-01-03", "2025-02-01"]
    assert i.fecha_max == date(2025, 2, 1)


def test_arbol_de_rayos():
    arbol, fechas = inst().arbol()
    assert len(fechas) == len(ZONA)
    vacio = datos.Instantanea(ZONA.clear(), {}, ("prueba",), sincronizada=False)
    assert vacio.arbol() == (None, None)
