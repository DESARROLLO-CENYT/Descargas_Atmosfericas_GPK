"""Foto de la cache del tablero, guardada en la propia base de datos.

Render Free borra la memoria y el disco cada vez que el servicio se duerme, se
redespliega o se reinicia, y el plan gratuito no tiene discos persistentes. Sin
foto, cada arranque en frio tendria que pedirle a Supabase las ~38 mil filas de
la zona (~3,4 MB de egress). Con foto baja una sola fila comprimida (~0,6 MB) y
la sincronizacion por huellas trae solo los dias que cambiaron despues.

Una foto guarda:
- la zona de rayos, con la caja de coordenadas con que se filtro,
- la huella de cada dia de toda la region (para los totales y para detectar
  cambios), y
- la huella global de esos datos.

Formato binario (FORMATO = 1):

    b"GPKFOTO" | formato uint16 | largo meta uint32 | meta JSON
               | largo zona uint64 | zona parquet zstd
               | largo dias uint64 | dias parquet zstd

Si algun dia cambia el contenido, se sube FORMATO: una foto de otro formato se
descarta y se arma una nueva desde las filas.
"""
import io
import json
import struct
from datetime import date
from decimal import Decimal

import polars as pl

from backend.lectura_parquet import COLUMNAS

FORMATO = 1
_FIRMA = b"GPKFOTO"

# Medido sobre el historico completo (38.857 filas de zona + 1.475 dias), en
# local; en Render Free (0,1 de CPU) comprimir tarda ~10 veces mas:
#
#   nivel   foto    por el cable (base64)   comprimir   leer
#       3   498 KB         673 KB              22 ms    13 ms
#      15   455 KB         614 KB             166 ms     8 ms
#      19   443 KB         598 KB             485 ms     8 ms
#      22   442 KB         598 KB             469 ms     9 ms
#
# Se comprime una vez por version de los datos (en la practica, una vez al dia,
# en segundo plano) y se descarga en cada arranque en frio: conviene gastar CPU
# al guardar para bajar menos al leer. Por encima de 19 ya no achica.
NIVEL_ZSTD = 19

# Una foto sirve si su caja cubre la actual. La tolerancia absorbe solo las
# diferencias de ultimo digito entre plataformas (la foto puede armarse en
# Windows y leerse en el Linux de Render): el error de redondeo de una
# coordenada ~72 es ~1e-14, y las coordenadas de la base tienen 8 decimales, asi
# que ningun rayo real cae en una franja de 1e-12 grados.
TOLERANCIA_GRADOS = 1e-12

_ESQUEMA_DIAS = {"Fecha": pl.Date, "n": pl.Int64, "nc": pl.Int64, "sc": pl.Int64,
                 "sla": pl.Utf8, "slo": pl.Utf8}


class FotoInvalida(Exception):
    """La foto no se puede usar (corrupta o de otro formato): se arma desde las filas."""


def huella_a_texto(huella_global) -> str:
    """Huella global en texto. Las sumas de coordenadas van como texto del
    Decimal: exactas, sin pasar por float."""
    fecha, (n, nc, sc, sla, slo) = huella_global
    return json.dumps([fecha.isoformat() if fecha else None, n, nc, sc, str(sla), str(slo)])


def texto_a_huella(texto: str) -> tuple:
    fecha, n, nc, sc, sla, slo = json.loads(texto)
    return (date.fromisoformat(fecha) if fecha else None, (n, nc, sc, Decimal(sla), Decimal(slo)))


def caja_cubre(exterior: dict | None, interior: dict | None) -> bool:
    """True si la zona guardada con la caja `exterior` contiene todos los rayos de
    la caja `interior`."""
    if interior is None:
        return True
    if exterior is None:
        return False
    t = TOLERANCIA_GRADOS
    return (exterior["la0"] <= interior["la0"] + t and exterior["la1"] >= interior["la1"] - t
            and exterior["lo0"] <= interior["lo0"] + t and exterior["lo1"] >= interior["lo1"] - t)


def recortar(zona: pl.DataFrame, caja: dict | None) -> pl.DataFrame:
    """Los rayos de la zona que caen en la caja: lo mismo que devolveria la base
    filtrando con esa caja."""
    if caja is None:
        return zona.clear()
    return zona.filter(pl.col("Latitud").is_between(caja["la0"], caja["la1"])
                       & pl.col("Longitud").is_between(caja["lo0"], caja["lo1"]))


def _parquet(df: pl.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.write_parquet(buf, compression="zstd", compression_level=NIVEL_ZSTD)
    return buf.getvalue()


def empaquetar(zona: pl.DataFrame, huellas: dict, caja: dict | None, huella_global) -> bytes:
    meta = json.dumps({"formato": FORMATO, "caja": caja, "huella": huella_a_texto(huella_global)}).encode()
    fechas = sorted(huellas)
    dias = pl.DataFrame({
        "Fecha": fechas,
        "n": [huellas[f][0] for f in fechas],
        "nc": [huellas[f][1] for f in fechas],
        "sc": [huellas[f][2] for f in fechas],
        "sla": [str(huellas[f][3]) for f in fechas],
        "slo": [str(huellas[f][4]) for f in fechas],
    }, schema=_ESQUEMA_DIAS)
    zona_bytes = _parquet(zona.select(list(COLUMNAS)))
    dias_bytes = _parquet(dias)
    return b"".join([
        _FIRMA, struct.pack("<HI", FORMATO, len(meta)), meta,
        struct.pack("<Q", len(zona_bytes)), zona_bytes,
        struct.pack("<Q", len(dias_bytes)), dias_bytes,
    ])


def desempaquetar(blob: bytes) -> tuple[dict, pl.DataFrame, dict]:
    """(meta, zona, huellas por dia). Levanta FotoInvalida si no se puede usar."""
    try:
        if not blob.startswith(_FIRMA):
            raise FotoInvalida("no empieza con la firma de una foto")
        pos = len(_FIRMA)
        formato, largo_meta = struct.unpack_from("<HI", blob, pos)
        pos += struct.calcsize("<HI")
        if formato != FORMATO:
            raise FotoInvalida(f"formato {formato}, se esperaba {FORMATO}")
        meta = json.loads(blob[pos:pos + largo_meta])
        pos += largo_meta
        partes = []
        for _ in range(2):
            (largo,) = struct.unpack_from("<Q", blob, pos)
            pos += 8
            partes.append(pl.read_parquet(io.BytesIO(blob[pos:pos + largo])))
            pos += largo
        if pos != len(blob):
            raise FotoInvalida("sobran o faltan bytes")
    except FotoInvalida:
        raise
    except Exception as e:
        raise FotoInvalida(f"no se pudo leer: {e}") from e

    zona, dias = partes
    zona = zona.select([pl.col(c).cast(t) for c, t in COLUMNAS.items()])
    huellas = {f: (n, nc, sc, Decimal(sla), Decimal(slo))
               for f, n, nc, sc, sla, slo in dias.select(list(_ESQUEMA_DIAS)).iter_rows()}
    return meta, zona, huellas
