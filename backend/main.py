from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import polars as pl
import numpy as np
from sklearn.neighbors import BallTree
import calendar
import io
import json
import math
import os
import traceback
from datetime import date, datetime
import pandas as pd
import uvicorn

from backend.informe import construir_informe

app = FastAPI(title="App Descargas Atmosféricas 2026")

# Fuentes de datos. El maestro manda la jerarquia de filtros y el inventario
# las estructuras; se cruzan por circuito (ver /api/procesar)
ARCHIVO_LOCALIZACIONES = "Localizaciones_Final.xlsx"
ARCHIVO_INVENTARIO = "Inventario_Estructuras_y_DPS_Final.xlsx"

# Tope del radio de busqueda.
#
# No es una preferencia de diseno: es un limite medido. query_radius devuelve,
# por cada una de las ~759 estructuras, el arreglo con TODOS los rayos que caen
# dentro del radio. El costo crece con el area, o sea con el cuadrado del radio.
# Contra el historial completo (779.109 descargas), en un contenedor con los
# 512 MB del plan Free de Render:
#
#   radio     rayos devueltos   memoria   resultado
#   1.000 m           5.309     199 MB    ok, 1,9 s
#   2.000 m           9.683     236 MB    ok, 2,1 s
#   5.000 m          23.717     222 MB    ok, 3,4 s
#  10.000 m               -          -    el proceso muere (OOM, exit 137)
#
# Un radio de 10 km tumbaba el servicio entero, no solo esa peticion. El uso
# real del tablero va entre 100 y 1.000 m, asi que 5.000 m deja muchisimo margen
# operativo y evita que un valor mal tecleado deje el tablero caido.
RADIO_MAXIMO_METROS = 5000.0


def validar_radio(radio_busqueda_metros: float) -> float:
    """Rechaza radios fuera de rango antes de tocar el arbol espacial."""
    if radio_busqueda_metros <= 0:
        raise HTTPException(
            status_code=400,
            detail="El radio de búsqueda debe ser mayor que cero.",
        )
    if radio_busqueda_metros > RADIO_MAXIMO_METROS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"El radio máximo permitido es de {RADIO_MAXIMO_METROS:,.0f} m. "
                "Un radio mayor agota la memoria del servidor y deja el tablero "
                "fuera de servicio."
            ).replace(",", "."),
        )
    return radio_busqueda_metros

# Montar frontend estático
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def sin_cache_en_frontend(request, call_next):
    """Impide que el navegador se quede con una version vieja del frontend.

    El volumen de Docker y el --reload de uvicorn dejan el codigo nuevo en el
    servidor al instante, pero el navegador seguia sirviendo el index.html y el
    script.js de su cache. El sintoma es el peor posible: mitad viejo y mitad
    nuevo, que parece un bug de logica y no un problema de cache.
    """
    respuesta = await call_next(request)
    ruta = request.url.path
    if ruta == "/" or ruta.startswith("/static/"):
        respuesta.headers["Cache-Control"] = "no-store, must-revalidate"
    return respuesta


@app.get("/")
def read_root():
    """Sirve el index con una version pegada a cada asset propio.

    El no-store de arriba solo evita que se cachee de ahora en mas: lo que el
    navegador ya guardo lo sigue usando sin preguntar. Colgarle a cada archivo
    un ?v= con su fecha de modificacion cambia la URL en cuanto se edita, y una
    URL nueva no puede tener copia vieja en cache.
    """
    html = open("static/index.html", encoding="utf-8").read()
    for asset in ("script.js", "styles.css"):
        try:
            version = int(os.path.getmtime(f"static/{asset}"))
        except OSError:
            continue
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(html)

def clean_lat(coord_series: pl.Series) -> pl.Series:
    def fix_lat(val):
        if val is None: return None
        val = str(val).strip()
        if not val: return None
        digits = ''.join(c for c in val if c.isdigit())
        if not digits: return None
        # Siempre debe empezar con 4 según el usuario
        if digits.startswith("4"):
            return float(f"4.{digits[1:]}")
        return float(f"{digits[0]}.{digits[1:]}")
    return coord_series.map_elements(fix_lat, return_dtype=pl.Float64)

def clean_lon(coord_series: pl.Series) -> pl.Series:
    def fix_lon(val):
        if val is None: return None
        val = str(val).strip()
        if not val: return None
        digits = ''.join(c for c in val if c.isdigit())
        if not digits: return None
        # Siempre debe empezar con -72 según el usuario
        if digits.startswith("72"):
            return float(f"-72.{digits[2:]}")
        elif digits.startswith("7") and len(digits) > 1:
            # Si alguien escribió 7.algo pero debia ser 72
            # asume -72.xxx
            return float(f"-72.{digits[1:]}")
        return float(f"-72.{digits}")
    return coord_series.map_elements(fix_lon, return_dtype=pl.Float64)

# Campos del inventario que se muestran en el panel de detalle, agrupados por
# tema. Se listan explicitamente porque el inventario trae ~40 columnas y
# volcarlas crudas seria ilegible; las que no existan en el Excel se saltan.
GRUPOS_DETALLE = [
    ("Identificación", ["Circuito_Corregido", "Area", "NombreResponsable", "FechaHora"]),
    ("Apoyo", ["Apoyo_Tipo", "Apoyo_Subtipo", "Configuracion", "Disposicion"]),
    ("Aisladores", ["Aislador_Tipo", "Aislador_A", "Aislador_B", "Aislador_C"]),
    ("Protección", ["DSD", "DPS", "DPS_A", "DPS_B", "DPS_C", "Cable_De_Guarda"]),
    ("Equipos", ["Seccionador", "Reconectador", "Linepost", "Transformador",
                 "PT", "CT", "Fusible", "Mufa", "Enrollamiento_Cable"]),
    ("Puesta a tierra", ["SPT_Bajante", "SPT_Conexion", "SPT_Cantidad", "SPT_Estado",
                         "ResistenciaPlaca", "PesoPlaca"]),
    ("Afloramiento", ["Afloramiento", "Conectores_Afloramiento", "Tuberia_Afloramiento", "Cable"]),
    ("Observaciones", ["Templete_Observaciones", "Observaciones_Equipos", "Herrajes_Accesorios"]),
]

# Etiquetas que no se leen bien con solo cambiar guiones bajos por espacios
ETIQUETAS_DETALLE = {
    "Circuito_Corregido": "Circuito",
    "NombreResponsable": "Responsable",
    "FechaHora": "Fecha de inspección",
    "Apoyo_Tipo": "Tipo de apoyo",
    "Apoyo_Subtipo": "Subtipo de apoyo",
    "Aislador_Tipo": "Tipo de aislador",
    "Cable_De_Guarda": "Cable de guarda",
    "SPT_Bajante": "Bajante SPT",
    "SPT_Conexion": "Conexión SPT",
    "SPT_Cantidad": "Cantidad SPT",
    "SPT_Estado": "Estado SPT",
    "ResistenciaPlaca": "Resistencia de placa",
    "PesoPlaca": "Peso de placa",
    "Conectores_Afloramiento": "Conectores de afloramiento",
    "Tuberia_Afloramiento": "Tubería de afloramiento",
    "Enrollamiento_Cable": "Enrollamiento de cable",
    "Templete_Observaciones": "Templete",
    "Observaciones_Equipos": "Equipos",
    "Herrajes_Accesorios": "Herrajes y accesorios",
}


def _texto(valor) -> str:
    """Normaliza una celda de Excel a texto plano, o cadena vacía si viene nula.

    Excel devuelve los enteros como float ("4.0") y las fechas como timestamp
    completo; ambos se leen mal en el panel de detalle, así que se limpian acá.
    """
    if valor is None:
        return ""
    try:
        if pd.isna(valor):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(valor, (pd.Timestamp, datetime)):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def _segundos_desde_medianoche(hora) -> int | None:
    """'HH:MM:SS' -> segundos desde las 00:00. None si no se puede leer.

    Es la coordenada temporal que usa el simulador para ubicar cada rayo en la
    barra de tiempo del dia (0 = 00:00:00, 86399 = 23:59:59).
    """
    txt = _texto(hora)
    if not txt:
        return None
    partes = txt.split(":")
    try:
        h = int(partes[0])
        m = int(partes[1]) if len(partes) > 1 else 0
        s = int(float(partes[2])) if len(partes) > 2 else 0
        if 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60:
            return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return None
    return None


# Arbol espacial de TODAS las descargas del historico, cacheado en memoria. Lo
# usa el marcado del calendario del sidebar (dias con impacto dentro del radio):
# construirlo una sola vez hace que un cambio de filtro o de radio solo tenga
# que re-consultar las estructuras, que es rapido. Se reconstruye si el parquet
# cambia de fecha de modificacion.
_arbol_rayos_cache = None
_arbol_rayos_mtime = None


def _arbol_todos_los_rayos():
    """Devuelve (BallTree, lista_de_fechas) alineados, o (None, None) si vacio."""
    global _arbol_rayos_cache, _arbol_rayos_mtime
    archivo = "Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
    try:
        mtime = os.path.getmtime(archivo)
    except OSError:
        return None, None
    if _arbol_rayos_cache is None or mtime != _arbol_rayos_mtime:
        schema = pl.read_parquet_schema(archivo)
        cols = [c for c in ["Fecha", "Latitud", "Longitud"] if c in schema]
        df = pl.read_parquet(archivo, columns=cols)
        limpio = df.with_columns([
            pl.col("Latitud").cast(pl.Float64).alias("la"),
            pl.col("Longitud").cast(pl.Float64).alias("lo"),
        ]).drop_nulls(subset=["la", "lo"])
        if not len(limpio):
            _arbol_rayos_cache = (None, None)
        else:
            fechas = limpio["Fecha"].to_list()
            arbol = BallTree(np.radians(limpio.select(["la", "lo"]).to_numpy()),
                             leaf_size=40, metric="haversine")
            _arbol_rayos_cache = (arbol, fechas)
        _arbol_rayos_mtime = mtime
    return _arbol_rayos_cache


def detalle_desde_fila(fila: dict) -> list:
    """Arma el panel de detalle de una estructura a partir de su fila cruda del
    inventario. Los campos vacios se omiten: un panel lleno de "nan" no informa."""
    grupos = []
    for titulo, columnas in GRUPOS_DETALLE:
        campos = []
        for col in columnas:
            if col not in fila:
                continue
            valor = _texto(fila[col])
            if not valor or valor.lower() in ("nan", "nat", "none"):
                continue
            campos.append({
                "etiqueta": ETIQUETAS_DETALLE.get(col, col.replace("_", " ")),
                "valor": valor
            })
        if campos:
            grupos.append({"grupo": titulo, "campos": campos})
    return grupos


# El catalogo cruza inventario y maestro una sola vez, porque releer los dos
# Excel en cada peticion es caro. La firma son las fechas de modificacion de los
# archivos: si alguien corrige el maestro, el catalogo se reconstruye solo en
# vez de servir datos viejos hasta el proximo reinicio.
_cache_catalogo = None
_firma_catalogo = None


def _firma_archivos():
    firma = []
    for ruta in (ARCHIVO_LOCALIZACIONES, ARCHIVO_INVENTARIO):
        try:
            firma.append(os.path.getmtime(ruta))
        except OSError:
            firma.append(None)
    return tuple(firma)


def es_portico(fila):
    """Un portico de derivacion se reconoce por Apoyo_Tipo, no por su tag.

    Importa porque las dos clases de fila usan las mismas columnas con distinto
    significado (ver `identidad_estructura`).
    """
    return _texto(fila.get("Apoyo_Tipo")).lower() == "pórtico"


def identidad_estructura(fila, col_tag, col_circuito):
    """Devuelve (id_unico, tag_visible, locacion_o_circuito) de una fila.

    El inventario guarda dos clases de fila con las mismas columnas:

    - Poste:   Circuito_Corregido = circuito ("JCB - TIE")
               Estructura_Tag_Corregido = su tag propio, ya unico
    - Portico: Circuito_Corregido = la LOCACION ("JCP")
               Estructura_Tag_Corregido = el PORTICO ("PORT"), repetido 21 veces

    Por eso el portico no puede identificarse por su tag: lo que lo hace unico
    es el par locacion + portico, que es justamente la llave con la que figura
    en el maestro.
    """
    tag = _texto(fila.get(col_tag))
    circuito = _texto(fila.get(col_circuito))
    if es_portico(fila):
        return f"{circuito}␟{tag}", tag, circuito
    return tag, tag, circuito


def construir_catalogo():
    """Cruza el inventario de estructuras con el maestro de localizaciones y
    devuelve la jerarquia completa mas el catalogo de estructuras.

    Para los postes la llave es el circuito: "PORTICO / SWG / TRAMO" del maestro
    se corresponde con "Circuito_Corregido" del inventario. Para los porticos de
    derivacion la llave es el par (locacion, portico), porque su tag se repite.

    Como cada estructura pertenece a un solo circuito, cada circuito a una sola
    locacion y cada locacion a un solo campo, la jerarquia se puede recorrer en
    los dos sentidos sin ambiguedad: de lo general a lo especifico y al reves.
    """
    global _cache_catalogo, _firma_catalogo
    firma = _firma_archivos()
    if _cache_catalogo is not None and _firma_catalogo == firma:
        return _cache_catalogo

    df_loc = pd.read_excel(ARCHIVO_LOCALIZACIONES)
    df_loc = df_loc.dropna(subset=['CAMPO', 'LOCACION / CIRCUITO', 'PORTICO / SWG / TRAMO'])
    # Filtro solicitado por usuario: CLASIF2 == "CIRCUITOS"
    df_loc = df_loc[df_loc['CLASIF2'].astype(str).str.strip().str.upper() == "CIRCUITOS"]

    jerarquia = {}
    # portico -> (campo, locacion): es lo que permite subir por la jerarquia
    ubicacion_por_portico = {}
    # (locacion, portico) -> campo: la llave de los porticos de derivacion, que
    # comparten el nombre "PORT" entre locaciones distintas
    campo_por_par = {}
    for _, row in df_loc.iterrows():
        campo = _texto(row['CAMPO'])
        locacion = _texto(row['LOCACION / CIRCUITO'])
        portico = _texto(row['PORTICO / SWG / TRAMO'])

        jerarquia.setdefault(campo, {}).setdefault(locacion, [])
        if portico not in jerarquia[campo][locacion]:
            jerarquia[campo][locacion].append(portico)
        # Solo el primero gana: "PORT" aparece en 20 locaciones distintas y este
        # diccionario solo sirve para los circuitos, que si son unicos
        ubicacion_por_portico.setdefault(portico, (campo, locacion))
        campo_por_par[(locacion, portico)] = campo

    for campo in jerarquia:
        for loc in jerarquia[campo]:
            jerarquia[campo][loc].sort()

    df_inv = pd.read_excel(ARCHIVO_INVENTARIO)
    col_tag = "Estructura_Tag_Corregido" if "Estructura_Tag_Corregido" in df_inv.columns else "Estructura_Tag"
    col_circuito = "Circuito_Corregido" if "Circuito_Corregido" in df_inv.columns else "Circuito"

    estructuras = []
    vistos = set()
    # Circuitos que estan en el inventario pero no en el maestro: sus estructuras
    # existen y hay que poder filtrarlas, pero no se les puede deducir campo ni
    # locacion. Se marcan para avisar en la UI en vez de esconderlas.
    sin_asignar = {}
    for _, row in df_inv.iterrows():
        ident, tag, circuito = identidad_estructura(row, col_tag, col_circuito)
        if not tag or ident in vistos:
            continue
        vistos.add(ident)

        if es_portico(row):
            # El portico ya trae su locacion en la columna de circuito; lo unico
            # que falta deducir es el campo, por el par contra el maestro
            locacion = circuito
            portico = tag
            campo = campo_por_par.get((locacion, portico), "")
        else:
            campo, locacion = ubicacion_por_portico.get(circuito, ("", ""))
            portico = circuito

        if portico and not campo:
            sin_asignar[portico] = sin_asignar.get(portico, 0) + 1

        estructuras.append({
            "id": ident,
            "tag": tag,
            "portico": portico,
            "locacion": locacion,
            "campo": campo,
            "es_portico": es_portico(row)
        })

    estructuras.sort(key=lambda e: (e["tag"], e["locacion"]))

    _cache_catalogo = {
        "jerarquia": jerarquia,
        "estructuras": estructuras,
        "sin_asignar": [{"portico": c, "estructuras": n} for c, n in sorted(sin_asignar.items())]
    }
    _firma_catalogo = firma
    return _cache_catalogo


def preparar_postes(filtro_campo="", filtro_locacion="", filtro_portico="",
                    filtro_estructura="", filtro_proteccion=""):
    """Lee el inventario y devuelve las estructuras que pasan los filtros.

    Vive fuera de /api/procesar porque el calendario necesita exactamente el
    mismo recorte: si cada endpoint filtrara por su cuenta, terminarian
    describiendo universos distintos sin que nadie lo note.
    """
    try:
        # Se guarda el inventario crudo aparte: el recorte deja unas pocas
        # columnas y el panel de detalle necesita la fila entera
        df_inv_crudo = pd.read_excel(ARCHIVO_INVENTARIO)
        df_postes = pl.from_pandas(df_inv_crudo)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo de postes local: {str(e)}")

    lat_poste_col, lon_poste_col = "Latitud_Manual", "Longitud_Manual"
    id_poste_col = "Estructura_Tag_Corregido" if "Estructura_Tag_Corregido" in df_postes.columns else "Estructura_Tag"
    if id_poste_col not in df_postes.columns:
        id_poste_col = df_postes.columns[0]  # Fallback
    circuito_col = "Circuito_Corregido" if "Circuito_Corregido" in df_postes.columns else "Circuito"

    if lat_poste_col not in df_postes.columns or lon_poste_col not in df_postes.columns:
        raise HTTPException(status_code=400, detail="Faltan columnas de lat/lon en postes")

    cols_to_keep = [lat_poste_col, lon_poste_col, id_poste_col]
    if circuito_col in df_postes.columns: cols_to_keep.append(circuito_col)
    if "Apoyo_Tipo" in df_postes.columns: cols_to_keep.append("Apoyo_Tipo")
    if "DSD" in df_postes.columns: cols_to_keep.append("DSD")
    if "DPS" in df_postes.columns: cols_to_keep.append("DPS")
    elif "DPS_Pararrayos" in df_postes.columns: cols_to_keep.append("DPS_Pararrayos")

    df_postes = df_postes.select(cols_to_keep).with_columns([
        clean_lat(pl.col(lat_poste_col)).alias("lat_clean"),
        clean_lon(pl.col(lon_poste_col)).alias("lon_clean")
    ]).filter(
        pl.col("lat_clean").is_not_null() & pl.col("lon_clean").is_not_null()
    )

    dps_col = "DPS" if "DPS" in df_postes.columns else ("DPS_Pararrayos" if "DPS_Pararrayos" in df_postes.columns else None)

    # Identidad unica de cada fila, con la misma regla que construir_catalogo:
    # el tag basta para un poste, pero un portico necesita su locacion porque
    # comparte el nombre "PORT" con otros veinte. Se calcula una sola vez y
    # todos los filtros trabajan sobre ella, para que ninguno vuelva a suponer
    # que "Circuito_Corregido" siempre significa lo mismo.
    tag_limpio = pl.col(id_poste_col).cast(pl.Utf8).str.strip_chars()
    if "Apoyo_Tipo" in df_postes.columns and circuito_col in df_postes.columns:
        loc_limpia = pl.col(circuito_col).cast(pl.Utf8).str.strip_chars()
        df_postes = df_postes.with_columns(
            pl.when(
                pl.col("Apoyo_Tipo").cast(pl.Utf8).str.strip_chars().str.to_lowercase() == "pórtico"
            ).then(loc_limpia + pl.lit("␟") + tag_limpio)
             .otherwise(tag_limpio)
             .alias("id_estructura")
        )
    else:
        df_postes = df_postes.with_columns(tag_limpio.alias("id_estructura"))

    # Filtros de ubicacion. Los cuatro niveles (campo, locacion, portico y
    # estructura) son independientes entre si: el frontend los mantiene
    # coherentes, y aca se aplican como condiciones AND en cualquier
    # combinacion. Asi da igual si el usuario empezo por el campo o por la
    # estructura, que es justo lo que pide la jerarquia bidireccional.
    if filtro_campo or filtro_locacion or filtro_portico:
        if circuito_col not in df_postes.columns:
            raise HTTPException(
                status_code=400,
                detail=f"El inventario no tiene la columna '{circuito_col}', necesaria para filtrar."
            )
        try:
            catalogo = construir_catalogo()
            # Se parte del catalogo ya cruzado y se recorta por los niveles que
            # vengan seteados. Lo que queda son identidades y no circuitos: en
            # un portico el circuito no lo identifica, porque esa columna
            # guarda su locacion.
            ids_validos = sorted({
                e["id"] for e in catalogo["estructuras"]
                if (not filtro_campo or e["campo"] == filtro_campo)
                and (not filtro_locacion or e["locacion"] == filtro_locacion)
                and (not filtro_portico or e["portico"] == filtro_portico)
            })
            df_postes = df_postes.filter(pl.col("id_estructura").is_in(ids_validos))
        except HTTPException:
            raise
        except Exception as e:
            # Antes esto solo se imprimia: el filtro fallaba en silencio y el
            # usuario recibia el universo completo creyendo haber filtrado
            raise HTTPException(status_code=400, detail=f"No se pudieron aplicar los filtros de ubicación: {str(e)}")

    # La estructura especifica se filtra contra el propio inventario, sin pasar
    # por el maestro: por eso funciona incluso para las estructuras cuyo
    # circuito no tiene campo ni locacion asignados
    detalle_estructura = None
    if filtro_estructura:
        # El identificador puede ser el tag de un poste o el par
        # "locacion␟portico" de un portico de derivacion, cuyo tag se repite
        buscado = filtro_estructura.strip()
        df_postes = df_postes.filter(pl.col("id_estructura") == buscado)

        if "␟" in buscado:
            loc_buscada, tag_buscado = buscado.split("␟", 1)
            filas = df_inv_crudo[
                (df_inv_crudo[id_poste_col].astype(str).str.strip() == tag_buscado)
                & (df_inv_crudo[circuito_col].astype(str).str.strip() == loc_buscada)
            ]
        else:
            tag_buscado = buscado
            filas = df_inv_crudo[
                df_inv_crudo[id_poste_col].astype(str).str.strip() == tag_buscado
            ]

        if len(filas) > 0:
            detalle_estructura = {
                "tag": tag_buscado,
                "grupos": detalle_desde_fila(filas.iloc[0].to_dict()),
                # El inventario tiene un tag repetido con coordenadas distintas;
                # si vuelve a pasar, mejor decirlo que elegir una
                "duplicado": len(filas) > 1
            }

    # Filtro por equipo de proteccion. "dsd" y "dps" son inclusivos (tiene ese
    # equipo, sin importar el otro) y no excluyentes: hoy las 7 estructuras con
    # DSD son un subconjunto exacto de las 52 con DPS, asi que un criterio
    # excluyente dejaria "Solo DSD" siempre en cero.
    if filtro_proteccion:
        def marcado(col):
            # fill_null(False) es lo que hace explicito que una celda vacia
            # cuenta como "no tiene", en vez de propagar nulos y que la fila se
            # caiga sola de los filtros sin que nadie lo haya decidido
            return (pl.col(col).cast(pl.Utf8).str.strip_chars()
                    .str.to_uppercase().is_in(["SI", "SÍ", "TRUE"]).fill_null(False))

        if "Apoyo_Tipo" in df_postes.columns:
            marca_portico = (pl.col("Apoyo_Tipo").cast(pl.Utf8).str.strip_chars()
                             .str.to_lowercase() == "pórtico").fill_null(False)
        else:
            marca_portico = pl.lit(False)

        hay_dsd = "DSD" in df_postes.columns
        condicion = None
        if filtro_proteccion == "porticos":
            condicion = marca_portico
        elif filtro_proteccion == "dsd" and hay_dsd:
            condicion = marcado("DSD")
        elif filtro_proteccion == "dps" and dps_col:
            condicion = marcado(dps_col)
        elif filtro_proteccion == "ambos" and hay_dsd and dps_col:
            condicion = marcado("DSD") & marcado(dps_col)
        elif filtro_proteccion == "sin" and (hay_dsd or dps_col):
            # Postes sin DPS ni DSD. Los porticos se excluyen aparte: no tienen
            # esos equipos, pero tienen su propio boton y contarlos aca
            # duplicaria las mismas 29 filas en dos categorias.
            sin_partes = []
            if hay_dsd:
                sin_partes.append(marcado("DSD"))
            if dps_col:
                sin_partes.append(marcado(dps_col))
            alguna = sin_partes[0]
            for extra in sin_partes[1:]:
                alguna = alguna | extra
            condicion = ~alguna & ~marca_portico
        elif filtro_proteccion == "protegidas" and (hay_dsd or dps_col):
            # Cualquier proteccion, sin importar cual. Hoy coincide con el
            # filtro de DPS porque toda estructura protegida tiene DPS, pero
            # deja de coincidir en cuanto aparezca una con DSD y sin DPS.
            partes = []
            if hay_dsd:
                partes.append(marcado("DSD"))
            if dps_col:
                partes.append(marcado(dps_col))
            condicion = partes[0]
            for extra in partes[1:]:
                condicion = condicion | extra

        if condicion is None:
            raise HTTPException(
                status_code=400,
                detail=f"No se puede filtrar por '{filtro_proteccion}': el inventario no tiene esas columnas."
            )
        df_postes = df_postes.filter(condicion)

    return {
        "df": df_postes,
        "crudo": df_inv_crudo,
        "col_id": id_poste_col,
        "col_circuito": circuito_col,
        "col_dps": dps_col,
        "detalle": detalle_estructura,
    }


@app.post("/api/procesar")
async def procesar_datos(
    radio_busqueda_metros: float = Form(1000.0),
    fecha_inicio: str = Form(None),
    fecha_fin: str = Form(None),
    filtro_campo: str = Form(""),
    filtro_locacion: str = Form(""),
    filtro_portico: str = Form(""),
    filtro_estructura: str = Form(""),
    filtro_proteccion: str = Form("")
):
    validar_radio(radio_busqueda_metros)

    try:
        # Archivos locales montados en el contenedor Docker en /app
        archivo_descargas = "Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
        archivo_postes = ARCHIVO_INVENTARIO

        try:
            # Detectar las columnas disponibles para leer lo minimo necesario
            schema = pl.read_parquet_schema(archivo_descargas)
            cols_to_read = []
            for c in ["Fecha", "Hora", "AÃ±o", "Año", "Mes", "Dia", "Latitud", "Longitud",
                      "Corriente_kA", "Corriente (kA)", "Polaridad_Descargas", "Error_km"]:
                if c in schema:
                    cols_to_read.append(c)
            df_descargas = pl.read_parquet(archivo_descargas, columns=cols_to_read)
            
            # Filtro por fechas si el usuario lo envió
            if "Fecha" in df_descargas.columns:
                if fecha_inicio:
                    try:
                        dt_inicio = datetime.strptime(fecha_inicio, "%Y-%m-%d").date()
                        # Si la columna ya es Date, filtramos directo
                        if df_descargas.schema["Fecha"] == pl.Date:
                            df_descargas = df_descargas.filter(pl.col("Fecha") >= dt_inicio)
                        else:
                            df_descargas = df_descargas.filter(
                                pl.col("Fecha").str.strptime(pl.Date, "%Y-%m-%d", strict=False) >= dt_inicio
                            )
                    except Exception as e:
                        print(f"Error parseando fecha_inicio: {e}")
                if fecha_fin:
                    try:
                        dt_fin = datetime.strptime(fecha_fin, "%Y-%m-%d").date()
                        if df_descargas.schema["Fecha"] == pl.Date:
                            df_descargas = df_descargas.filter(pl.col("Fecha") <= dt_fin)
                        else:
                            df_descargas = df_descargas.filter(
                                pl.col("Fecha").str.strptime(pl.Date, "%Y-%m-%d", strict=False) <= dt_fin
                            )
                    except Exception as e:
                        print(f"Error parseando fecha_fin: {e}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo de descargas local: {str(e)}")

        # Todo el recorte de estructuras (ubicacion, estructura puntual y
        # proteccion) vive en preparar_postes, compartido con el calendario
        prep = preparar_postes(filtro_campo, filtro_locacion, filtro_portico,
                               filtro_estructura, filtro_proteccion)
        df_postes = prep["df"]
        df_inv_crudo = prep["crudo"]
        id_poste_col = prep["col_id"]
        circuito_col = prep["col_circuito"]
        dps_col = prep["col_dps"]
        detalle_estructura = prep["detalle"]

        # Para descargas
        lat_desc_col = "Latitud" if "Latitud" in df_descargas.columns else "LATITUDE"
        lon_desc_col = "Longitud" if "Longitud" in df_descargas.columns else "LONGITUDE"
        corriente_col = "Corriente_kA" if "Corriente_kA" in df_descargas.columns else ("Corriente (kA)" if "Corriente (kA)" in df_descargas.columns else None)
        fecha_col = "Fecha" if "Fecha" in df_descargas.columns else ("Fecha_corta" if "Fecha_corta" in df_descargas.columns else ("FechaHora" if "FechaHora" in df_descargas.columns else None))

        # Preparar datos de descargas
        polaridad_col = "Polaridad_Descargas" if "Polaridad_Descargas" in df_descargas.columns else None
        error_col = "Error_km" if "Error_km" in df_descargas.columns else None
        cols_desc = [lat_desc_col, lon_desc_col]
        if corriente_col: cols_desc.append(corriente_col)
        if fecha_col: cols_desc.append(fecha_col)
        if polaridad_col: cols_desc.append(polaridad_col)
        if error_col: cols_desc.append(error_col)

        df_descargas = df_descargas.select(cols_desc).with_columns([
            pl.col(lat_desc_col).cast(pl.Float64).alias("lat_desc_clean"),
            pl.col(lon_desc_col).cast(pl.Float64).alias("lon_desc_clean")
        ]).drop_nulls(subset=["lat_desc_clean", "lon_desc_clean"])
        
        # Opcional: ordenar descargas por fecha si existe para la gradiente temporal
        if fecha_col:
            df_descargas = df_descargas.sort(fecha_col)

        # Total del rango de fechas, sin recorte geografico ni filtros de
        # ubicacion: es el denominador contra el que se compara cuantas
        # descargas llegaron a amenazar una estructura
        total_rayos_rango = len(df_descargas)

        # Convertir a radianes para BallTree (haversine)
        EARTH_RADIUS_M = 6371000.0
        radius_rad = radio_busqueda_metros / EARTH_RADIUS_M

        descargas_coords_rad = np.radians(df_descargas.select(["lat_desc_clean", "lon_desc_clean"]).to_numpy())
        postes_coords_rad = np.radians(df_postes.select(["lat_clean", "lon_clean"]).to_numpy())

        # Sin descargas en el rango (o sin estructuras tras los filtros) no hay
        # nada que cruzar. BallTree revienta con un array vacio, asi que se salta
        # el cruce y se responde el mapa sin rayos en vez de devolver un error
        aviso = None
        if len(descargas_coords_rad) == 0:
            indices = [np.array([], dtype=int)] * len(postes_coords_rad)
            distancias = [np.array([])] * len(postes_coords_rad)
            aviso = "No hay descargas registradas en el rango de fechas seleccionado."
        elif len(postes_coords_rad) == 0:
            indices = []
            distancias = []
            aviso = "Ninguna estructura coincide con los filtros seleccionados."
        else:
            # Construir BallTree sobre DESCARGAS
            tree = BallTree(descargas_coords_rad, leaf_size=40, metric='haversine')

            # Buscar todos los rayos dentro del radio para cada poste. Las
            # distancias ya se calculan para decidir que entra en el radio, asi
            # que pedirlas de vuelta no cuesta nada; sort_results deja el mas
            # cercano en la primera posicion.
            indices, distancias = tree.query_radius(
                postes_coords_rad, r=radius_rad, return_distance=True, sort_results=True
            )

        ids_rayos_a_mostrar = set()
        resumen_impactos = []

        # Impactos de cada poste en el mismo orden que df_postes, para poder
        # ponderar el mapa de calor estructura por estructura
        impactos_por_poste = [len(idx) for idx in indices]

        # Corriente maxima de cada poste, en el mismo orden que df_postes. Se
        # lleva aparte de resumen_impactos porque la tabla de datos tambien
        # lista las estructuras que no recibieron ningun impacto.
        corriente_max_por_poste = [0.0] * len(indices)

        # Distancia al rayo mas cercano y error de localizacion de ESE rayo. Van
        # juntos a proposito: sobre este dataset el error mediano de posicion
        # (452 m) es mayor que la distancia tipica (65 m), asi que mostrar la
        # distancia sola sugeriria una precision que la medicion no tiene.
        dist_min_por_poste = [None] * len(indices)
        error_min_por_poste = [None] * len(indices)
        errores_km = df_descargas[error_col].to_list() if error_col else None

        for i, idx_array in enumerate(indices):
            if len(idx_array) > 0:
                # Poste i fue impactado por los rayos en idx_array
                ids_rayos_a_mostrar.update(idx_array)

                corriente_max = 0
                if corriente_col:
                    corrientes = df_descargas[idx_array.tolist()][corriente_col].to_list()
                    # filtrar None o nulos
                    corrientes = [c for c in corrientes if c is not None]
                    # Las corrientes traen signo y el 62 % de las descargas son
                    # negativas. Con max() a secas, un poste alcanzado por rayos
                    # de -30 kA y -5 kA reportaba -5: el mas debil de los dos.
                    # Lo que importa es la magnitud, no el signo.
                    corriente_max = max(corrientes, key=abs) if corrientes else 0

                corriente_max_por_poste[i] = round(corriente_max, 2)

                # sort_results dejo el mas cercano primero; el radio del arbol
                # viene en radianes, se pasa a metros con el radio terrestre
                if i < len(distancias) and len(distancias[i]) > 0:
                    dist_min_por_poste[i] = round(distancias[i][0] * EARTH_RADIUS_M, 1)
                    if errores_km is not None:
                        err = errores_km[int(idx_array[0])]
                        if err is not None:
                            error_min_por_poste[i] = round(err * 1000, 1)

                row_poste = df_postes.row(i, named=True)
                resumen_impactos.append({
                    "TAG": row_poste.get(id_poste_col, f"Poste_{i}"),
                    "Circuito": row_poste.get(circuito_col, "N/A"),
                    "Latitud": row_poste["lat_clean"],
                    "Longitud": row_poste["lon_clean"],
                    "N_Impactos": len(idx_array),
                    "Corriente_Max_kA": round(corriente_max, 2)
                })

        # Extraer rayos a mostrar
        idx_list = sorted(list(ids_rayos_a_mostrar))
        df_rayos_filtrados = df_descargas[idx_list]

        # Preparar data para el Frontend.
        # La jerarquia sale del catalogo ya cruzado: las tablas de la vista de
        # datos necesitan campo y locacion, que no estan en el inventario.
        try:
            # Por id y no por tag: los porticos comparten el tag "PORT" y
            # buscarlos asi hacia que 21 filas heredaran la ubicacion de la
            # primera, quedando todas como si fueran la de JCP
            ubicacion_por_id = {
                e["id"]: e for e in construir_catalogo()["estructuras"]
            }
        except Exception as e:
            print(f"No se pudo cruzar la jerarquia: {e}")
            ubicacion_por_id = {}

        estructuras_json = []
        for i, row in enumerate(df_postes.iter_rows(named=True)):
            tiene_dsd = str(row.get("DSD", "")).strip().upper() in ["SÍ", "SI", "TRUE"]
            tiene_dps = False
            if dps_col:
                tiene_dps = str(row.get(dps_col, "")).strip().upper() in ["SÍ", "SI", "TRUE"]
            
            ident, tag, _circ = identidad_estructura(row, id_poste_col, circuito_col)
            ubic = ubicacion_por_id.get(ident, {})

            estructuras_json.append({
                "id": ident,
                "tag": tag,
                "es_portico": ubic.get("es_portico", False),
                "lat": row["lat_clean"],
                "lon": row["lon_clean"],
                "campo": ubic.get("campo", ""),
                "locacion": ubic.get("locacion", ""),
                "portico": ubic.get("portico", row.get(circuito_col, "")),
                "protegido": tiene_dsd or tiene_dps,
                "impactos": impactos_por_poste[i] if i < len(impactos_por_poste) else 0,
                "corriente_max": corriente_max_por_poste[i] if i < len(corriente_max_por_poste) else 0,
                "dist_min": dist_min_por_poste[i] if i < len(dist_min_por_poste) else None,
                "error_min": error_min_por_poste[i] if i < len(error_min_por_poste) else None,
                "dsd": tiene_dsd,
                "dps": tiene_dps,
                "detalles": {
                    "Circuito": row.get(circuito_col, "N/A"),
                    "DSD": row.get("DSD", "No"),
                    "DPS": row.get(dps_col, "No") if dps_col else "No"
                }
            })

        rayos_json = []
        for i, row in enumerate(df_rayos_filtrados.iter_rows(named=True)):
            rayos_json.append({
                "lat": row["lat_desc_clean"],
                "lon": row["lon_desc_clean"],
                "corriente": row.get(corriente_col, 0),
                "fecha": str(row.get(fecha_col, "")),
                "polaridad": _texto(row.get(polaridad_col)) if polaridad_col else "",
                "orden": i
            })

        return JSONResponse(content={
            "kpis": {
                "total_estructuras": len(df_postes),
                "estructuras_afectadas": len(resumen_impactos),
                "total_rayos": len(df_rayos_filtrados),
                "total_rayos_rango": total_rayos_rango,
                "radio": radio_busqueda_metros
            },
            "estructuras": estructuras_json,
            "rayos": rayos_json,
            "impactos": resumen_impactos,
            "detalle_estructura": detalle_estructura,
            "aviso": aviso
        })

    except Exception as e:
        print(f"Error procesando datos: {e}")
        traceback.print_exc()
        return JSONResponse(
            status_code=400,
            content={"message": f"Error procesando datos: {str(e)}"}
        )

@app.post("/api/calendario")
async def calendario_mensual(
    anio: int = Form(...),
    mes: int = Form(...),
    radio_busqueda_metros: float = Form(1000.0),
    filtro_campo: str = Form(""),
    filtro_locacion: str = Form(""),
    filtro_portico: str = Form(""),
    filtro_estructura: str = Form(""),
    filtro_proteccion: str = Form("")
):
    """Actividad diaria de un mes, con el mismo recorte de estructuras del mapa.

    El cruce espacial se hace una sola vez para todo el mes y despues se agrupa
    por dia: repetir el BallTree treinta veces costaria treinta veces mas para
    llegar exactamente al mismo resultado.
    """
    validar_radio(radio_busqueda_metros)

    try:
        if not 1 <= mes <= 12:
            raise HTTPException(status_code=400, detail="El mes debe estar entre 1 y 12")

        prep = preparar_postes(filtro_campo, filtro_locacion, filtro_portico,
                               filtro_estructura, filtro_proteccion)
        df_postes = prep["df"]

        archivo = "Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
        cols = ["Fecha", "Hora", "Latitud", "Longitud", "Corriente_kA", "Polaridad_Descargas"]
        schema = pl.read_parquet_schema(archivo)
        df = pl.read_parquet(archivo, columns=[c for c in cols if c in schema])

        # El mes completo, del 1 al ultimo dia
        dias_mes = calendar.monthrange(anio, mes)[1]
        desde = date(anio, mes, 1)
        hasta = date(anio, mes, dias_mes)
        df_mes = df.filter((pl.col("Fecha") >= desde) & (pl.col("Fecha") <= hasta))

        # Descargas de la region por dia, sin recorte geografico: es el
        # denominador que dice si el dia estuvo tormentoso en general
        rango_por_dia = {}
        if len(df_mes):
            for f, n in df_mes.group_by("Fecha").agg(pl.len().alias("n")).iter_rows():
                rango_por_dia[f.isoformat()] = n

        # Mismo mes en todos los anios con datos, para saber si este mes fue
        # mas o menos tormentoso que lo usual. Sin recorte geografico (igual
        # que rango_por_dia): es una referencia regional, no de estructuras
        # puntuales, y sale del parquet que ya esta en memoria, sin leerlo de
        # nuevo ni repetir el cruce espacial por cada anio
        comparacion_anios = []
        if "Fecha" in df.columns and len(df):
            por_anio = (
                df.filter(pl.col("Fecha").dt.month() == mes)
                  .with_columns(pl.col("Fecha").dt.year().alias("anio"))
                  .group_by("anio").agg(pl.len().alias("n"))
                  .sort("anio")
            )
            comparacion_anios = [{"anio": a, "total": n} for a, n in por_anio.iter_rows()]

        # Cruce espacial una sola vez para todo el mes
        EARTH_RADIUS_M = 6371000.0
        limpio = df_mes.with_columns([
            pl.col("Latitud").cast(pl.Float64).alias("la"),
            pl.col("Longitud").cast(pl.Float64).alias("lo"),
        ]).drop_nulls(subset=["la", "lo"])

        indices = []
        if len(limpio) and len(df_postes):
            arbol = BallTree(np.radians(limpio.select(["la", "lo"]).to_numpy()),
                             leaf_size=40, metric="haversine")
            indices = arbol.query_radius(
                np.radians(df_postes.select(["lat_clean", "lon_clean"]).to_numpy()),
                r=radio_busqueda_metros / EARTH_RADIUS_M)

        fechas = [f.isoformat() for f in limpio["Fecha"].to_list()] if len(limpio) else []
        horas = limpio["Hora"].to_list() if "Hora" in limpio.columns and len(limpio) else []
        corrientes = limpio["Corriente_kA"].to_list() if "Corriente_kA" in limpio.columns and len(limpio) else []
        polaridades = limpio["Polaridad_Descargas"].to_list() if "Polaridad_Descargas" in limpio.columns and len(limpio) else []
        tags = df_postes["id_estructura"].to_list() if len(df_postes) else []

        # Se invierte el mapeo estructura -> descargas para poder agrupar por dia
        por_dia = {}
        alcanzadas = set()
        for i, idx in enumerate(indices):
            tag = tags[i] if i < len(tags) else str(i)
            for j in idx:
                dia = fechas[j]
                d = por_dia.setdefault(dia, {"rayos": set(), "estructuras": set(), "horas": [0] * 24, "corrientes": []})
                if j not in d["rayos"]:
                    d["rayos"].add(j)
                    if j < len(horas) and horas[j]:
                        try:
                            d["horas"][int(str(horas[j])[:2])] += 1
                        except (ValueError, IndexError):
                            pass
                    if j < len(corrientes) and corrientes[j] is not None:
                        pol = polaridades[j] if j < len(polaridades) else None
                        d["corrientes"].append((corrientes[j], pol))
                d["estructuras"].add(tag)
                alcanzadas.add(tag)

        dias = []
        for n_dia in range(1, dias_mes + 1):
            iso = date(anio, mes, n_dia).isoformat()
            d = por_dia.get(iso)
            if d:
                pico = max(range(24), key=lambda h: d["horas"][h]) if any(d["horas"]) else None
                corr_val, corr_pol = max(d["corrientes"], key=lambda p: abs(p[0])) if d["corrientes"] else (0, None)
                dias.append({
                    "fecha": iso, "dia": n_dia,
                    "rayos_radio": len(d["rayos"]),
                    "rayos_rango": rango_por_dia.get(iso, 0),
                    "estructuras": len(d["estructuras"]),
                    "estructuras_lista": sorted(d["estructuras"]),
                    "corriente_max": round(corr_val, 2),
                    "corriente_max_polaridad": _texto(corr_pol) if corr_pol is not None else "",
                    "hora_pico": pico,
                    "horas": d["horas"],
                })
            else:
                dias.append({
                    "fecha": iso, "dia": n_dia, "rayos_radio": 0,
                    "rayos_rango": rango_por_dia.get(iso, 0), "estructuras": 0,
                    "estructuras_lista": [],
                    "corriente_max": 0, "corriente_max_polaridad": "", "hora_pico": None, "horas": [0] * 24,
                })

        # Distribucion horaria del mes entero, para la ventana de mantenimiento
        horas_mes = [0] * 24
        for d in dias:
            for h in range(24):
                horas_mes[h] += d["horas"][h]

        peor = max(dias, key=lambda d: d["rayos_radio"]) if dias else None
        return JSONResponse(content={
            "anio": anio, "mes": mes, "dias_del_mes": dias_mes,
            # Dia de la semana del 1 (0=lunes), que es donde arranca la grilla
            "primer_dia_semana": date(anio, mes, 1).weekday(),
            "dias": dias,
            "horas_mes": horas_mes,
            "comparacion_anios": comparacion_anios,
            "resumen": {
                "total_radio": sum(d["rayos_radio"] for d in dias),
                "total_rango": sum(d["rayos_rango"] for d in dias),
                "dias_con_actividad": sum(1 for d in dias if d["rayos_radio"] > 0),
                "estructuras_alcanzadas": len(alcanzadas),
                "estructuras_analizadas": len(df_postes),
                "peor_dia": peor["fecha"] if peor and peor["rayos_radio"] else None,
                "peor_dia_rayos": peor["rayos_radio"] if peor else 0,
                "radio": radio_busqueda_metros,
            },
        })
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error en calendario: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"message": f"Error generando el calendario: {str(e)}"})


@app.post("/api/simulador")
async def simulador_rango(
    fecha_inicio: str = Form(...),
    fecha_fin: str = Form(...),
    radio_busqueda_metros: float = Form(1000.0),
    filtro_campo: str = Form(""),
    filtro_locacion: str = Form(""),
    filtro_portico: str = Form(""),
    filtro_estructura: str = Form(""),
    filtro_proteccion: str = Form("")
):
    """Rayos del rango [inicio, fin] que caen dentro del radio de las estructuras
    filtradas, con un timestamp continuo (segundos desde el arranque del rango)
    para reproducirlos cronologicamente en el Simulador de Tormenta.

    El rango puede ser un dia, una semana, un mes, un anio o todo el historial.
    Un rango sin ninguna fila en el parquet es un periodo tranquilo (soleado).
    """
    validar_radio(radio_busqueda_metros)

    try:
        try:
            d0 = date.fromisoformat(fecha_inicio)
            d1 = date.fromisoformat(fecha_fin)
        except ValueError:
            raise HTTPException(status_code=400, detail="Fechas inválidas, se esperan YYYY-MM-DD")
        if d1 < d0:
            d0, d1 = d1, d0

        dias = (d1 - d0).days + 1
        span = dias * 86400   # segundos totales del rango (dias completos)

        prep = preparar_postes(filtro_campo, filtro_locacion, filtro_portico,
                               filtro_estructura, filtro_proteccion)
        df_postes = prep["df"]

        archivo = "Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
        cols = ["Fecha", "Hora", "Latitud", "Longitud", "Corriente_kA", "Polaridad_Descargas"]
        schema = pl.read_parquet_schema(archivo)
        df = pl.read_parquet(archivo, columns=[c for c in cols if c in schema])
        df_rango = df.filter((pl.col("Fecha") >= d0) & (pl.col("Fecha") <= d1))
        total_region = len(df_rango)

        EARTH_RADIUS_M = 6371000.0
        limpio = df_rango.with_columns([
            pl.col("Latitud").cast(pl.Float64).alias("la"),
            pl.col("Longitud").cast(pl.Float64).alias("lo"),
        ]).drop_nulls(subset=["la", "lo"])

        # Union de los rayos que caen dentro del radio de CUALQUIER estructura
        # filtrada. Un mismo rayo cerca de dos postes se cuenta una sola vez.
        rayos = []
        if len(limpio) and len(df_postes):
            arbol = BallTree(np.radians(limpio.select(["la", "lo"]).to_numpy()),
                             leaf_size=40, metric="haversine")
            idx_por_poste = arbol.query_radius(
                np.radians(df_postes.select(["lat_clean", "lon_clean"]).to_numpy()),
                r=radio_busqueda_metros / EARTH_RADIUS_M)
            dentro = set()
            for idx in idx_por_poste:
                dentro.update(int(j) for j in idx)

            if dentro:
                la = limpio["la"].to_list()
                lo = limpio["lo"].to_list()
                fechas = limpio["Fecha"].to_list()
                horas = limpio["Hora"].to_list()
                corr = limpio["Corriente_kA"].to_list() if "Corriente_kA" in limpio.columns else []
                pol = limpio["Polaridad_Descargas"].to_list() if "Polaridad_Descargas" in limpio.columns else []

                # Estructura mas cercana a cada rayo (una consulta batch sobre un
                # arbol de las estructuras filtradas) para la tabla cronologica
                dentro_list = sorted(dentro)
                arbol_postes = BallTree(
                    np.radians(df_postes.select(["lat_clean", "lon_clean"]).to_numpy()),
                    leaf_size=40, metric="haversine")
                tags_postes = df_postes["id_estructura"].to_list()
                coords_rayos = np.radians(np.array([[la[j], lo[j]] for j in dentro_list]))
                dist_rad, idx_est = arbol_postes.query(coords_rayos, k=1)

                for k_i, j in enumerate(dentro_list):
                    seg_dia = _segundos_desde_medianoche(horas[j])
                    if seg_dia is None:
                        continue
                    # Timestamp continuo: dias desde el arranque + hora del dia
                    t = (fechas[j] - d0).days * 86400 + seg_dia
                    rayos.append({
                        "t": t,
                        "lat": round(la[j], 6),
                        "lon": round(lo[j], 6),
                        "c": round(corr[j], 1) if j < len(corr) and corr[j] is not None else 0,
                        "p": _texto(pol[j]) if j < len(pol) and pol[j] is not None else "",
                        # Estructura mas cercana (su identidad) y distancia en metros
                        "e": tags_postes[int(idx_est[k_i][0])],
                        "d": round(float(dist_rad[k_i][0]) * EARTH_RADIUS_M, 1),
                    })
                rayos.sort(key=lambda r: r["t"])

        return JSONResponse(content={
            "inicio": d0.isoformat(),
            "fin": d1.isoformat(),
            "dias": dias,
            "span": span,
            "rayos": rayos,
            "total_radio": len(rayos),
            "total_region": total_region,
            "estructuras_filtradas": len(df_postes),
            "radio": radio_busqueda_metros,
            # Soleado = ni un rayo en toda la region en el rango. Si hubo rayos
            # en la region pero ninguno dentro del radio, no es soleado: es un
            # periodo con tormentas lejanas que no tocaron a las estructuras
            "soleado": total_region == 0,
        })
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error en simulador: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"message": f"Error generando el simulador: {str(e)}"})


@app.post("/api/dias-radio")
async def dias_con_impacto_radio(
    radio_busqueda_metros: float = Form(1000.0),
    filtro_campo: str = Form(""),
    filtro_locacion: str = Form(""),
    filtro_portico: str = Form(""),
    filtro_estructura: str = Form(""),
    filtro_proteccion: str = Form("")
):
    """Dias del historico con al menos una descarga dentro del radio de las
    estructuras filtradas. Sirve para marcar en rojo esos dias en el calendario
    del sidebar (los demas dias con datos van en verde). Depende del radio y de
    los filtros, asi que se recalcula cuando cambian; el arbol de rayos esta
    cacheado para que solo cueste la consulta de las estructuras.
    """
    validar_radio(radio_busqueda_metros)

    try:
        prep = preparar_postes(filtro_campo, filtro_locacion, filtro_portico,
                               filtro_estructura, filtro_proteccion)
        df_postes = prep["df"]

        arbol, fechas = _arbol_todos_los_rayos()
        EARTH_RADIUS_M = 6371000.0
        dias = set()
        if arbol is not None and len(df_postes):
            idx_por_poste = arbol.query_radius(
                np.radians(df_postes.select(["lat_clean", "lon_clean"]).to_numpy()),
                r=radio_busqueda_metros / EARTH_RADIUS_M)
            arreglos = [a for a in idx_por_poste if len(a)]
            if arreglos:
                for j in np.unique(np.concatenate(arreglos)):
                    dias.add(fechas[int(j)].isoformat())

        return JSONResponse(content={"dias_con_radio": sorted(dias)})
    except Exception as e:
        print(f"Error en dias-radio: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=400, content={"message": f"Error en dias-radio: {str(e)}"})


@app.post("/api/exportar")
async def exportar_informe(
    radio_busqueda_metros: float = Form(1000.0),
    fecha_inicio: str = Form(None),
    fecha_fin: str = Form(None),
    filtro_campo: str = Form(""),
    filtro_locacion: str = Form(""),
    filtro_portico: str = Form(""),
    filtro_estructura: str = Form(""),
    filtro_proteccion: str = Form("")
):
    """Devuelve el informe en Excel del mismo analisis que se ve en pantalla.

    Reusa /api/procesar en vez de recalcular: si el dia de manana cambia una
    regla del cruce, el informe la hereda sin tener que tocarse. Por eso no
    valida el radio aca: lo hace procesar_datos y la excepcion sube sola.
    """
    respuesta = await procesar_datos(
        radio_busqueda_metros=radio_busqueda_metros,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        filtro_campo=filtro_campo,
        filtro_locacion=filtro_locacion,
        filtro_portico=filtro_portico,
        filtro_estructura=filtro_estructura,
        filtro_proteccion=filtro_proteccion,
    )
    if respuesta.status_code != 200:
        return respuesta

    datos = json.loads(respuesta.body)

    etiquetas_proteccion = {
        "": "Todas", "sin": "Estructuras sin protección", "porticos": "Solo pórticos",
        "protegidas": "Con DPS o DSD", "dps": "Con DPS", "dsd": "Con DSD",
        "ambos": "Con DPS y DSD",
    }
    filtros = [
        ("Campo", filtro_campo or "Todos"),
        ("Locación / Circuito", filtro_locacion or "Todas"),
        ("Pórtico / SWG / Tramo", filtro_portico or "Todos"),
        ("Estructura específica", filtro_estructura or "Todas"),
        ("Protección", etiquetas_proteccion.get(filtro_proteccion, filtro_proteccion)),
        ("Desde", fecha_inicio or "—"),
        ("Hasta", fecha_fin or "—"),
        ("Radio de búsqueda", f"{radio_busqueda_metros:.0f} m"),
    ]

    contenido = construir_informe(datos, filtros)
    nombre = f"Informe_Descargas_Atmosfericas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

    return Response(
        content=contenido,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@app.get("/api/filtros")
async def obtener_filtros():
    # Devuelve la jerarquia y el catalogo de estructuras en la misma respuesta:
    # el buscador necesita las dos cosas a la vez para poder resolver la
    # ubicacion de una estructura sin volver a consultar al servidor
    try:
        catalogo = construir_catalogo()
        return JSONResponse(content={
            "filtros": catalogo["jerarquia"],
            "estructuras": catalogo["estructuras"],
            "sin_asignar": catalogo["sin_asignar"]
        })
    except Exception as e:
        print(f"Error cargando filtros: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

_cache_rango_fechas = None

@app.get("/api/rango-fechas")
async def obtener_rango_fechas():
    # Se cachea porque el parquet no cambia entre peticiones y recorrerlo
    # completo en cada carga del calendario es caro
    global _cache_rango_fechas
    if _cache_rango_fechas is not None:
        return JSONResponse(content=_cache_rango_fechas)

    try:
        df = pl.read_parquet(
            "Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet",
            columns=["Fecha"]
        )
        fechas = df["Fecha"].drop_nulls().unique().sort()
        dias = [d.isoformat() for d in fechas.to_list()]

        if not dias:
            return JSONResponse(content={"error": "El parquet no tiene fechas válidas"}, status_code=500)

        _cache_rango_fechas = {
            "min": dias[0],
            "max": dias[-1],
            "dias_con_datos": dias
        }
        return JSONResponse(content=_cache_rango_fechas)
    except Exception as e:
        print(f"Error cargando rango de fechas: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
