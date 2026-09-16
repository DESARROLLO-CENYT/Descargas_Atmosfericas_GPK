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


def intentar(gestor):
    """instantanea() sin propagar DatosNoDisponibles: sirve para disparar el
    reintento en segundo plano mientras la base sigue sin datos."""
    try:
        return gestor.instantanea()
    except datos.DatosNoDisponibles:
        return None


def revisar_ya(gestor):
    """Corre la revision de cambios ahora y en este hilo, sin esperar el minuto."""
    assert gestor._revisando.acquire(blocking=False)
    gestor._revisar_en_segundo_plano()


def test_supabase_arranca_vacio_y_trae_todo_de_la_base(nuevo_gestor, df_completo, caja_zona, tmp_path):
    remoto = RemotoFalso(df_completo)
    # Ruta inexistente a proposito: en produccion la imagen no trae parquet
    gestor = nuevo_gestor("supabase", tmp_path / "no_existe.parquet", remoto)

    inst = gestor.instantanea()

    assert remoto.llamadas == ["huella_global", "huellas_por_mes", "huellas_por_dia", "filas_zona"]
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert gestor.estado()["estado"] == "ok"


def test_baja_solo_los_dias_nuevos(nuevo_gestor, df_completo, ultimos_dias, caja_zona, tmp_path):
    # La base todavia no tiene los ultimos 10 dias; despues el pipeline los publica
    remoto = RemotoFalso(df_completo.filter(pl.col("Fecha") < min(ultimos_dias)))
    gestor = nuevo_gestor("supabase", None, remoto)
    assert gestor.instantanea().fecha_max < min(ultimos_dias)

    remoto.cambiar_datos(df_completo)
    remoto.llamadas.clear()
    remoto.dias_bajados.clear()
    revisar_ya(gestor)

    nuevos = set(df_completo.filter(pl.col("Fecha").is_in(ultimos_dias))["Fecha"].unique())
    assert set(remoto.dias_bajados) == nuevos
    assert remoto.llamadas == ["huella_global", "huellas_por_mes", "huellas_por_dia", "filas_zona"]
    assert iguales(gestor.instantanea(), instantanea_directa(PARQUET, caja_zona(), tmp_path))


def test_si_nada_cambio_hace_una_sola_consulta(nuevo_gestor, df_completo):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", None, remoto)
    gestor.instantanea()

    remoto.llamadas.clear()
    revisar_ya(gestor)

    assert remoto.llamadas == ["huella_global"]
    assert gestor.estado()["estado"] == "ok"


def test_detecta_dias_recargados_y_borrados(nuevo_gestor, df_completo, caja_zona, tmp_path):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", None, remoto)
    gestor.instantanea()

    # El pipeline recargo un dia con una fila menos y borro otro entero
    quitada = df_completo.with_row_index("i").filter(pl.col("Fecha") == FECHA_DE_PRUEBA).row(0)[0]
    borrado = date(2022, 5, 10)
    modificado = (df_completo.with_row_index("i")
                  .filter((pl.col("i") != quitada) & (pl.col("Fecha") != borrado))
                  .drop("i"))
    remoto.cambiar_datos(modificado)
    revisar_ya(gestor)

    inst = gestor.instantanea()
    assert iguales(inst, instantanea_directa(modificado, caja_zona(), tmp_path))
    assert borrado not in inst.huellas
    rayos_originales = df_completo.filter(pl.col("Fecha") == FECHA_DE_PRUEBA).height
    assert inst.huellas[FECHA_DE_PRUEBA][0] == rayos_originales - 1


def test_caida_al_arrancar_recuperacion_y_nueva_caida(nuevo_gestor, df_completo, monkeypatch):
    remoto = RemotoFalso(df_completo)
    remoto.caido = True
    gestor = nuevo_gestor("supabase", None, remoto)

    # Sin base al arrancar no hay copia a la que caer: se dice, no se inventa
    with pytest.raises(datos.DatosNoDisponibles):
        gestor.instantanea()
    assert gestor.estado()["estado"] == "sin_datos"

    # Vuelve: la siguiente peticion dispara el reintento en segundo plano
    monkeypatch.setattr(datos, "REVISION_SEGUNDOS", 0)
    remoto.caido = False
    assert esperar(lambda: intentar(gestor) is not None and gestor.estado()["estado"] == "ok")
    assert gestor.instantanea().fecha_max == df_completo["Fecha"].max()

    # Se cae de nuevo: sigue sirviendo lo ya traido y lo avisa
    remoto.caido = True
    assert esperar(lambda: intentar(gestor) is not None and gestor.estado()["estado"] == "sin_conexion")
    assert gestor.instantanea().fecha_max == df_completo["Fecha"].max()


def test_fuente_parquet_nunca_consulta_supabase(nuevo_gestor, df_completo):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("parquet", PARQUET, remoto)

    gestor.instantanea()

    assert remoto.llamadas == []
    assert gestor.estado()["estado"] == "local"


def test_la_etiqueta_solo_nombra_la_fuente(nuevo_gestor, df_completo):
    """Las dos fuentes sanas se nombran y nada mas, sin fecha.

    El parquet no es data de prueba: son los mismos datos reales del pipeline,
    congelados en la copia del repositorio. La etiqueta anterior ("Datos de
    prueba (parquet) · hasta ...") hacia dudar de un tablero que estaba bien.
    """
    local = nuevo_gestor("parquet", PARQUET, RemotoFalso(df_completo))
    local.instantanea()
    assert local.estado()["mensaje"] == "Data Local (Parquet)"

    base = nuevo_gestor("supabase", None, RemotoFalso(df_completo))
    base.instantanea()
    assert base.estado()["mensaje"] == "Base de datos (Supabase)"


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
