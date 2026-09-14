"""Conexion de solo lectura a Supabase (Postgres), por el Transaction pooler.

Migracion incremental del dashboard: mientras se valida endpoint por endpoint,
cada uno intenta leer de aca primero y cae al parquet local si esto falla (ver
SupabaseNoDisponible y su uso en backend/main.py). Asi el dashboard sigue
funcionando igual que hoy mientras SUPABASE_DASHBOARD_DB_URL no este
configurada, y no se rompe si Supabase esta pausado o el pooler no responde.
"""
import os
import threading
import time

import psycopg2
from psycopg2.extensions import QueryCanceledError
from psycopg2.pool import ThreadedConnectionPool

# Conexiones simultaneas del dashboard contra el pooler. Los endpoints corren en
# hilos (uno por peticion), asi que este numero es cuantas consultas pueden
# estar en vuelo a la vez. El Transaction pooler las multiplexa sobre pocas
# conexiones reales de Postgres, asi que no compite con el limite de 60 del
# plan Nano.
MAX_CONEXIONES = 10

# Si Supabase no respondio al crear el pool (por ejemplo, un corte de red justo
# al arrancar), se vuelve a intentar pasado este tiempo. Antes nunca se
# reintentaba: un fallo al arrancar dejaba el tablero leyendo el parquet viejo
# hasta el siguiente reinicio de Render.
REINTENTO_POOL_SEGUNDOS = 60

_pool = None
_ultimo_fallo_pool = None
_candado_pool = threading.Lock()

# ThreadedConnectionPool no espera cuando se agotan las conexiones: lanza
# PoolError. Sin este semaforo, la consulta numero MAX_CONEXIONES + 1 fallaria y
# el fallback la mandaria en silencio al parquet desactualizado. Con el semaforo
# simplemente espera turno.
_turnos = threading.BoundedSemaphore(MAX_CONEXIONES)


class SupabaseNoDisponible(Exception):
    """El caller debe capturar esto y usar el parquet local como respaldo."""


def _obtener_pool():
    global _pool, _ultimo_fallo_pool
    if _pool is not None:
        return _pool
    with _candado_pool:
        if _pool is not None:
            return _pool
        dsn = os.environ.get("SUPABASE_DASHBOARD_DB_URL")
        if not dsn:
            return None
        if _ultimo_fallo_pool is not None and time.monotonic() - _ultimo_fallo_pool < REINTENTO_POOL_SEGUNDOS:
            return None
        try:
            # ThreadedConnectionPool (no SimpleConnectionPool) porque cada
            # peticion corre en su propio hilo.
            #
            # sslmode=require: sin esto, psycopg2 usa "prefer" por defecto, que
            # intenta cifrar pero cae a texto plano en silencio si la
            # negociacion TLS falla. Verificado contra este mismo pooler: acepta
            # conexiones sin cifrar si el cliente no exige TLS explicitamente.
            # "require" no tolera ese downgrade: si el servidor no puede cifrar,
            # la conexion falla en vez de mandar la contraseña y las queries en
            # claro.
            #
            # No hace falta fijar statement_timeout aca: Supabase ya aplica 2 min
            # por defecto a nivel de proyecto (verificado con SHOW
            # statement_timeout), y pasarlo via "options" en el connect no tiene
            # efecto a traves del Transaction pooler (las conexiones fisicas se
            # comparten entre clientes, asi que el SET de arranque no persiste).
            # Si se quiere un tope mas ajustado, va con ALTER ROLE
            # dashboard_readonly SET statement_timeout = '15s' en el SQL Editor.
            _pool = ThreadedConnectionPool(
                1, MAX_CONEXIONES, dsn,
                connect_timeout=5,
                sslmode="require",
            )
            # El pool de psycopg2 cierra toda conexion por encima de minconn
            # cuando se devuelve. Con minconn=1, cada rafaga de usuarios volvia a
            # abrir conexiones (TLS + autenticacion, ~1 s cada una y en la
            # practica de a una): 10 consultas simultaneas tardaban 8 s. Subirlo
            # despues de crear el pool hace que conserve las que ya abrio sin
            # abrir 10 de golpe al arrancar. Medido: las mismas 10 consultas
            # bajan a 0,6 s.
            _pool.minconn = MAX_CONEXIONES
            _ultimo_fallo_pool = None
        except Exception as e:
            _ultimo_fallo_pool = time.monotonic()
            print(f"No se pudo crear el pool de conexion a Supabase: {e}")
            return None
        return _pool


def precalentar(cantidad: int) -> None:
    """Abre conexiones por adelantado para que la primera carga del tablero no
    pague la apertura. Nunca toma mas turnos de los libres: si ya hay consultas
    en curso, no les quita conexiones."""
    p = _obtener_pool()
    if p is None:
        return
    tomadas = []
    try:
        for _ in range(cantidad):
            if not _turnos.acquire(blocking=False):
                break
            try:
                tomadas.append(p.getconn())
            except Exception:
                _turnos.release()
                raise
    except Exception as e:
        print(f"No se pudieron abrir conexiones por adelantado: {e}")
    finally:
        for conn in tomadas:
            p.putconn(conn)
            _turnos.release()


def consultar(sql: str, params: tuple | dict = ()) -> list[tuple]:
    """Ejecuta un SELECT contra Supabase y devuelve las filas como tuplas.

    Levanta SupabaseNoDisponible si la variable de entorno no esta configurada,
    si no se pudo conectar, o si la query fallo — en cualquiera de esos casos
    el caller debe caer al parquet local.
    """
    p = _obtener_pool()
    if p is None:
        raise SupabaseNoDisponible("SUPABASE_DASHBOARD_DB_URL no configurada o conexion no disponible")

    with _turnos:
        # Dos intentos: el pooler cierra conexiones que quedan ociosas, y el
        # pool de psycopg2 no se entera hasta usarlas. Una conexion muerta se
        # descarta y se reintenta una vez con otra nueva, en vez de mandar esa
        # peticion al parquet viejo por una desconexion que no es una caida.
        for intento in (1, 2):
            conn = None
            try:
                conn = p.getconn()
                # Solo hay SELECTs: sin autocommit, psycopg2 abre una
                # transaccion antes de cada consulta y el pool la cierra con un
                # rollback al devolverla. Son dos viajes de red de mas por
                # consulta que no aportan nada.
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    filas = cur.fetchall()
                p.putconn(conn)
                return filas
            except QueryCanceledError as e:
                # statement_timeout: reintentar solo duplicaria la espera
                p.putconn(conn)
                raise SupabaseNoDisponible(str(e)) from e
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                if conn is not None:
                    p.putconn(conn, close=True)
                if intento == 2:
                    raise SupabaseNoDisponible(str(e)) from e
            except Exception as e:
                if conn is not None:
                    p.putconn(conn)
                raise SupabaseNoDisponible(str(e)) from e
