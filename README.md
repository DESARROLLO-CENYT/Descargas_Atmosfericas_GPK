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

**Cómo actualizar los datos:** reemplazar el archivo por la versión nueva
conservando el mismo nombre y volver a desplegar. El backend detecta el cambio
por la fecha de modificación e invalida sus cachés solo; no hay que tocar código.

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

## Despliegue en Render

El repositorio trae un blueprint (`render.yaml`) con la configuración completa.

1. En Render: **New → Blueprint** y conectar este repositorio.
2. Revisar los valores marcados como `AJUSTAR` en `render.yaml` (nombre del
   servicio, región, rama y plan).
3. Desplegar. Cada push a la rama configurada redespliega solo.

No hay variables de entorno que configurar: los datos viajan dentro de la imagen
y Render inyecta el puerto en `$PORT`, que el `Dockerfile` ya usa.

### Requisitos de recursos (medidos, no estimados)

Medido dentro de un contenedor limitado a 512 MB, contra el historial completo:

| Petición | Tiempo | Memoria |
|---|---|---|
| Arranque en frío | ~10 s | 120 MB |
| Análisis de un mes, radio 1 km | 1,1 s | 174 MB |
| Análisis del historial completo, radio 1 km | 1,9 s | 199 MB |
| Análisis del historial completo, radio 5 km | 3,4 s | 222 MB |

Imagen construida: **1,24 GB**.

### Limitaciones del plan Free

El plan Free de Render da 512 MB de RAM y 0,1 de CPU. Con eso:

- **El servicio se suspende tras 15 minutos sin tráfico.** La primera visita
  después de la suspensión tarda alrededor de un minuto en responder.
- **Los tiempos de la tabla anterior se multiplican.** Se midieron en una
  máquina de desarrollo; con 0,1 de CPU hay que contar con varias veces más.
  Render corta las peticiones que superan los 100 segundos.
- **Un solo worker.** Cada worker carga su propia copia del parquet y del árbol
  espacial; con dos, el servicio se queda sin memoria. Está fijado en el
  `Dockerfile` y no debe subirse sin cambiar de plan.

Si el uso se vuelve cotidiano, el plan Starter elimina la suspensión y triplica
la CPU sin cambiar nada del código.

### Por qué el radio está limitado a 5.000 m

`sklearn.neighbors.BallTree.query_radius` devuelve, por cada una de las 759
estructuras, el arreglo con **todos** los rayos que caen dentro del radio. El
costo crece con el área, es decir con el cuadrado del radio. Medido contra el
historial completo en un contenedor de 512 MB:

| Radio | Rayos devueltos | Memoria | Resultado |
|---|---|---|---|
| 1.000 m | 5.309 | 199 MB | 1,9 s |
| 2.000 m | 9.683 | 236 MB | 2,1 s |
| 5.000 m | 23.717 | 222 MB | 3,4 s |
| 10.000 m | — | — | **el proceso muere (OOM, exit 137)** |

Un radio de 10 km no devolvía un error: **tumbaba el servicio completo**. Por eso
`RADIO_MAXIMO_METROS = 5000` en `backend/main.py` rechaza el valor con un HTTP
400 antes de tocar el árbol espacial, y el campo del formulario tiene el mismo
tope. El uso real del tablero va entre 100 y 1.000 m, así que el límite no
estorba en la operación.

---

## Estructura del proyecto

```
├── backend/
│   ├── main.py          API FastAPI: cruce espacial, filtros y endpoints
│   └── informe.py       Generación del informe en Excel
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

### Endpoints

| Método | Ruta | Devuelve |
|---|---|---|
| `GET` | `/` | El tablero |
| `GET` | `/api/filtros` | Jerarquía de filtros y catálogo de estructuras |
| `GET` | `/api/rango-fechas` | Primer y último día con datos, y los días con actividad |
| `POST` | `/api/procesar` | Análisis principal: KPIs, estructuras y rayos del período |
| `POST` | `/api/calendario` | Actividad diaria de un mes |
| `POST` | `/api/simulador` | Rayos del rango con timestamp continuo, para la animación |
| `POST` | `/api/dias-radio` | Días del histórico con impacto dentro del radio |
| `POST` | `/api/exportar` | Informe en Excel del análisis en pantalla |

---

## Notas de mantenimiento

- **Las dependencias están fijadas** en `requirements.txt` a las versiones que
  corren hoy. Para actualizar: subir la versión, reconstruir la imagen y probar
  en local antes de desplegar.
- **El frontend no se cachea.** El backend le cuelga a `script.js` y `styles.css`
  un `?v=<fecha de modificación>` y manda `Cache-Control: no-store`, para que
  nadie quede con medio tablero viejo después de un despliegue.
- **Los cachés del backend se invalidan solos** cuando cambia la fecha de
  modificación de los archivos de datos.
- Las mejoras identificadas y todavía no implementadas están en
  [`MEJORAS_PENDIENTES.md`](MEJORAS_PENDIENTES.md).
