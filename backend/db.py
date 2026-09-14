"""Conexion de solo lectura a Supabase (Postgres), por el Transaction pooler.

Solo la usa backend/datos.py con FUENTE_DATOS=supabase, para revisar si los
datos cambiaron y bajar los dias nuevos. Las consultas del tablero se responden
desde memoria, asi que aca nunca hay mas de una o dos consultas a la vez.
"""
import os
import threading
import time

import psycopg2
from psycopg2.extensions import QueryCanceledError
from psycopg2.pool import ThreadedConnectionPool

# Conexiones contra el pooler. Solo sincroniza un hilo a la vez, asi que una
# alcanza. Se mantiene abierta entre revisiones: abrir una nueva cada minuto
# (TLS y autenticacion, ~6 KB) gastaria mucho mas egress que la consulta.
MAX_CONEXIONES = 1

# Si Supabase no respondio al crear el pool (por ejemplo, un corte de red justo
# al arrancar), se vuelve a intentar pasado este tiempo. Antes nunca se
# reintentaba: un fallo al arrancar dejaba el tablero sin sincronizar hasta el
# siguiente reinicio de Render.
REINTENTO_POOL_SEGUNDOS = 60

_pool = None
_ultimo_fallo_pool = None
_candado_pool = threading.Lock()

# ThreadedConnectionPool no espera cuando se agotan las conexiones: lanza
# PoolError. Con este semaforo, la consulta numero MAX_CONEXIONES + 1 espera
# turno en vez de fallar y dejar el tablero marcado como sin conexion.
_turnos = threading.BoundedSemaphore(MAX_CONEXIONES)


class SupabaseNoDisponible(Exception):
    """El caller debe capturar esto y seguir con los datos que ya tiene."""


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
            # ThreadedConnectionPool (no SimpleConnectionPool) porque la
            # sincronizacion corre en hilos aparte de las peticiones.
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
            #
            # keepalives: si la red se corta en silencio (sin cerrar la
            # conexion), una consulta en curso esperaria para siempre y la
            # sincronizacion no volveria a correr. Con keepalive el sistema
            # operativo detecta la conexion muerta en ~6 minutos y la consulta
            # falla. Una sonda cada 5 minutos de inactividad no pesa en egress.
            _pool = ThreadedConnectionPool(
                1, MAX_CONEXIONES, dsn,
                connect_timeout=5,
                sslmode="require",
                keepalives=1,
                keepalives_idle=300,
                keepalives_interval=30,
                keepalives_count=3,
            )
            _ultimo_fallo_pool = None
        except Exception as e:
            _ultimo_fallo_pool = time.monotonic()
            print(f"No se pudo crear el pool de conexion a Supabase: {e}")
            return None
        return _pool


def consultar(sql: str, params: tuple | dict = ()) -> list[tuple]:
    """Ejecuta un SELECT contra Supabase y devuelve las filas como tuplas.

    Levanta SupabaseNoDisponible si la variable de entorno no esta configurada,
    si no se pudo conectar, o si la query fallo.
    """
    p = _obtener_pool()
    if p is None:
        raise SupabaseNoDisponible("SUPABASE_DASHBOARD_DB_URL no configurada o conexion no disponible")

    with _turnos:
        # Dos intentos: el pooler cierra conexiones que quedan ociosas, y el
        # pool de psycopg2 no se entera hasta usarlas. Una conexion muerta se
        # descarta y se reintenta una vez con otra nueva, en vez de marcar el
        # tablero sin conexion por una desconexion que no es una caida.
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
