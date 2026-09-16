@echo off
REM Doble clic para levantar el Tablero de Descargas Atmosfericas.
REM Muestra un menu para elegir la fuente de datos (parquet o base de datos),
REM levanta el contenedor y abre el tablero en el navegador. La ventana queda
REM abierta al final para que puedas leer el resultado.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0iniciar_dashboard.ps1"
echo.
pause
