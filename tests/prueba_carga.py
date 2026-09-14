"""Prueba de carga manual: N usuarios usando el tablero al mismo tiempo.

No la corre pytest. Con el servidor levantado (en modo parquet, para no gastar
egress):

    python tests/prueba_carga.py http://127.0.0.1:8000 10

Cada usuario abre el tablero (las 4 peticiones del arranque en paralelo) y
despues explora tres meses con radios distintos, sin pausas. El usuario 0 pide
ademas el peor caso: todo el historial con el radio maximo. Es mas exigente que
el uso real, a proposito.
"""
import concurrent.futures
import statistics
import sys
import threading
import time
import urllib.parse
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
USUARIOS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
LIMITE_RENDER_SEGUNDOS = 100

tiempos = []
errores = []
candado = threading.Lock()


def pedir(ruta, datos=None):
    cuerpo = urllib.parse.urlencode(datos).encode() if datos is not None else None
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + ruta, data=cuerpo), timeout=900) as r:
            r.read()
    except Exception as e:
        with candado:
            errores.append(f"{ruta}: {e}")
    with candado:
        tiempos.append((ruta, time.time() - t0))


def en_paralelo(llamadas):
    with concurrent.futures.ThreadPoolExecutor(len(llamadas)) as ex:
        list(ex.map(lambda c: pedir(*c), llamadas))


def usuario(i):
    inicio = time.time()
    pedir("/api/filtros")
    pedir("/api/rango-fechas")
    dia = {"radio_busqueda_metros": 100, "fecha_inicio": "2026-09-13", "fecha_fin": "2026-09-13"}
    en_paralelo([
        ("/api/procesar", dia),
        ("/api/dias-radio", {"radio_busqueda_metros": 100}),
        ("/api/calendario", {"anio": 2026, "mes": 9, "radio_busqueda_metros": 100}),
        ("/api/simulador", dia),
    ])
    radio = [100, 500, 1000, 2000][i % 4]
    for paso in range(3):
        anio, mes = 2021 + (i + paso) % 6, (i * 5 + paso * 3) % 12 + 1
        if anio == 2026 and mes > 8:
            mes = 8
        rango = {"radio_busqueda_metros": radio, "fecha_inicio": f"{anio}-{mes:02d}-01",
                 "fecha_fin": f"{anio}-{mes:02d}-28"}
        if i == 0 and paso == 1:
            rango = {"radio_busqueda_metros": 5000, "fecha_inicio": "2021-01-01", "fecha_fin": "2026-12-31"}
        en_paralelo([
            ("/api/procesar", rango),
            ("/api/calendario", {"anio": anio, "mes": mes, "radio_busqueda_metros": rango["radio_busqueda_metros"]}),
            ("/api/simulador", rango),
            ("/api/dias-radio", {"radio_busqueda_metros": rango["radio_busqueda_metros"]}),
        ])
    return time.time() - inicio


def main():
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(USUARIOS) as ex:
        sesiones = list(ex.map(usuario, range(USUARIOS)))
    total = time.time() - t0

    todos = sorted(t for _, t in tiempos)
    p90 = todos[min(len(todos) - 1, int(0.9 * len(todos)))]
    print(f"{USUARIOS} usuarios, {len(tiempos)} peticiones, {len(errores)} errores")
    print(f"Por peticion: mediana {statistics.median(todos):.1f}s | p90 {p90:.1f}s | peor {todos[-1]:.1f}s")
    print(f"Sesion completa por usuario: mediana {statistics.median(sesiones):.1f}s | peor {max(sesiones):.1f}s")
    print(f"Tiempo total: {total:.1f}s")
    print(f"Peticiones de mas de {LIMITE_RENDER_SEGUNDOS} s (Render las corta): "
          f"{sum(1 for t in todos if t > LIMITE_RENDER_SEGUNDOS)}")
    for ruta in ("/api/procesar", "/api/calendario", "/api/simulador", "/api/dias-radio"):
        ts = sorted(t for r, t in tiempos if r == ruta)
        print(f"  {ruta:18s} mediana {statistics.median(ts):5.1f}s | peor {ts[-1]:5.1f}s")
    for e in errores[:5]:
        print("  ERROR", e)
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
