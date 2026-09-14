"""Gestor de datos: sincronizacion con Supabase (simulado) y fuente parquet."""
import time
from datetime import date

import polars as pl
import pytest

from backend import datos
from conftest import FECHA_DE_PRUEBA, PARQUET, RemotoFalso, instantanea_directa


def iguales(a: datos.Instantanea, b: datos.Instantanea) -> bool:
    return a.zona.equals(b.zona) and a.huellas == b.huellas


def esperar(condicion, segundos=10):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        if condicion():
            return True
        time.sleep(0.1)
    return False


def test_baja_solo_los_dias_nuevos(nuevo_gestor, parquet_viejo, df_completo, ultimos_dias, caja_zona, tmp_path):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", parquet_viejo, remoto)

    inst = gestor.instantanea()

    assert remoto.llamadas == ["huella_global", "huellas_por_mes", "huellas_por_dia", "filas_zona"]
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert inst.fecha_max == max(ultimos_dias)
    assert gestor.estado()["estado"] == "ok"


def test_si_nada_cambio_hace_una_sola_consulta(nuevo_gestor, df_completo):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", PARQUET, remoto)

    inst = gestor.instantanea()

    assert remoto.llamadas == ["huella_global"]
    assert inst.sincronizada
    assert gestor.estado()["estado"] == "ok"


def test_detecta_dias_recargados_y_borrados(nuevo_gestor, df_completo, caja_zona, tmp_path):
    # El pipeline recargo un dia con una fila menos y borro otro entero
    quitada = df_completo.with_row_index("i").filter(pl.col("Fecha") == FECHA_DE_PRUEBA).row(0)[0]
    borrado = date(2022, 5, 10)
    modificado = (df_completo.with_row_index("i")
                  .filter((pl.col("i") != quitada) & (pl.col("Fecha") != borrado))
                  .drop("i"))
    remoto = RemotoFalso(modificado)
    gestor = nuevo_gestor("supabase", PARQUET, remoto)

    inst = gestor.instantanea()

    assert iguales(inst, instantanea_directa(modificado, caja_zona(), tmp_path))
    assert borrado not in inst.huellas
    rayos_originales = df_completo.filter(pl.col("Fecha") == FECHA_DE_PRUEBA).height
    assert inst.huellas[FECHA_DE_PRUEBA][0] == rayos_originales - 1


def test_caida_recuperacion_y_nueva_caida(nuevo_gestor, parquet_viejo, df_completo, ultimos_dias, monkeypatch):
    remoto = RemotoFalso(df_completo)
    remoto.caido = True
    gestor = nuevo_gestor("supabase", parquet_viejo, remoto)

    # Supabase caido al arrancar: responde con la copia y lo avisa
    inst = gestor.instantanea()
    assert inst.fecha_max < min(ultimos_dias)
    assert gestor.estado()["estado"] == "respaldo"

    # Vuelve: la siguiente peticion dispara la revision en segundo plano
    monkeypatch.setattr(datos, "REVISION_SEGUNDOS", 0)
    remoto.caido = False
    assert esperar(lambda: gestor.instantanea() and gestor.estado()["estado"] == "ok")
    assert gestor.instantanea().fecha_max == max(ultimos_dias)

    # Se cae de nuevo: sigue con lo ya sincronizado, que es mas nuevo que la copia
    remoto.caido = True
    assert esperar(lambda: gestor.instantanea() and gestor.estado()["estado"] == "sin_conexion")
    assert gestor.instantanea().fecha_max == max(ultimos_dias)


def test_fuente_parquet_nunca_consulta_supabase(nuevo_gestor, df_completo):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("parquet", PARQUET, remoto)

    gestor.instantanea()

    assert remoto.llamadas == []
    assert gestor.estado()["estado"] == "prueba"


def test_fuente_parquet_recarga_si_cambia_el_archivo(nuevo_gestor, parquet_viejo, tmp_path, ultimos_dias):
    ruta = tmp_path / "datos.parquet"
    ruta.write_bytes(parquet_viejo.read_bytes())
    gestor = nuevo_gestor("parquet", ruta)
    assert gestor.instantanea().fecha_max < min(ultimos_dias)

    ruta.write_bytes(PARQUET.read_bytes())

    assert gestor.instantanea().fecha_max == max(ultimos_dias)


def test_configuracion_invalida_falla_al_arrancar(m, caja_zona, monkeypatch):
    with pytest.raises(ValueError):
        datos.GestorDatos("supabse", caja_zona, m._firma_archivos)

    monkeypatch.setattr(datos, "_gestor", datos.gestor())
    monkeypatch.setenv("FUENTE_DATOS", "supabase")
    monkeypatch.delenv("SUPABASE_DASHBOARD_DB_URL")
    with pytest.raises(RuntimeError):
        datos.configurar(caja_zona, m._firma_archivos)
