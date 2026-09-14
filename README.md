# Monitor de Descargas Atmosféricas — Campo Llanos 34

Tablero web para analizar el riesgo por rayos sobre la red eléctrica de GeoPark
en el Campo Llanos 34. Cruza el histórico de descargas atmosféricas con el
inventario de estructuras y responde, para un período y un radio dados, qué
estructuras fueron alcanzadas, cuándo y con qué intensidad.

Backend en **FastAPI** (Python 3.11), frontend estático sin framework
(HTML + CSS + JavaScript, con Leaflet para los mapas y Chart.js para las
gráficas).

---

## Qué se puede hacer

El tablero tiene cuatro vistas, todas gobernadas por los mismos filtros del
panel izquierdo (ubicación, radio de búsqueda y rango de fechas):

| Vista | Para qué sirve |
|---|---|
| **Mapa** | Vista satelital con las estructuras y las descargas del período. Alterna entre mapa general y mapa de calor, y colorea cada rayo según su corriente. |
| **Datos** | Gráficas y tablas del período: distribución por circuito, por tipo de estructura y por presencia de protección (DPS/DSD). Exporta el informe a Excel. |
| **Calendario** | Rejilla mensual con la intensidad de cada día. Al pasar el cursor por un día con actividad aparece una tarjeta con el detalle (descargas, estructuras impactadas, hora pico, corriente máxima y la distribución por hora); al hacer clic queda fija. |
| **Simulador de Tormenta** | Reproduce la caída de los rayos sobre el mapa en orden cronológico, con ambiente meteorológico (sol, lluvia, tormenta), línea de tiempo navegable y la cronología completa en tabla. |

---

## Fuentes de datos

Los tres archivos viven en la raíz del repositorio y se leen al arrancar. Sus
nombres están fijados en `backend/main.py`.

| Archivo | Contenido |
|---|---|
| `Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet` | Histórico de descargas: fecha, hora (`HH:MM:SS`), latitud, longitud, corriente en kA, polaridad y error de localización. **779.109 registros entre 2021-01-01 y 2026-09-03.** |
| `Inventario_Estructuras_y_DPS_Final.xlsx` | Inventario de estructuras: coordenadas, circuito, tipo de apoyo y equipos de protección (DPS, DSD, cable de guarda, puesta a tierra). **759 estructuras.** |
| `Localizaciones_Final.xlsx` | Maestro de localizaciones. Define la jerarquía de filtros Campo → Locación/Circuito → Pórtico/Tramo. |

### Elegir de dónde salen las descargas: parquet o Supabase

La variable `FUENTE_DATOS` (en `.env` para local, en el panel de Render para
producción) decide la fuente. El tablero muestra una etiqueta con la fuente
activa y la fecha del dato más reciente.

| `FUENTE_DATOS` | Para qué | Cómo funciona |
|---|---|---|
| `parquet` *(por defecto)* | Desarrollo y pruebas | Lee el parquet del repositorio. **Nunca se conecta a Supabase**, aunque la URL esté configurada, así que no gasta egress. |
| `supabase` | Producción y demos | Arranca con la copia del parquet y le pregunta a Supabase solo qué días cambiaron (huellas por mes y por día); baja únicamente esos. Responde desde memoria y revisa cambios como máximo una vez por minuto mientras alguien usa el tablero. Requiere `SUPABASE_DASHBOARD_DB_URL`. |

Si Supabase no responde, el tablero sigue funcionando con los últimos datos que
tiene y la etiqueta lo avisa ("Sin conexión con la base · datos hasta …").

**Cómo actualizar el parquet de pruebas** sin gastar egress, copiando el Gold que
genera el pipeline (definir `RUTA_GOLD_PIPELINE` en `.env`):

```bash
python scripts/actualizar_parquet.py
```

El script valida el archivo antes de reemplazarlo. Con `FUENTE_DATOS=parquet` el
tablero toma la copia nueva solo, sin reiniciar. Para que llegue a Render hay que
commitearla. Los Excel se actualizan igual que siempre: reemplazar el archivo
conservando el nombre; el backend detecta el cambio por la fecha de modificación.

> Los pórticos se identifican por el par `circuito␟tag`, no por el tag suelto:
> el tag `PORT` se repite en varias locaciones y por sí solo no distingue nada.

---

## Correr en local (Docker)

Requiere Docker y Docker Compose.

```bash
docker compose up --build -d
```

El tablero queda en **http://localhost:8080**.

```bash
docker compose down
```

`docker-compose.yml` es solo para desarrollo: monta el código como volumen y
arranca uvicorn con `--reload`, así los cambios se ven sin reconstruir la imagen.
El despliegue en Render **no** usa este archivo, sino el `Dockerfile`.

---

## Pruebas

Ninguna prueba se conecta a Supabase ni gasta egress: el papel de la base lo
cumple un Supabase simulado sobre el parquet del repositorio.

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

Cubren la sincronización con la base (días nuevos, recargados o borrados, caídas
y recuperación), que modo parquet y modo Supabase den respuestas idénticas byte a
byte, las consultas sobre los datos en memoria y el script de actualización del
parquet.

Dos pruebas de carga se corren a mano:

```bash
python tests/prueba_carga.py http://127.0.0.1:8000 10
```

Con el servidor ya levantado, simula 10 usuarios usándolo a la vez sin pausas.

```bash
python tests/simular_render.py 10
```

Solo en Windows: levanta el servidor con 0,1 de CPU, como el plan Free de
Render, corre la prueba de carga y reporta el pico de memoria contra los 512 MB.

---

## Estructura del proyecto

```
├── backend/
│   ├── main.py          API FastAPI: cruce espacial, filtros y endpoints
│   ├── datos.py         Fuente de datos (parquet o Supabase) y sincronización
│   ├── lectura_parquet.py  Lectura del parquet en un proceso aparte
│   ├── db.py            Conexión de solo lectura a Supabase
│   └── informe.py       Generación del informe en Excel
├── scripts/
│   └── actualizar_parquet.py  Copia el Gold del pipeline como parquet de pruebas
├── tests/               Pruebas (pytest) y pruebas de carga manuales
├── static/
│   ├── index.html       Estructura del tablero (las cuatro vistas)
│   ├── script.js        Toda la lógica del frontend
│   ├── styles.css       Estilos, escala adaptativa y temas
│   └── LOGO-GEOPARK-NEGATIVO.png
├── Dockerfile           Imagen de producción (la que usa Render)
├── docker-compose.yml   Entorno de desarrollo local
├── render.yaml          Blueprint del despliegue
├── requirements.txt     Dependencias con versiones fijadas
└── MEJORAS_PENDIENTES.md
```


