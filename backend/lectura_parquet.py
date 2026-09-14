"""Lectura del parquet de descargas, pensada para correr en un proceso aparte.

Leer el historico completo (~780 mil filas) para quedarse con la zona de las
estructuras y los conteos por dia deja memoria ocupada en el proceso que lo
hace, aunque el resultado pese ~3 MB: medido, el servidor quedaba entre 57 y
160 MB mas pesado, y en Render Free (512 MB) eso llevaba la prueba de 10
usuarios a 557 MB. Hecho en un proceso aparte, esa memoria se libera al
terminar y el servidor solo recibe el resultado (+8 MB).

Solo importa Polars, para que el proceso aparte arranque rapido.

Uso interno: python -m backend.lectura_parquet RUTA CAJA_JSON CON_HUELLAS
Escribe en stdout la zona y los dias, cada uno como Arrow IPC precedido por su
largo (8 bytes).
"""
import io
import json
import struct
import sys

import polars as pl

COLUMNAS = {
    "Fecha": pl.Date,
    "Hora": pl.Utf8,
    "Latitud": pl.Float64,
    "Longitud": pl.Float64,
    "Corriente_kA": pl.Float64,
    "Polaridad_Descargas": pl.Utf8,
    "Error_km": pl.Float64,
}


def leer(ruta: str, caja: dict | None, con_huellas: bool) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(zona, dias): los rayos dentro de la caja y, por dia, cantidad de rayos,
    cantidad con coordenadas y, si se pide, la huella para comparar con Supabase."""
    lf = pl.scan_parquet(ruta)
    faltan = [c for c in COLUMNAS if c not in lf.collect_schema()]
    if faltan:
        raise ValueError(f"Al parquet {ruta} le faltan las columnas {faltan}")

    tipos = [pl.col(c).cast(t) for c, t in COLUMNAS.items()]
    if caja is None:
        zona = pl.DataFrame(schema=COLUMNAS)
    else:
        # El filtro va antes de convertir el resto de columnas: asi solo se
        # arman las filas de la zona y no las 780 mil
        zona = (lf.filter(pl.col("Fecha").is_not_null()
                          & pl.col("Latitud").cast(pl.Float64).is_between(caja["la0"], caja["la1"])
                          & pl.col("Longitud").cast(pl.Float64).is_between(caja["lo0"], caja["lo1"]))
                  .select(tipos)
                  .collect(engine="streaming"))

    agregados = [
        pl.len().alias("n"),
        (pl.col("Latitud").is_not_null() & pl.col("Longitud").is_not_null()).sum().alias("nc"),
    ]
    if con_huellas:
        agregados += [
            (pl.col("Corriente_kA") * 10000).round(0).cast(pl.Int64).sum().alias("sc"),
            pl.col("Latitud").sum().alias("sla"),
            pl.col("Longitud").sum().alias("slo"),
        ]
    dias = (lf.filter(pl.col("Fecha").is_not_null())
              .group_by("Fecha").agg(agregados)
              .collect(engine="streaming"))
    return zona, dias


def _main():
    ruta, caja, con_huellas = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3] == "1"
    salida = sys.stdout.buffer
    for df in leer(ruta, caja, con_huellas):
        buf = io.BytesIO()
        df.write_ipc(buf)
        contenido = buf.getvalue()
        salida.write(struct.pack("<Q", len(contenido)))
        salida.write(contenido)
    salida.flush()


if __name__ == "__main__":
    _main()
