# Arranca la plataforma con base de datos y bucket SIN guardar credenciales en archivos (texto en ASCII a proposito):
# las pide ocultas, las deja solo en esta ventana y las borra al terminar.
# Uso:  & .\iniciar_con_bd.ps1                 (servidor del tablero en vivo)
#       & .\iniciar_con_bd.ps1 -Accion cargar  (carga la linea base a PostgreSQL; desde prototipo\plataforma)
#       & .\iniciar_con_bd.ps1 -Accion subir   (sube las evidencias al bucket; desde prototipo\plataforma)
#       & .\iniciar_con_bd.ps1 -Accion usuario -Datos 'alta','correo@guadalajara.gob.mx','Nombre','operador'
#                                               (cuentas de gobierno; la contrasena la escribe la persona)
param([ValidateSet('servidor', 'cargar', 'subir', 'usuario')][string]$Accion = 'servidor', [switch]$SinBucket, [string[]]$Datos = @())
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
function Leer-Oculto($texto) {
    $s = Read-Host -AsSecureString $texto
    return [System.Net.NetworkCredential]::new('', $s).Password
}
$nombres = @('DATABASE_URL', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_ENDPOINT_URL_S3', 'AWS_REGION', 'BUCKET')
try {
    $env:DATABASE_URL = Leer-Oculto "Pega la cadena de conexion de PostgreSQL (postgresql://...)"
    if (-not $SinBucket -and $Accion -notin @('cargar', 'usuario')) {
        $usar = Read-Host "Configurar el bucket de evidencias? (s/n)"
        if ($usar -eq 's') {
            $env:AWS_ENDPOINT_URL_S3 = Read-Host "Endpoint S3 (https://...)"
            $env:AWS_REGION = Read-Host "Region (p. ej. us-east-1 o auto)"
            $env:BUCKET = Read-Host "Nombre del bucket (Enter = reportbucket)"
            if (-not $env:BUCKET) { $env:BUCKET = 'reportbucket' }
            $env:AWS_ACCESS_KEY_ID = Leer-Oculto "Access key ID"
            $env:AWS_SECRET_ACCESS_KEY = Leer-Oculto "Secret access key"
        }
    }
    switch ($Accion) {
        'servidor' { python servidor.py }
        'cargar'   { python cargar_bd.py }
        'subir'    { python subir_evidencias.py }
        'usuario'  { python crear_usuario.py @Datos }
    }
} finally {
    foreach ($n in $nombres) { Remove-Item "Env:$n" -ErrorAction SilentlyContinue }
}
