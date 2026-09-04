FROM python:3.11-slim

WORKDIR /app

# No escribir .pyc y no bufferizar la salida: los logs de Render se ven en vivo
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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

# Un solo worker a proposito: cada worker carga su propia copia del parquet y
# del arbol espacial (~340 MB de pico medido), y en el plan Free de Render solo
# hay 512 MB. Con dos workers el servicio se queda sin memoria.
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
