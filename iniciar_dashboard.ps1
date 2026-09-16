# =============================================================================
# Iniciar el Tablero de Descargas Atmosfericas - GeoPark Llanos 34
#
# Uso normal: doble clic en Iniciar_Dashboard.bat (muestra el menu).
#
# Desde una terminal, saltandose el menu:
#   powershell -ExecutionPolicy Bypass -File .\iniciar_dashboard.ps1 -Fuente parquet
#   powershell -ExecutionPolicy Bypass -File .\iniciar_dashboard.ps1 -Fuente supabase
#   powershell -ExecutionPolicy Bypass -File .\iniciar_dashboard.ps1 -Detener
#   powershell -ExecutionPolicy Bypass -File .\iniciar_dashboard.ps1 -Fuente parquet -Reconstruir
#
# Que hace:
#   1. Pregunta de donde salen los datos (parquet local o Supabase).
#   2. Verifica que Docker Desktop este corriendo (si no, lo abre y lo espera).
#   3. Levanta el contenedor GPK_Tablero_Web con esa fuente.
#   4. Comprueba que el tablero responda de verdad antes de decir que esta listo.
#   5. Abre http://localhost:8080 en el navegador.
# =============================================================================

param(
    # Salta el menu y arranca directo con esta fuente.
    [ValidateSet("parquet", "supabase")]
    [string]$Fuente,
    # Reconstruye la imagen. Solo hace falta si cambio el Dockerfile o requirements.txt.
    [switch]$Reconstruir,
    # Apaga el tablero y termina.
    [switch]$Detener,
    # No abrir el navegador al final.
    [switch]$SinNavegador
)

# "Continue" y no "Stop": docker escribe su progreso normal por stderr, y en
# Windows PowerShell 5.1 eso se convierte en un error fatal con "Stop".
# Los fallos reales se detectan con los codigos de salida.
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# --- Configuracion ---------------------------------------------------------
$Compose       = Join-Path $PSScriptRoot "docker-compose.yml"
$ArchivoEnv    = Join-Path $PSScriptRoot ".env"
$Contenedor    = "GPK_Tablero_Web"
$UrlTablero    = "http://localhost:8080"
$UrlFuente     = "$UrlTablero/api/fuente-datos"
$DockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

$EsperaDockerSeg       = 180
$EsperaTableroSeg      = 120   # desde que docker compose termina
$EsperaConstruccionSeg = 1800  # tope para descargar y construir la imagen

function Escribir-Paso($texto)  { Write-Host "  > $texto" -ForegroundColor Cyan }
function Escribir-Ok($texto)    { Write-Host "  [OK] $texto" -ForegroundColor Green }
function Escribir-Aviso($texto) { Write-Host "  [!] $texto" -ForegroundColor Yellow }

function Salir-Con-Error($texto, $pista) {
    Write-Host ""
    Write-Host "  [ERROR] $texto" -ForegroundColor Red
    if ($pista) { Write-Host "          $pista" -ForegroundColor DarkYellow }
    Write-Host ""
    exit 1
}

function Escribir-Encabezado {
    Write-Host ""
    Write-Host "  ============================================================" -ForegroundColor DarkCyan
    Write-Host "   Tablero de Descargas Atmosfericas  |  GeoPark Llanos 34" -ForegroundColor White
    Write-Host "  ============================================================" -ForegroundColor DarkCyan
    Write-Host ""
}

function Docker-Responde {
    & docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

# Lee una clave del .env. Sirve para avisar ANTES de arrancar que falta la URL
# de Supabase: sin ella el backend arranca, se cae solo, y el usuario solo veria
# un contenedor que se apaga sin explicacion.
function Leer-Env($clave) {
    if (-not (Test-Path $ArchivoEnv)) { return $null }
    foreach ($linea in (Get-Content $ArchivoEnv)) {
        $t = $linea.Trim()
        if ($t.StartsWith("#") -or -not $t.Contains("=")) { continue }
        $i = $t.IndexOf("=")
        if ($t.Substring(0, $i).Trim() -ne $clave) { continue }
        $valor = $t.Substring($i + 1).Trim()
        # Quita el comentario del final ("parquet    #parquet o supabase")
        $corte = $valor.IndexOf("#")
        if ($corte -ge 0) { $valor = $valor.Substring(0, $corte).Trim() }
        return $valor.Trim([char]34).Trim([char]39)
    }
    return $null
}

# --- Menu ------------------------------------------------------------------
function Preguntar-Fuente {
    while ($true) {
        Clear-Host
        Escribir-Encabezado
        Write-Host "   De donde quieres que salgan los datos?" -ForegroundColor White
        Write-Host ""
        Write-Host -NoNewline "     [1]  Parquet" -ForegroundColor Green
        Write-Host "            Archivo local en datos/ (no va a git)." -ForegroundColor Gray
        Write-Host "                            Arranca rapido y NO gasta cupo de" -ForegroundColor DarkGray
        Write-Host "                            Supabase. Para pruebas y mejoras." -ForegroundColor DarkGray
        Write-Host ""
        Write-Host -NoNewline "     [2]  Base de datos" -ForegroundColor Yellow
        Write-Host "      Supabase (Postgres) en vivo." -ForegroundColor Gray
        Write-Host "                            Igual que produccion. CONSUME egress" -ForegroundColor DarkGray
        Write-Host "                            del plan gratuito en cada arranque." -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "     [3]  Detener el tablero" -ForegroundColor DarkGray
        Write-Host "     [0]  Salir" -ForegroundColor DarkGray
        Write-Host ""
        $opcion = (Read-Host "   Opcion").Trim()

        switch ($opcion) {
            "1" { return "parquet" }
            "2" { if (Confirmar-Supabase) { return "supabase" } }
            "3" { return "detener" }
            "0" { return $null }
            default {
                Escribir-Aviso "Opcion no valida: escribe 1, 2, 3 o 0."
                Start-Sleep -Seconds 2
            }
        }
    }
}

# El modo Supabase gasta cupo del plan gratuito, asi que no se entra sin querer.
function Confirmar-Supabase {
    Clear-Host
    Escribir-Encabezado
    Write-Host "   Modo BASE DE DATOS (Supabase)" -ForegroundColor Yellow
    Write-Host "   ------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "   El tablero arranca igual que en produccion: lee la foto de su" -ForegroundColor Gray
    Write-Host "   cache guardada en Supabase y baja solo los dias nuevos." -ForegroundColor Gray
    Write-Host ""
    Write-Host "   Ten en cuenta:" -ForegroundColor White
    Write-Host "     - Consume egress del plan gratuito (5 GB al mes): ~0,75 MB" -ForegroundColor DarkYellow
    Write-Host "       por arranque con foto, ~3,9 MB si todavia no hay foto." -ForegroundColor DarkYellow
    Write-Host "     - Es el modo de produccion: sirve para demos y para" -ForegroundColor Gray
    Write-Host "       comprobar que la conexion con la base funciona." -ForegroundColor Gray
    Write-Host "     - Para el trabajo normal alcanza con la opcion 1," -ForegroundColor Gray
    Write-Host "       que no gasta nada." -ForegroundColor Gray
    Write-Host ""

    if (-not (Leer-Env "SUPABASE_DASHBOARD_DB_URL")) {
        Write-Host "   [ERROR] Falta SUPABASE_DASHBOARD_DB_URL en el archivo .env" -ForegroundColor Red
        Write-Host "           Sin esa direccion el tablero no puede conectarse." -ForegroundColor DarkYellow
        Write-Host ""
        Read-Host "   Enter para volver al menu" | Out-Null
        return $false
    }

    $r = (Read-Host "   Conectar a la base de datos? (s/N)").Trim().ToLower()
    return ($r -eq "s" -or $r -eq "si")
}

# El parquet es solo local y no esta en git: en un clon nuevo no existe. Sin
# esta comprobacion el tablero arranca, la pagina carga y el lanzador dice
# "listo", pero cada consulta de datos falla.
function Comprobar-Parquet {
    $parquet = Join-Path $PSScriptRoot "datos\Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet"
    if (Test-Path $parquet) { return }
    Salir-Con-Error "Falta el parquet local: datos\Gold_Consolidado_Historico_Descargas_Electricas_GPK.parquet" `
        "No esta en git. Generalo con: python scripts\actualizar_parquet.py (usa RUTA_GOLD_PIPELINE del .env)"
}

# --- Apagado ---------------------------------------------------------------
function Detener-Tablero {
    Escribir-Paso "Deteniendo el tablero ..."
    & docker compose -f "$Compose" down
    if ($LASTEXITCODE -ne 0) {
        Salir-Con-Error "No se pudo detener el contenedor." "Prueba a mano: docker compose -f `"$Compose`" down"
    }
    Write-Host ""
    Escribir-Ok "El tablero esta apagado."
    Write-Host ""
    exit 0
}

# --- Arranque --------------------------------------------------------------
Clear-Host
Escribir-Encabezado

if (-not (Test-Path $Compose)) {
    Salir-Con-Error "No se encontro $Compose" "Este script debe quedarse en la raiz del proyecto, junto a docker-compose.yml."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Salir-Con-Error "Docker no esta instalado o no esta en el PATH." "Instala Docker Desktop: https://www.docker.com/products/docker-desktop/"
}

# 1. Que fuente
if ($Detener) {
    $eleccion = "detener"
} elseif ($Fuente) {
    $eleccion = $Fuente
} else {
    $eleccion = Preguntar-Fuente
}

if (-not $eleccion) {
    Write-Host ""
    Write-Host "   Cancelado. No se levanto nada." -ForegroundColor DarkGray
    Write-Host ""
    exit 0
}
if ($eleccion -eq "parquet") {
    Comprobar-Parquet
}

Clear-Host
Escribir-Encabezado

# 2. Docker Desktop
Escribir-Paso "Verificando Docker Desktop ..."
if (-not (Docker-Responde)) {
    if (-not (Test-Path $DockerDesktop)) {
        Salir-Con-Error "Docker Desktop no esta corriendo y no se encontro en $DockerDesktop" "Abrelo a mano y vuelve a ejecutar este script."
    }
    Escribir-Aviso "Docker Desktop estaba cerrado. Abriendolo, espera un momento ..."
    Start-Process $DockerDesktop
    $inicioDocker = Get-Date
    while (-not (Docker-Responde)) {
        $seg = [int]((Get-Date) - $inicioDocker).TotalSeconds
        if ($seg -ge $EsperaDockerSeg) {
            Salir-Con-Error "Docker Desktop no respondio despues de $EsperaDockerSeg segundos." "Revisa que haya terminado de abrir y vuelve a ejecutar el script."
        }
        Write-Host -NoNewline "`r    Esperando a Docker Desktop ... $seg s   "
        Start-Sleep -Seconds 3
    }
    Write-Host ""
}
Escribir-Ok "Docker Desktop esta corriendo."

if ($eleccion -eq "detener") {
    Write-Host ""
    Detener-Tablero
}

# 3. Levantar el contenedor
#
# La fuente viaja como variable de entorno del proceso, no editando el .env: una
# variable del entorno le gana al archivo tanto en docker compose como en
# python-dotenv, asi que el .env del usuario queda intacto y el modo elegido
# manda igual.
$env:FUENTE_DATOS = $eleccion
if ($eleccion -eq "supabase") {
    $etiqueta = "base de datos (Supabase)"
} else {
    $etiqueta = "parquet local"
}

Write-Host ""
if ($Reconstruir) {
    Escribir-Paso "Reconstruyendo la imagen y levantando el tablero con $etiqueta ..."
    Escribir-Aviso "La primera construccion puede tardar varios minutos."
    $argumentos = @("compose", "-f", "$Compose", "up", "-d", "--build")
    $tope = $EsperaConstruccionSeg
} else {
    Escribir-Paso "Levantando el tablero con $etiqueta ..."
    $argumentos = @("compose", "-f", "$Compose", "up", "-d")
    $tope = $EsperaTableroSeg
}
Write-Host ""

$inicio = Get-Date
& docker @argumentos
if ($LASTEXITCODE -ne 0) {
    Salir-Con-Error "docker compose no pudo levantar el contenedor." "El detalle de Docker esta justo arriba."
}

# 4. Esperar a que el contenedor este corriendo
Write-Host ""
Escribir-Paso "Esperando a que el contenedor arranque ..."
$corriendo = $false
while ([int]((Get-Date) - $inicio).TotalSeconds -lt $tope) {
    $estado = & docker inspect -f "{{.State.Status}}|{{.State.ExitCode}}" $Contenedor 2>$null
    if ($LASTEXITCODE -eq 0 -and $estado) {
        $p = ($estado | Select-Object -First 1).Trim().Split("|")
        if ($p[0] -eq "running") { $corriendo = $true; break }
        # Si el backend se cae al arrancar (por ejemplo, FUENTE_DATOS=supabase
        # con una URL que no sirve) el contenedor termina solo: no tiene sentido
        # seguir esperandolo.
        if ($p[0] -eq "exited" -or $p[0] -eq "dead") {
            Write-Host ""
            & docker logs --tail 15 $Contenedor 2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            Salir-Con-Error "El contenedor se detuvo solo (codigo $($p[1]))." "El motivo esta en las lineas de arriba."
        }
    }
    Start-Sleep -Seconds 2
}
if (-not $corriendo) {
    Salir-Con-Error "El contenedor no arranco a tiempo." "Ver el motivo: docker logs $Contenedor"
}
Escribir-Ok "Contenedor $Contenedor levantado."

# 5. Comprobar que el tablero responda de verdad
Escribir-Paso "Comprobando que el tablero responda ..."
$webOk = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri $UrlTablero -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $webOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $webOk) {
    Salir-Con-Error "El contenedor esta arriba, pero $UrlTablero no responde." "Ver detalle: docker logs $Contenedor"
}

# La fuente que el propio backend dice tener: es la confirmacion real de que el
# modo elegido quedo activo. Si todavia esta cargando los datos este endpoint
# aun no responde, y entonces simplemente no se muestra el aviso.
$fuenteReal = $null
try {
    $fuenteReal = (Invoke-RestMethod -Uri $UrlFuente -TimeoutSec 5).fuente
} catch { }

# --- Listo -----------------------------------------------------------------
$seg = [int]((Get-Date) - $inicio).TotalSeconds
Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Tablero listo en $seg segundos." -ForegroundColor Green
Write-Host ""
Write-Host -NoNewline "   Entra en:     " -ForegroundColor White
Write-Host $UrlTablero -ForegroundColor Yellow
Write-Host -NoNewline "   Fuente:       " -ForegroundColor White
if ($eleccion -eq "supabase") {
    Write-Host "base de datos Supabase (consumiendo egress)" -ForegroundColor Yellow
} else {
    Write-Host "parquet local (sin gasto de egress)" -ForegroundColor Green
}
if ($fuenteReal -and $fuenteReal -ne $eleccion) {
    Write-Host -NoNewline "   Aviso:        " -ForegroundColor White
    Write-Host "el backend reporta la fuente '$fuenteReal'" -ForegroundColor Red
}
Write-Host -NoNewline "   Contenedor:   " -ForegroundColor White
Write-Host $Contenedor -ForegroundColor Gray
Write-Host ""
Write-Host "   Para apagarlo: vuelve a ejecutar este lanzador y elige [3]." -ForegroundColor DarkGray
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""

if (-not $SinNavegador) {
    Start-Process $UrlTablero
}
