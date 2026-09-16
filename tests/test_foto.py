"""Foto de la cache: formato, y arranque del modo supabase partiendo de ella."""
import struct
from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from backend import datos, foto
from conftest import (PARQUET, RemotoFalso, esperar, iguales, instantanea_directa, intentar, revisar_ya,
                      terminar_foto)


@pytest.fixture
def remoto_con_foto(nuevo_gestor, df_completo):
    """Supabase simulado con la foto que dejo un arranque anterior."""
    remoto = RemotoFalso(df_completo)
    anterior = nuevo_gestor("supabase", None, remoto)
    anterior.instantanea()
    terminar_foto(anterior)
    assert remoto.foto is not None
    remoto.llamadas.clear()
    remoto.dias_bajados.clear()
    return remoto


def agrandar(caja, grados):
    return {"la0": caja["la0"] - grados, "la1": caja["la1"] + grados,
            "lo0": caja["lo0"] - grados, "lo1": caja["lo1"] + grados}


# ---- formato ----

def test_ida_y_vuelta_identica(caja_zona, tmp_path, parquet_local):
    inst = instantanea_directa(PARQUET, caja_zona(), tmp_path)

    contenido = foto.empaquetar(inst.zona, inst.huellas, caja_zona(), inst.huella_global())
    meta, zona, huellas = foto.desempaquetar(contenido)

    assert zona.equals(inst.zona)
    assert huellas == inst.huellas
    assert meta["caja"] == caja_zona() and meta["formato"] == foto.FORMATO
    assert foto.texto_a_huella(meta["huella"]) == inst.huella_global()
    assert len(contenido) < 600 * 1024  # medido: ~443 KB


def test_las_sumas_de_coordenadas_viajan_exactas():
    # Con los datos de hoy las sumas diarias tienen ~14 digitos y un float las
    # aguanta por un digito de margen. Esta no cabe en un float: si la foto
    # pasara las sumas por float, la huella no coincidiria con la de la base y
    # cada arranque volveria a bajar los dias.
    exacta = Decimal("364123.123456789012345678")
    assert Decimal(str(float(exacta))) != exacta
    dia = date(2026, 9, 13)
    huellas = {dia: (5055, 5055, 123456789, exacta, -exacta)}
    global_ = (dia, huellas[dia])

    meta, _, vuelta = foto.desempaquetar(foto.empaquetar(pl.DataFrame(schema=datos.COLUMNAS), huellas, None, global_))

    assert vuelta == huellas
    assert foto.texto_a_huella(meta["huella"]) == global_


def test_foto_corrupta_o_de_otro_formato_no_se_usa(caja_zona, tmp_path, parquet_local):
    inst = instantanea_directa(PARQUET, caja_zona(), tmp_path)
    contenido = foto.empaquetar(inst.zona, inst.huellas, caja_zona(), inst.huella_global())
    firma = len(b"GPKFOTO")
    otro_formato = contenido[:firma] + struct.pack("<H", foto.FORMATO + 1) + contenido[firma + 2:]

    for mala in (b"basura", contenido[:-10], otro_formato):
        with pytest.raises(foto.FotoInvalida):
            foto.desempaquetar(mala)


def test_caja_cubre():
    caja = {"la0": 4.3, "la1": 4.5, "lo0": -72.8, "lo1": -72.5}
    assert foto.caja_cubre(caja, caja)
    assert foto.caja_cubre(agrandar(caja, 0.1), caja)
    assert not foto.caja_cubre(caja, agrandar(caja, 0.1))
    # Solo absorbe diferencias de ultimo digito entre plataformas
    assert foto.caja_cubre(caja, agrandar(caja, 1e-14))
    assert not foto.caja_cubre(caja, agrandar(caja, 1e-8))
    assert foto.caja_cubre(None, None) and foto.caja_cubre(caja, None) and not foto.caja_cubre(None, caja)


# ---- arranque ----

def test_sin_foto_arma_desde_las_filas_y_la_guarda(nuevo_gestor, df_completo, caja_zona, tmp_path):
    remoto = RemotoFalso(df_completo)
    gestor = nuevo_gestor("supabase", None, remoto)

    inst = gestor.instantanea()
    terminar_foto(gestor)

    assert "filas_zona" in remoto.llamadas and remoto.llamadas[-1] == "guardar_foto"
    meta, zona, huellas = foto.desempaquetar(remoto.foto[1])
    assert zona.equals(inst.zona) and huellas == inst.huellas
    assert remoto.foto[0]["caja"] == caja_zona()


def test_con_foto_no_baja_ninguna_fila(remoto_con_foto, nuevo_gestor, caja_zona, tmp_path):
    gestor = nuevo_gestor("supabase", None, remoto_con_foto)

    inst = gestor.instantanea()
    terminar_foto(gestor)

    assert remoto_con_foto.llamadas == ["leer_foto_meta", "leer_foto", "huella_global"]
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert gestor.estado()["estado"] == "ok"


def test_foto_vieja_baja_solo_los_dias_nuevos_y_se_reemplaza(nuevo_gestor, df_completo, ultimos_dias,
                                                            caja_zona, tmp_path):
    # La foto se armo antes de que el pipeline publicara los ultimos 10 dias
    remoto = RemotoFalso(df_completo.filter(pl.col("Fecha") < min(ultimos_dias)))
    anterior = nuevo_gestor("supabase", None, remoto)
    anterior.instantanea()
    terminar_foto(anterior)
    huella_vieja = remoto.foto[0]["huella"]

    remoto.cambiar_datos(df_completo)
    remoto.llamadas.clear()
    remoto.dias_bajados.clear()
    gestor = nuevo_gestor("supabase", None, remoto)
    inst = gestor.instantanea()
    terminar_foto(gestor)

    nuevos = set(df_completo.filter(pl.col("Fecha").is_in(ultimos_dias))["Fecha"].unique())
    assert set(remoto.dias_bajados) == nuevos
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert remoto.llamadas[-1] == "guardar_foto"
    assert remoto.foto[0]["huella"] != huella_vieja
    assert foto.texto_a_huella(remoto.foto[0]["huella"]) == inst.huella_global()


def test_foto_de_una_zona_mas_chica_se_rearma(remoto_con_foto, nuevo_gestor, caja_zona, df_completo):
    # Se agrego una estructura: la zona actual ya no cabe en la de la foto
    chica = dict(caja_zona())
    chica["la1"] -= 0.05
    meta, contenido = remoto_con_foto.foto
    remoto_con_foto.foto = ({**meta, "caja": chica}, contenido)

    gestor = nuevo_gestor("supabase", None, remoto_con_foto)
    gestor.instantanea()
    terminar_foto(gestor)

    # Ni siquiera descarga la foto: con leer sus datos alcanza para descartarla
    assert "leer_foto" not in remoto_con_foto.llamadas
    assert set(remoto_con_foto.dias_bajados) == set(df_completo["Fecha"].drop_nulls().unique())
    assert remoto_con_foto.foto[0]["caja"] == caja_zona()


def test_foto_de_una_zona_mas_grande_se_recorta(nuevo_gestor, df_completo, caja_zona, tmp_path):
    # Se quito una estructura: la foto trae rayos de mas y se recortan a la zona actual
    grande = agrandar(caja_zona(), 0.05)
    completa = instantanea_directa(PARQUET, grande, tmp_path)
    remoto = RemotoFalso(df_completo)
    remoto.guardar_foto(foto.FORMATO, grande, foto.huella_a_texto(completa.huella_global()),
                        foto.empaquetar(completa.zona, completa.huellas, grande, completa.huella_global()))
    remoto.llamadas.clear()

    gestor = nuevo_gestor("supabase", None, remoto)
    inst = gestor.instantanea()
    terminar_foto(gestor)

    assert "filas_zona" not in remoto.llamadas
    assert len(completa.zona) > len(inst.zona)
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    # La reemplaza por una de la zona justa, que pesa menos
    assert remoto.foto[0]["caja"] == caja_zona()


def test_foto_de_otro_formato_se_rearma(remoto_con_foto, nuevo_gestor):
    meta, contenido = remoto_con_foto.foto
    remoto_con_foto.foto = ({**meta, "formato": foto.FORMATO + 1}, contenido)

    gestor = nuevo_gestor("supabase", None, remoto_con_foto)
    gestor.instantanea()
    terminar_foto(gestor)

    assert "leer_foto" not in remoto_con_foto.llamadas
    assert "filas_zona" in remoto_con_foto.llamadas
    assert remoto_con_foto.foto[0]["formato"] == foto.FORMATO


def test_foto_corrupta_en_la_base_se_rearma(remoto_con_foto, nuevo_gestor, caja_zona, tmp_path):
    meta, contenido = remoto_con_foto.foto
    remoto_con_foto.foto = (meta, contenido[:1000])

    gestor = nuevo_gestor("supabase", None, remoto_con_foto)
    inst = gestor.instantanea()
    terminar_foto(gestor)

    assert "filas_zona" in remoto_con_foto.llamadas
    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert foto.desempaquetar(remoto_con_foto.foto[1])  # quedo una sana


def test_si_falla_guardar_sigue_sirviendo_y_no_insiste(nuevo_gestor, df_completo, ultimos_dias):
    remoto = RemotoFalso(df_completo.filter(pl.col("Fecha") < min(ultimos_dias)))
    remoto.falla_guardar = True
    gestor = nuevo_gestor("supabase", None, remoto)

    gestor.instantanea()
    terminar_foto(gestor)
    assert gestor.estado()["estado"] == "ok"
    assert remoto.llamadas.count("guardar_foto") == 1

    # Llegan datos nuevos: se sirven, pero no se vuelve a intentar subir la foto
    # hasta pasado REINTENTO_FOTO_SEGUNDOS
    remoto.cambiar_datos(df_completo)
    revisar_ya(gestor)
    terminar_foto(gestor)
    assert gestor.instantanea().fecha_max == df_completo["Fecha"].max()
    assert remoto.llamadas.count("guardar_foto") == 1


def test_sin_la_tabla_de_la_foto_funciona_como_antes(nuevo_gestor, df_completo, caja_zona, tmp_path):
    # El codigo llego a produccion antes de aplicar sql/001_foto_tablero.sql
    remoto = RemotoFalso(df_completo)
    remoto.foto_instalada = False
    gestor = nuevo_gestor("supabase", None, remoto)

    inst = gestor.instantanea()
    terminar_foto(gestor)

    assert iguales(inst, instantanea_directa(PARQUET, caja_zona(), tmp_path))
    assert gestor.estado()["estado"] == "ok"


def test_tras_una_caida_al_arrancar_reintenta_con_la_foto(remoto_con_foto, nuevo_gestor, monkeypatch):
    remoto_con_foto.caido = True
    gestor = nuevo_gestor("supabase", None, remoto_con_foto)
    with pytest.raises(datos.DatosNoDisponibles):
        gestor.instantanea()

    monkeypatch.setattr(datos, "REVISION_SEGUNDOS", 0)
    remoto_con_foto.caido = False
    assert esperar(lambda: intentar(gestor) is not None and gestor.estado()["estado"] == "ok")
    terminar_foto(gestor)

    assert "leer_foto" in remoto_con_foto.llamadas
    assert "filas_zona" not in remoto_con_foto.llamadas
