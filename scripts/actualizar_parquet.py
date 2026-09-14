"""Actualiza el parquet de pruebas del tablero con el Gold que genera el pipeline.

No se conecta a Supabase ni gasta egress: copia el archivo que el pipeline deja
cada dia en Data/3. Gold, que tiene exactamente los mismos datos que sube a la
base. Antes de reemplazar revisa que el archivo nuevo sea valido y no traiga
menos datos que el actual.

Uso (desde la raiz del repositorio):
    python scripts/actualizar_parquet.py
    python scripts/actualizar_parquet.py "C:\\ruta\\Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"

Sin argumento usa RUTA_GOLD_PIPELINE del .env. Con FUENTE_DATOS=parquet el
tablero toma el archivo nuevo solo, sin reiniciar.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

import polars as pl
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend.datos import ARCHIVO_PARQUET, COLUMNAS  # noqa: E402

DESTINO = RAIZ / ARCHIVO_PARQUET
# Mismo freno que usa el pipeline al publicar: si el archivo nuevo trae menos
# del 95 % de las filas del actual, algo salio mal y no se copia
MINIMO_FILAS = 0.95


def resumen(ruta: Path):
    lf = pl.scan_parquet(ruta)
    esquema = lf.collect_schema()
    if "Fecha" not in esquema:
        return esquema, None
    fila = lf.select(
        pl.len().alias("filas"),
        pl.col("Fecha").min().alias("desde"),
        pl.col("Fecha").max().alias("hasta"),
        pl.col("Fecha").null_count().alias("fechas_nulas"),
        pl.col("Fecha").drop_nulls().n_unique().alias("dias"),
    ).collect()
    return esquema, fila.row(0, named=True)


def main():
    load_dotenv(RAIZ / ".env")
    parser = argparse.ArgumentParser(description="Copia el Gold del pipeline como parquet de pruebas.")
    parser.add_argument("origen", nargs="?", default=os.environ.get("RUTA_GOLD_PIPELINE"),
                        help="parquet Gold del pipeline (por defecto RUTA_GOLD_PIPELINE del .env)")
    parser.add_argument("--forzar", action="store_true",
                        help="copiar aunque traiga menos filas o fechas que el actual")
    args = parser.parse_args()

    if not args.origen:
        sys.exit("Falta la ruta del Gold del pipeline: pasala como argumento o define RUTA_GOLD_PIPELINE en .env")
    origen = Path(args.origen)
    if not origen.is_file():
        sys.exit(f"No existe el archivo {origen}")

    try:
        esquema, nuevo = resumen(origen)
    except Exception as e:
        sys.exit(f"No se pudo leer {origen} como parquet: {e}")
    problemas = [f"falta la columna {c}" for c in COLUMNAS if c not in esquema]
    if nuevo is None:
        print("No se copio el archivo:\n  - " + "\n  - ".join(problemas))
        sys.exit(1)
    if nuevo["fechas_nulas"]:
        problemas.append(f"{nuevo['fechas_nulas']} filas sin fecha")

    actual = None
    if DESTINO.is_file():
        esquema_actual, actual = resumen(DESTINO)
        for c in COLUMNAS:
            if c in esquema and c in esquema_actual and esquema[c] != esquema_actual[c]:
                problemas.append(f"la columna {c} cambio de tipo: {esquema_actual[c]} -> {esquema[c]}")
        if not args.forzar:
            if nuevo["filas"] < actual["filas"] * MINIMO_FILAS:
                problemas.append(f"trae {nuevo['filas']:,} filas, menos del 95 % de las {actual['filas']:,} actuales")
            if actual["hasta"] and nuevo["hasta"] < actual["hasta"]:
                problemas.append(f"llega hasta {nuevo['hasta']}, antes que el actual ({actual['hasta']})")

    if problemas:
        print("No se copio el archivo:")
        for p in problemas:
            print(f"  - {p}")
        sys.exit(1)

    # Copia a un temporal y reemplazo atomico: el tablero nunca ve un archivo a medias
    temporal = DESTINO.with_suffix(".parquet.tmp")
    shutil.copyfile(origen, temporal)
    os.replace(temporal, DESTINO)

    print(f"Parquet actualizado desde {origen}")
    if actual:
        print(f"  filas: {actual['filas']:,} -> {nuevo['filas']:,} ({nuevo['filas'] - actual['filas']:+,})")
        print(f"  dias:  {actual['dias']:,} -> {nuevo['dias']:,} ({nuevo['dias'] - actual['dias']:+,})")
        print(f"  hasta: {actual['hasta']} -> {nuevo['hasta']}")
    else:
        print(f"  filas: {nuevo['filas']:,} | dias: {nuevo['dias']:,} | {nuevo['desde']} a {nuevo['hasta']}")
    print("Para que la copia llegue a Render (respaldo y punto de partida en modo supabase), commitea el archivo.")


if __name__ == "__main__":
    main()
