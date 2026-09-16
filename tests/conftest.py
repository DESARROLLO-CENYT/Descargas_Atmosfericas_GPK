"""Configuracion compartida de las pruebas.

Ninguna prueba se conecta a Supabase: el papel de la base lo cumple
RemotoFalso, que responde las mismas consultas que RemotoSupabase calculandolas
sobre el parquet del repositorio. Por si algo intentara conectarse igual, la URL
de Supabase apunta a un puerto local cerrado y falla sin salir a internet.
"""
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
os.environ["FUENTE_DATOS"] = "parquet"
os.environ["SUPABASE_DASHBOARD_DB_URL"] = "postgresql://sin_red:sin_red@127.0.0.1:1/sin_red"
os.environ.setdefault("POLARS_MAX_THREADS", "2")
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))

import polars as pl  # noqa: E402
import pytest  # noqa: E402

from backend import datos  # noqa: E402
from backend.db import SupabaseNoDisponible  # noqa: E402
from backend.lectura_parquet import COLUMNAS  # noqa: E402

PARQUET = RAIZ / datos.ARCHIVO_PARQUET


def _agregados():
    return [
        pl.len().alias("n"),
        (pl.col("Latitud").is_not_null() & pl.col("Longitud").is_not_null()).sum().alias("nc"),
        (pl.col("Corriente_kA") * 10000).round(0).cast(pl.Int64).sum().alias("sc"),
        pl.col("Latitud").sum().alias("sla"),
        pl.col("Longitud").sum().alias("slo"),
    ]


class RemotoFalso:
    """Supabase simulado sobre un DataFrame, con las mismas consultas que
    backend.datos.RemotoSupabase. Registra cada llamada."""

    def __init__(self, df: pl.DataFrame):
        self.cambiar_datos(df)
        self.caido = False
        self.llamadas = []
        self.dias_bajados = []
        # La tabla de la foto: (meta, contenido) como quedaria guardada
        self.foto = None
        self.foto_instalada = True   # False: no se aplico sql/001_foto_tablero.sql
        self.falla_guardar = False

    def cambiar_datos(self, df: pl.DataFrame):
        """Lo que haria el pipeline al publicar: la base pasa a tener estos datos."""
        self.df = df.filter(pl.col("Fecha").is_not_null())

    def _registrar(self, nombre):
        if self.caido:
            raise SupabaseNoDisponible("simulado: sin conexion")
        self.llamadas.append(nombre)

    def huella_global(self):
        self._registrar("huella_global")
        fila = self.df.select([pl.col("Fecha").max(), *_agregados()]).row(0)
        return fila[0], datos._huella(*fila[1:])

    def huellas_por_mes(self):
        self._registrar("huellas_por_mes")
        g = self.df.group_by(pl.col("Fecha").dt.truncate("1mo")).agg(_agregados())
        return {f[0]: datos._huella(*f[1:]) for f in g.iter_rows()}

    def huellas_por_dia(self, meses):
        self._registrar("huellas_por_dia")
        g = (self.df.filter(pl.col("Fecha").dt.truncate("1mo").is_in(meses))
                    .group_by("Fecha").agg(_agregados()))
        return {f[0]: datos._huella(*f[1:]) for f in g.iter_rows()}

    def filas_zona(self, dias, caja):
        self._registrar("filas_zona")
        self.dias_bajados.extend(dias)
        return (self.df.select([pl.col(c).cast(t) for c, t in COLUMNAS.items()])
                       .filter(pl.col("Fecha").is_in(dias)
                               & pl.col("Latitud").is_between(caja["la0"], caja["la1"])
                               & pl.col("Longitud").is_between(caja["lo0"], caja["lo1"])))

    # Mismo contrato que RemotoSupabase: sin tabla (no instalada) leer da None,
    # como hace _consulta_foto, y guardar falla, como la funcion inexistente
    def leer_foto_meta(self):
        self._registrar("leer_foto_meta")
        if not self.foto_instalada or self.foto is None:
            return None
        return dict(self.foto[0])

    def leer_foto(self):
        self._registrar("leer_foto")
        if not self.foto_instalada or self.foto is None:
            return None
        return self.foto[1]

    def guardar_foto(self, formato, caja, huella, contenido):
        self._registrar("guardar_foto")
        if not self.foto_instalada or self.falla_guardar:
            raise SupabaseNoDisponible("simulado: no se pudo guardar la foto")
        # Por JSON, como la columna jsonb: la caja vuelve como la leeria la base
        self.foto = ({"formato": formato, "caja": json.loads(json.dumps(caja)), "huella": huella}, contenido)


@pytest.fixture(scope="session")
def m():
    import backend.main as main
    return main


@pytest.fixture(scope="session")
def caja_zona(m):
    return lambda: m._caja_alrededor(m.preparar_postes()["df"], m.RADIO_MAXIMO_METROS)


@pytest.fixture(scope="session")
def parquet_local():
    """El parquet vive solo en local (no esta en git). Sin el, las pruebas que lo
    necesitan se saltan diciendo como generarlo, en vez de fallar sin explicacion."""
    if not PARQUET.is_file():
        pytest.skip(f"Falta {datos.ARCHIVO_PARQUET} (solo local, no esta en git). "
                    "Generalo con: python scripts/actualizar_parquet.py")
    return PARQUET


@pytest.fixture(scope="session")
def df_completo(parquet_local):
    """Los datos que tendria Supabase: el parquet local, identico al Gold."""
    return pl.read_parquet(parquet_local)


@pytest.fixture(scope="session")
def ultimos_dias(df_completo):
    fin = df_completo["Fecha"].max()
    return [fin - timedelta(days=i) for i in range(10)]


@pytest.fixture(scope="session")
def parquet_viejo(tmp_path_factory, df_completo, ultimos_dias):
    """Copia sin los ultimos 10 dias: datos desactualizados respecto de la base."""
    ruta = tmp_path_factory.mktemp("datos") / "viejo.parquet"
    df_completo.filter(pl.col("Fecha") < min(ultimos_dias)).write_parquet(ruta)
    return ruta


@pytest.fixture
def nuevo_gestor(m, caja_zona, parquet_local):
    def crear(fuente, ruta, remoto=None):
        return datos.GestorDatos(fuente, caja_zona, m._firma_archivos, ruta_parquet=str(ruta), remoto=remoto)
    return crear


def instantanea_directa(ruta_o_df, caja, tmp_path) -> datos.Instantanea:
    """Lo que deberia quedar en memoria si se leyeran estos datos directamente."""
    if isinstance(ruta_o_df, pl.DataFrame):
        ruta = tmp_path / "directo.parquet"
        ruta_o_df.write_parquet(ruta)
        ruta_o_df = ruta
    zona, huellas = datos.leer_parquet(str(ruta_o_df), caja, con_huellas=True)
    return datos.Instantanea(zona, huellas, None, True)


FECHA_DE_PRUEBA = date(2023, 3, 2)


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


def terminar_foto(gestor):
    """Espera a que termine de guardarse la foto, que va en otro hilo: sin esto
    las llamadas registradas dependerian de quien llega primero."""
    if gestor._hilo_foto is not None:
        gestor._hilo_foto.join(60)
        assert not gestor._hilo_foto.is_alive()
