-- =============================================================================
-- 001 - Foto de la cache del tablero
--
-- QUE ES
-- El tablero responde desde memoria, pero Render Free la borra cada vez que el
-- servicio se duerme o se reinicia. Para no pedirle a Supabase las ~38 mil
-- filas de la zona en cada arranque (~3,4 MB de egress), guarda aqui una foto
-- comprimida de su cache (~0,6 MB) y la lee al despertar. Ver backend/foto.py.
--
-- COMO SE APLICA
-- Una sola vez, con el usuario postgres, en el SQL Editor de Supabase. Es
-- idempotente: correrlo de nuevo no rompe nada ni borra la foto.
-- Requiere que ya exista el rol dashboard_readonly (el que usa el tablero).
--
-- SEGURIDAD: EL TABLERO SIGUE SIN PODER ESCRIBIR
-- dashboard_readonly NO recibe INSERT ni UPDATE sobre la tabla. Lo unico que
-- puede hacer es ejecutar guardar_foto_tablero(), que corre con los permisos de
-- su duenio (SECURITY DEFINER) y solo sabe reemplazar la unica fila de esta
-- tabla. No puede tocar el Gold ni ninguna otra cosa.
--
-- Si el tablero arranca antes de que se aplique este script, funciona igual:
-- no encuentra la tabla, lo registra y arma la cache desde las filas.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dashboard_readonly') THEN
        RAISE EXCEPTION 'No existe el rol dashboard_readonly: crear primero el usuario del tablero';
    END IF;
END $$;

-- Una sola fila (id = 1): cada foto nueva reemplaza a la anterior.
CREATE TABLE IF NOT EXISTS public.gpk_tablero_foto (
    id              smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    formato         smallint    NOT NULL,
    caja            jsonb,                      -- zona con que se filtraron los rayos
    huella_global   text        NOT NULL,       -- huella de los datos de la foto
    bytes           integer     NOT NULL,
    generada_en     timestamptz NOT NULL DEFAULT now(),
    foto            bytea       NOT NULL
);

COMMENT ON TABLE public.gpk_tablero_foto IS
    'Foto comprimida de la cache del tablero (backend/foto.py). La escribe el tablero con guardar_foto_tablero(); se puede borrar sin perder datos: el tablero la vuelve a armar.';

-- Tope de tamanio: hoy pesa ~0,45 MB. Un valor absurdo delata un error y no
-- debe llenar los 500 MB del plan gratuito.
CREATE OR REPLACE FUNCTION public.guardar_foto_tablero(
    p_formato   smallint,
    p_caja      jsonb,
    p_huella    text,
    p_foto      bytea
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
-- search_path fijo: sin esto, quien ejecuta podria hacer que la funcion use
-- objetos suyos con el mismo nombre (riesgo clasico de SECURITY DEFINER).
SET search_path = public, pg_temp
AS $$
BEGIN
    IF octet_length(p_foto) > 20 * 1024 * 1024 THEN
        RAISE EXCEPTION 'Foto de % bytes: supera el tope de 20 MB', octet_length(p_foto);
    END IF;

    INSERT INTO public.gpk_tablero_foto (id, formato, caja, huella_global, bytes, generada_en, foto)
    VALUES (1, p_formato, p_caja, p_huella, octet_length(p_foto), now(), p_foto)
    ON CONFLICT (id) DO UPDATE
        SET formato       = EXCLUDED.formato,
            caja          = EXCLUDED.caja,
            huella_global = EXCLUDED.huella_global,
            bytes         = EXCLUDED.bytes,
            generada_en   = EXCLUDED.generada_en,
            foto          = EXCLUDED.foto;
END $$;

-- Permisos minimos --------------------------------------------------------------

-- Por defecto Postgres deja ejecutar funciones nuevas a cualquiera: se cierra.
REVOKE ALL ON FUNCTION public.guardar_foto_tablero(smallint, jsonb, text, bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.guardar_foto_tablero(smallint, jsonb, text, bytea) TO dashboard_readonly;

REVOKE ALL ON public.gpk_tablero_foto FROM PUBLIC;
GRANT SELECT ON public.gpk_tablero_foto TO dashboard_readonly;

-- RLS activado como en las demas tablas. Con RLS y sin politica, un rol ve CERO
-- filas sin dar error: el tablero creeria que no hay foto y la rearmaria en cada
-- arranque. Por eso la politica de lectura explicita para dashboard_readonly.
ALTER TABLE public.gpk_tablero_foto ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tablero_lee_foto ON public.gpk_tablero_foto;
CREATE POLICY tablero_lee_foto ON public.gpk_tablero_foto
    FOR SELECT TO dashboard_readonly USING (true);

-- Cerrado a la API publica de Supabase (anon y authenticated). En un Postgres
-- local esos roles no existen: se revoca solo a los que haya.
DO $$
DECLARE
    r text;
BEGIN
    FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
        EXECUTE format('REVOKE ALL ON public.gpk_tablero_foto FROM %I', r);
        EXECUTE format('REVOKE ALL ON FUNCTION public.guardar_foto_tablero(smallint, jsonb, text, bytea) FROM %I', r);
    END LOOP;
END $$;
