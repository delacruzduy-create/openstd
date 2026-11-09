# Prepara la versión portable para Windows (una sola vez, con internet):
# baja el Python oficial "embeddable" de python.org y las librerías de PyPI dentro de esta carpeta,
# así el programa corre desde un pendrive en cualquier Windows 10/11 sin instalar nada.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$raiz = Split-Path -Parent $PSScriptRoot
$py = Join-Path $raiz 'python'
$version = '3.12.10'
Write-Host ''
Write-Host '== Preparando OpenSTD Test portable ==' -ForegroundColor Cyan
try {
    if (-not (Test-Path (Join-Path $py 'python.exe'))) {
        Write-Host "1/2 Bajando Python $version portable (unos 11 MB) de python.org..."
        $zip = Join-Path $env:TEMP 'openstd-python.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$version/python-$version-embed-amd64.zip" -OutFile $zip
        New-Item -ItemType Directory -Force -Path $py | Out-Null
        Expand-Archive -Path $zip -DestinationPath $py -Force
        Remove-Item $zip -Force
    } else {
        Write-Host '1/2 Python portable: ya estaba.'
    }
    Write-Host '2/2 Bajando las librerías (PDF, imágenes, sensores) de PyPI...'
    & (Join-Path $py 'python.exe') (Join-Path $PSScriptRoot 'preparar_portable.py')
    if ($LASTEXITCODE -ne 0) { throw 'no se pudieron bajar las librerías' }
    New-Item -ItemType Directory -Force -Path (Join-Path $raiz 'datos') | Out-Null
    Write-Host ''
    Write-Host 'LISTO. Ya se puede usar INICIAR.bat en cualquier PC con Windows 10 u 11 (no necesita internet).' -ForegroundColor Green
} catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'Revisá que haya internet y volvé a ejecutar PREPARAR_PENDRIVE.bat'
    exit 1
}
