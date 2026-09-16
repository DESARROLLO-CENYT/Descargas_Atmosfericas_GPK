"""Fuente de datos del tablero: el parquet local o Supabase, segun FUENTE_DATOS.

- parquet (por defecto): SOLO para trabajar en local. Lee el parquet de datos/,
  que no esta en git ni en la imagen, y nunca se conecta a Supabase aunque la
  URL este configurada: no gasta egress. Si el archivo se reemplaza, se recarga
  solo.
- supabase: la unica fuente de produccion. La imagen no trae parquet, asi que
  arranca vacio y trae de la base lo que falta, comparando huellas (primero la
  global, despues por mes y por dia) y bajando solo los dias distintos. Desde
  cero eso es la zona completa: ~38 mil filas, ~3,4 MB. Mientras no haya traido
  nada, las consultas del tablero responden 503 (DatosNoDisponibles) en vez de
  contestar como si no hubiera rayos.

En las dos fuentes las consultas del tablero se responden desde memoria con el
mismo codigo, asi que con los mismos datos dan exactamente el mismo resultado.

En memoria se guarda solo lo necesario:
- los rayos de la zona de las estructuras (con el radio maximo), que son los
  unicos que pueden caer dentro de algun radio (~5 % del historico), y
- por cada dia de toda la region, la cantidad de rayos y su huella, para los
  totales de las tarjetas y para detectar cambios.
"""
import json
import os
import struct
import subprocess
import sys
import threading
import time
import traceback
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.neighbors import BallTree

from backend import lectura_parquet
from backend.db import SupabaseNoDisponible, consultar
from backend.lectura_parquet import COLUMNAS

ARCHIVO_PARQUET = "datos/Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
FUENTES = ("parquet", "supabase")

# Cada cuanto se le pregunta a Supabase si los datos cambiaron, como maximo, y
# solo mientras alguien usa el tablero. Es una consulta de una fila.
REVISION_SEGUNDOS = 60

# Orden total y fijo de los rayos: con los mismos datos, las dos fuentes
# producen las mismas filas en el mismo orden, y por lo tanto las mismas
# respuestas byte a byte (incluidos los empates de distancia del BallTree).
ORDEN = list(COLUMNAS)

# Huella de un dia (o mes, o de todo): cantidad de rayos, cantidad con
# coordenadas, suma de la corriente en decimas de milesima de kA, y sumas
# exactas de latitud y longitud. La corriente va como entero redondeado y no
# como decimal porque convertir un float a decimal da resultados distintos en
# Polars y en Postgres (-8,7 pasa a -8,69999999 en uno y a -8,7 en el otro);
# multiplicar y redondear es la misma operacion IEEE en los dos.
_AGREGADOS_SQL = (
    "count(*), "
    "count(*) FILTER (WHERE latitud IS NOT NULL AND longitud IS NOT NULL), "
    "sum(round(corriente_ka * 10000)::bigint), "
    "sum(latitud), sum(longitud)"
)
_TABLA = "public.gpk_descargas_atmosfericas_gold"
_FILTRO_CAJA_SQL = "latitud BETWEEN %(la0)s AND %(la1)s AND longitud BETWEEN %(lo0)s AND %(lo1)s"


class DatosNoDisponibles(Exception):
    """Modo supabase sin ningun dato traido todavia (la base no respondio al
    arrancar). No hay copia local a la que caer: responder con cero rayos haria
    creer que no cayo ninguno."""


def _huella(n, nc, sc, sla, slo) -> tuple:
    return (int(n), int(nc), int(sc or 0), Decimal(sla or 0), Decimal(slo or 0))


def _sumar(huellas) -> tuple:
    n = nc = sc = 0
    sla = slo = Decimal(0)
    for h in huellas:
        n += h[0]; nc += h[1]; sc += h[2]; sla += h[3]; slo += h[4]
    return (n, nc, sc, sla, slo)


def _mes(dia: date) -> date:
    return dia.replace(day=1)


class RemotoSupabase:
    """Las unicas consultas que el tablero le hace a Supabase."""

    def huella_global(self):
        fila = consultar(f"SELECT max(fecha), {_AGREGADOS_SQL} FROM {_TABLA} WHERE fecha IS NOT NULL")[0]
        return fila[0], _huella(*fila[1:])

    def huellas_por_mes(self) -> dict:
        filas = consultar(
            f"SELECT date_trunc('month', fecha::timestamp)::date, {_AGREGADOS_SQL} FROM {_TABLA} "
            f"WHERE fecha IS NOT NULL GROUP BY 1")
        return {f[0]: _huella(*f[1:]) for f in filas}

    def huellas_por_dia(self, meses: list) -> dict:
        filas = consultar(
            f"SELECT fecha, {_AGREGADOS_SQL} FROM {_TABLA} "
            f"WHERE date_trunc('month', fecha::timestamp)::date = ANY(%(meses)s) GROUP BY fecha",
            {"meses": meses})
        return {f[0]: _huella(*f[1:]) for f in filas}

    def filas_zona(self, dias: list, caja: dict) -> pl.DataFrame:
        filas = consultar(
            f"SELECT fecha, hora::text, latitud::float8, longitud::float8, corriente_ka, polaridad, error_km "
            f"FROM {_TABLA} WHERE fecha = ANY(%(dias)s) AND {_FILTRO_CAJA_SQL}",
            {"dias": dias, **caja})
        return pl.DataFrame(filas, schema=COLUMNAS, orient="row")


class Instantanea:
    """Datos del tablero en un momento dado. No se modifica: cuando cambian los
    datos se arma una nueva y se reemplaza entera."""

    def __init__(self, zona: pl.DataFrame, huellas: dict, version: tuple, sincronizada: bool):
        self.zona = zona.sort(ORDEN, maintain_order=True)
        self.huellas = huellas
        self.version = version
        # True si coincide con Supabase; False si es la copia del parquet
        self.sincronizada = sincronizada
        self.fecha_max = max(huellas) if huellas else None
        self._arbol = None
        self._candado_arbol = threading.Lock()

    @classmethod
    def vacia(cls) -> "Instantanea":
        """Punto de partida del modo supabase: sin rayos ni dias. Su huella
        difiere de cualquier base con datos, asi que la primera sincronizacion
        trae todo."""
        return cls(pl.DataFrame(schema=COLUMNAS), {}, version=None, sincronizada=False)

    def huella_global(self):
        return self.fecha_max, _sumar(self.huellas.values())

    def descargas(self, desde: date | None, hasta: date | None, caja: dict | None) -> pl.DataFrame:
        """Rayos del rango de fechas dentro de la caja. Sin caja no hay rayos."""
        if caja is None:
            return self.zona.clear()
        condicion = (pl.col("Latitud").is_between(caja["la0"], caja["la1"])
                     & pl.col("Longitud").is_between(caja["lo0"], caja["lo1"]))
        if desde is not None:
            condicion &= pl.col("Fecha") >= desde
        if hasta is not None:
            condicion &= pl.col("Fecha") <= hasta
        return self.zona.filter(condicion)

    def total(self, desde: date | None, hasta: date | None, con_coordenadas: bool) -> int:
        """Rayos de toda la region en el rango, no solo los de la zona."""
        i = 1 if con_coordenadas else 0
        return sum(h[i] for d, h in self.huellas.items()
                   if (desde is None or d >= desde) and (hasta is None or d <= hasta))

    def conteo_por_dia(self, desde: date, hasta: date) -> dict:
        return {d.isoformat(): h[0] for d, h in self.huellas.items() if desde <= d <= hasta}

    def comparacion_anios(self, mes: int) -> list:
        por_anio = {}
        for d, h in self.huellas.items():
            if d.month == mes:
                por_anio[d.year] = por_anio.get(d.year, 0) + h[0]
        return [{"anio": a, "total": n} for a, n in sorted(por_anio.items())]

    def dias_con_datos(self) -> list:
        return [d.isoformat() for d in sorted(self.huellas)]

    def arbol(self):
        """(BallTree, fechas) de todos los rayos de la zona, o (None, None)."""
        with self._candado_arbol:
            if self._arbol is None:
                if len(self.zona):
                    coords = np.radians(self.zona.select(["Latitud", "Longitud"]).to_numpy())
                    self._arbol = (BallTree(coords, leaf_size=40, metric="haversine"),
                                   self.zona["Fecha"].to_list())
                else:
                    self._arbol = (None, None)
            return self._arbol


_RAIZ = Path(__file__).resolve().parents[1]


def leer_parquet(ruta: str, caja: dict | None, con_huellas: bool):
    """(zona, huellas por dia) a partir del parquet.

    La lectura corre en un proceso aparte (ver backend/lectura_parquet.py): la
    memoria que usa para recorrer el historico se libera al terminar. Si no se
    puede lanzar el proceso, se lee aca mismo.
    """
    try:
        resultado = subprocess.run(
            [sys.executable, "-m", "backend.lectura_parquet", os.path.abspath(ruta),
             json.dumps(caja), "1" if con_huellas else "0"],
            cwd=_RAIZ, capture_output=True, timeout=300)
        if resultado.returncode != 0:
            error = resultado.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(error[-1] if error else f"codigo de salida {resultado.returncode}")
        salida = resultado.stdout
        tablas = []
        inicio = 0
        for _ in range(2):
            (largo,) = struct.unpack_from("<Q", salida, inicio)
            inicio += 8
            tablas.append(pl.read_ipc(salida[inicio:inicio + largo], memory_map=False))
            inicio += largo
        zona, dias = tablas
    except Exception as e:
        print(f"No se pudo leer el parquet en un proceso aparte ({e}); se lee en este proceso")
        zona, dias = lectura_parquet.leer(ruta, caja, con_huellas)

    if con_huellas:
        huellas = {f: _huella(n, nc, sc, sla, slo) for f, n, nc, sc, sla, slo in dias.iter_rows()}
    else:
        huellas = {f: (n, nc) for f, n, nc in dias.iter_rows()}
    return zona, huellas


class GestorDatos:
    def __init__(self, fuente: str, caja_zona, firma_zona, ruta_parquet: str = ARCHIVO_PARQUET,
                 remoto=None):
        if fuente not in FUENTES:
            raise ValueError(f"FUENTE_DATOS debe ser 'parquet' o 'supabase', no '{fuente}'")
        self.fuente = fuente
        self._caja_zona = caja_zona
        self._firma_zona = firma_zona
        self._ruta = ruta_parquet
        self._remoto = remoto if remoto is not None else (RemotoSupabase() if fuente == "supabase" else None)

        self._inst = None
        self._base = None  # (firma del parquet, firma de la zona) con la que se armo
        self._candado = threading.Lock()
        self._revisando = threading.Lock()
        self._ultima_revision = float("-inf")
        self._ultima_sincronizacion = None
        self._conectado = None

    # ---- lectura ----

    def instantanea(self) -> Instantanea:
        # En modo supabase el parquet no participa: ni existe en la imagen
        firma_parquet = self._firma_parquet() if self.fuente == "parquet" else None
        base = (firma_parquet, self._firma_zona())
        if self._inst is None or base != self._base:
            with self._candado:
                if self._inst is None or base != self._base:
                    self._construir(base)
        elif (self.fuente == "supabase"
              and time.monotonic() - self._ultima_revision > REVISION_SEGUNDOS
              and self._revisando.acquire(blocking=False)):
            # En segundo plano: la peticion responde ya con los datos que hay,
            # y si hubo cambios las siguientes usan la version nueva. Si todavia
            # no hay datos (la base no respondio al arrancar), esto es el
            # reintento.
            threading.Thread(target=self._revisar_en_segundo_plano, daemon=True).start()
        if self.fuente == "supabase" and not self._inst.sincronizada:
            raise DatosNoDisponibles("Sin conexión con la base de datos. Se reintenta en menos de un minuto.")
        return self._inst

    def estado(self) -> dict:
        inst = self._inst
        if inst is None:
            return {"fuente": self.fuente, "estado": "cargando", "fecha_max": None,
                    "ultima_sincronizacion": None, "mensaje": "Cargando datos…"}
        hasta = inst.fecha_max.strftime("%d/%m/%Y") if inst.fecha_max else "—"
        sincronizacion = (self._ultima_sincronizacion.isoformat(timespec="seconds")
                          if self._ultima_sincronizacion else None)
        # Las dos fuentes sanas se nombran y nada mas. El parquet NO es data de
        # prueba ni inventada: son los mismos datos reales que publica el
        # pipeline, solo que congelados en la copia del repositorio en vez de
        # venir en vivo. Llamarlos "de prueba" hacia dudar de un tablero que
        # estaba bien. La fecha del ultimo dato tampoco va aca: se lee en los
        # filtros y en el calendario, y en la etiqueta solo invitaba a leerla
        # como si los datos estuvieran incompletos.
        #
        # En los estados con problema la fecha si se queda: ahi es el dato que
        # importa, porque dice hasta donde alcanza lo que se esta viendo.
        if self.fuente == "parquet":
            estado, mensaje = "local", "Data Local (Parquet)"
        elif not inst.sincronizada:
            # Nunca se trajo nada: no hay fecha que mostrar ni datos que servir
            estado, mensaje = "sin_datos", "Sin conexión con la base de datos · reintentando"
        elif self._conectado:
            estado, mensaje = "ok", "Base de datos (Supabase)"
        else:
            estado, mensaje = "sin_conexion", f"Sin conexión con la base · datos hasta {hasta}"
        return {"fuente": self.fuente, "estado": estado,
                "fecha_max": inst.fecha_max.isoformat() if inst.fecha_max else None,
                "ultima_sincronizacion": sincronizacion, "mensaje": mensaje}

    # ---- construccion y sincronizacion ----

    def _firma_parquet(self):
        try:
            st = os.stat(self._ruta)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _construir(self, base):
        caja = self._caja_zona()
        if self.fuente == "parquet":
            zona, huellas = leer_parquet(self._ruta, caja, con_huellas=False)
            inst = Instantanea(zona, huellas, version=("parquet", base), sincronizada=False)
        else:
            # Se parte de cero y la sincronizacion trae todo lo que falta. Si
            # la base no responde queda vacia y sin sincronizar: instantanea()
            # lo convierte en DatosNoDisponibles y la siguiente revision reintenta.
            inst = self._sincronizar(Instantanea.vacia(), caja)
        self._inst = inst
        self._base = base

    def _revisar_en_segundo_plano(self):
        try:
            # La red va fuera del candado: si Supabase tarda o la conexion se
            # cuelga, las reconstrucciones (por un Excel nuevo, por ejemplo) no
            # quedan esperando detras
            inst = self._inst
            nueva = self._sincronizar(inst, self._caja_zona())
            with self._candado:
                # Si mientras tanto se reconstruyo desde cero, esta revision
                # partio de datos viejos y se descarta
                if self._inst is inst:
                    self._inst = nueva
        except Exception as e:
            print(f"Fallo la revision de cambios en Supabase: {e}")
        finally:
            self._revisando.release()

    def _sincronizar(self, inst: Instantanea, caja: dict | None) -> Instantanea:
        """Devuelve la instantanea igualada con Supabase, o la misma si no hay
        conexion (el tablero lo avisa con la etiqueta de la fuente)."""
        self._ultima_revision = time.monotonic()
        try:
            remota = self._remoto.huella_global()
            if remota != inst.huella_global():
                inst = self._traer_cambios(inst, caja)
                if inst.huella_global() != remota:
                    # Solo si Supabase cambio mientras se sincronizaba; la
                    # proxima revision lo iguala
                    print("Aviso: los datos cambiaron durante la sincronizacion con Supabase")
            elif not inst.sincronizada:
                inst = Instantanea(inst.zona, inst.huellas, ("supabase", remota), sincronizada=True)
        except Exception as e:
            # Cualquier falla (red, Supabase pausado o un error inesperado) deja
            # el tablero con los datos que ya tiene y la etiqueta lo avisa
            if not isinstance(e, SupabaseNoDisponible):
                traceback.print_exc()
            print(f"No se pudo sincronizar con Supabase, se sigue con los datos en memoria: {e}")
            self._conectado = False
            return inst
        self._conectado = True
        self._ultima_sincronizacion = datetime.now()
        return inst

    def _traer_cambios(self, inst: Instantanea, caja: dict | None) -> Instantanea:
        meses_locales = {}
        for d, h in inst.huellas.items():
            meses_locales.setdefault(_mes(d), []).append(h)
        meses_locales = {m: _sumar(hs) for m, hs in meses_locales.items()}
        meses_remotos = self._remoto.huellas_por_mes()
        meses = sorted(m for m in set(meses_locales) | set(meses_remotos)
                       if meses_locales.get(m) != meses_remotos.get(m))

        dias_remotos = self._remoto.huellas_por_dia(meses) if meses else {}
        dias_locales = {d: h for d, h in inst.huellas.items() if _mes(d) in meses}
        distintos = {d for d in set(dias_locales) | set(dias_remotos)
                     if dias_locales.get(d) != dias_remotos.get(d)}
        a_bajar = sorted(d for d in distintos if d in dias_remotos)

        partes = [inst.zona.filter(~pl.col("Fecha").is_in(list(distintos)))]
        if a_bajar and caja is not None:
            partes.append(self._remoto.filas_zona(a_bajar, caja))
        huellas = {d: h for d, h in inst.huellas.items() if d not in distintos}
        huellas.update({d: dias_remotos[d] for d in a_bajar})
        print(f"Sincronizado con Supabase: {len(meses)} meses revisados, {len(a_bajar)} dias bajados, "
              f"{len(distintos) - len(a_bajar)} dias quitados")
        nueva = Instantanea(pl.concat(partes), huellas, version=None, sincronizada=True)
        nueva.version = ("supabase", nueva.huella_global())
        return nueva


_gestor = None


def configurar(caja_zona, firma_zona) -> GestorDatos:
    """Crea el gestor segun FUENTE_DATOS. Falla al arrancar si la configuracion
    es invalida, en vez de mostrar datos de la fuente equivocada."""
    global _gestor
    fuente = os.environ.get("FUENTE_DATOS", "parquet").strip().lower()
    if fuente == "supabase" and not os.environ.get("SUPABASE_DASHBOARD_DB_URL"):
        raise RuntimeError("FUENTE_DATOS=supabase requiere SUPABASE_DASHBOARD_DB_URL")
    _gestor = GestorDatos(fuente, caja_zona, firma_zona)
    return _gestor


def gestor() -> GestorDatos:
    return _gestor
