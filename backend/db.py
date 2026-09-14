"""Conexion de solo lectura a Supabase (Postgres), por el Transaction pooler.

Migracion incremental del dashboard: mientras se valida endpoint por endpoint,
cada uno intenta leer de aca primero y cae al parquet local si esto falla (ver
SupabaseNoDisponible y su uso en backend/main.py). Asi el dashboard sigue
funcionando igual que hoy mientras SUPABASE_DASHBOARD_DB_URL no este
configurada, y no se rompe si Supabase esta pausado o el pooler no responde.
"""
import os

import psycopg2
from psycopg2 import pool

_pool = None
_pool_intentado = False


class SupabaseNoDisponible(Exception):
    """El caller debe capturar esto y usar el parquet local como respaldo."""


def _obtener_pool():
    global _pool, _pool_intentado
    if _pool is not None:
        return _pool
    if _pool_intentado:
        # Ya se intento antes y fallo (o no esta configurada la variable): no
        # tiene sentido reintentar la conexion en cada request
        return None
    _pool_intentado = True

    dsn = os.environ.get("SUPABASE_DASHBOARD_DB_URL")
    if not dsn:
        return None
    try:
        # Pool chico: el Transaction pooler ya multiplexa del lado de Supabase,
        # y el plan Nano solo admite 60 conexiones en total, compartidas con el
        # pipeline
        _pool = psycopg2.pool.SimpleConnectionPool(1, 3, dsn, connect_timeout=5)
    except Exception as e:
        print(f"No se pudo crear el pool de conexion a Supabase: {e}")
        return None
    return _pool


def consultar(sql: str, params: tuple = ()) -> list[tuple]:
    """Ejecuta un SELECT contra Supabase y devuelve las filas como tuplas.

    Levanta SupabaseNoDisponible si la variable de entorno no esta configurada,
    si no se pudo conectar, o si la query fallo — en cualquiera de esos casos
    el caller debe caer al parquet local.
    """
    p = _obtener_pool()
    if p is None:
        raise SupabaseNoDisponible("SUPABASE_DASHBOARD_DB_URL no configurada o conexion no disponible")

    conn = None
    try:
        conn = p.getconn()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:
        raise SupabaseNoDisponible(str(e)) from e
    finally:
        if conn is not None:
            p.putconn(conn)
