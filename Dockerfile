FROM python:3.11-slim

WORKDIR /app

# No escribir .pyc y no bufferizar la salida: los logs de Render se ven en vivo.
# POLARS_MAX_THREADS: Polars abre por defecto un hilo por nucleo que ve la
# maquina, aunque Render Free solo da 0,1 de CPU; mas hilos no aceleran nada y
# cada uno reserva memoria. Medido con 2 hilos: ~40 MB menos en los calculos
# pesados.
# MALLOC_ARENA_MAX: en Linux, el malloc de glibc crea una zona de memoria por
# hilo que la pide, y con un servidor que atiende cada peticion en su propio
# hilo eso infla la memoria sin necesidad. Es el ajuste habitual para servidores
# Python con hilos; no se pudo medir en local porque las pruebas corren en Windows.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POLARS_MAX_THREADS=2 \
    MALLOC_ARENA_MAX=2

# Las dependencias van en una capa aparte del codigo: mientras requirements.txt
# no cambie, Docker reutiliza esta capa y el build no vuelve a compilar nada
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render inyecta el puerto en $PORT y espera que el proceso escuche ahi. En
# local no existe esa variable, asi que cae a 8000 (docker-compose lo publica
# en el 8080 del host).
ENV PORT=8000
EXPOSE 8000

# La imagen es la de produccion: lee de Supabase aunque en Render falte la
# variable, y sin SUPABASE_DASHBOARD_DB_URL falla al arrancar en vez de mostrar
# datos de otra fuente. En local, docker-compose.yml la pisa con parquet.
ENV FUENTE_DATOS=supabase

# Un solo worker a proposito: cada worker tendria su propia copia de los datos,
# del cache y de la conexion a Supabase, y en el plan Free de Render solo hay
# 512 MB. La concurrencia la dan los hilos: FastAPI atiende cada peticion en el
# suyo.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
