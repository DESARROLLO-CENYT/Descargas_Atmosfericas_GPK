"""scripts/actualizar_parquet.py rechaza archivos invalidos sin tocar el parquet actual."""
import hashlib
import subprocess
import sys

import polars as pl
import pytest

from conftest import PARQUET, RAIZ

SCRIPT = RAIZ / "scripts" / "actualizar_parquet.py"


def correr(*argumentos):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, argumentos)],
                          cwd=RAIZ, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture
def huella_actual():
    return lambda: hashlib.sha256(PARQUET.read_bytes()).hexdigest()


def test_rechaza_un_archivo_que_no_es_parquet(huella_actual):
    antes = huella_actual()
    resultado = correr(RAIZ / "Localizaciones_Final.xlsx")
    assert resultado.returncode == 1
    assert "No se pudo leer" in resultado.stdout + resultado.stderr
    assert huella_actual() == antes


def test_rechaza_un_parquet_sin_las_columnas(tmp_path, huella_actual):
    ruta = tmp_path / "sin_columnas.parquet"
    pl.DataFrame({"x": [1]}).write_parquet(ruta)
    antes = huella_actual()
    resultado = correr(ruta)
    assert resultado.returncode == 1
    assert "falta la columna Fecha" in resultado.stdout
    assert huella_actual() == antes


def test_rechaza_datos_mas_viejos_que_los_actuales(parquet_viejo, huella_actual):
    antes = huella_actual()
    resultado = correr(parquet_viejo)
    assert resultado.returncode == 1
    assert "antes que el actual" in resultado.stdout
    assert huella_actual() == antes
