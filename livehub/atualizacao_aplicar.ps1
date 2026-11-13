# Aplica uma atualizacao do LiveHub. Roda FORA da pasta de instalacao (copiado para %APPDATA%\LiveHub\update).
param(
    [Parameter(Mandatory)][int]$ProcId,
    [Parameter(Mandatory)][string]$Install,
    [Parameter(Mandatory)][string]$Staged,
    [Parameter(Mandatory)][string]$Marker,
    [Parameter(Mandatory)][string]$Log,
    [int]$TimeoutOk = 60,
    [string]$ExeName = "LiveHub.exe"
)
$ErrorActionPreference = "Continue"
Set-Location $env:TEMP   # nunca segurar handle dentro da pasta de instalacao
$Install = $Install.TrimEnd('\', '/')
$old = "$Install.old"

function Log($m) { try { Add-Content -LiteralPath $Log -Value ("{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 } catch {} }
function Abrir($dir) {
    $exe = Join-Path $dir $ExeName
    if ($exe -like "*.cmd" -or $exe -like "*.bat") { Start-Process -FilePath $env:ComSpec -ArgumentList "/c", "`"$exe`"" -WindowStyle Hidden }
    else { Start-Process -FilePath $exe -WorkingDirectory $dir }
}

Log "inicio: pid=$ProcId install=$Install staged=$Staged"

# 1. espera o LiveHub fechar
$p = Get-Process -Id $ProcId -ErrorAction SilentlyContinue
if ($p) {
    if (-not $p.WaitForExit(60000)) { Log "processo nao saiu em 60s; forcando"; Stop-Process -Id $ProcId -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
}
Log "processo encerrado"

# 2. Install -> Install.old (com tentativas: Explorer/antivirus podem segurar a pasta)
if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    try { Rename-Item -LiteralPath $Install -NewName (Split-Path $old -Leaf) -ErrorAction Stop; $ok = $true }
    catch { Log "rename tentativa $($i + 1) falhou: $($_.Exception.Message)"; Start-Sleep -Seconds 1 }
}
if (-not $ok) { Log "nao consegui liberar a pasta; mantendo versao atual"; Abrir $Install; exit 1 }

# 3. Staged -> Install (Move-Item de pasta falha entre discos: staged fica em %APPDATA%, o app pode estar em D:\ etc.)
try {
    $mesmoDisco = [IO.Path]::GetPathRoot($Staged) -eq [IO.Path]::GetPathRoot($Install)
    if ($mesmoDisco) { Move-Item -LiteralPath $Staged -Destination $Install -ErrorAction Stop }
    else { Copy-Item -LiteralPath $Staged -Destination $Install -Recurse -Force -ErrorAction Stop }
    if (-not (Test-Path -LiteralPath (Join-Path $Install $ExeName))) { throw "executavel ausente apos copiar" }
}
catch {
    Log "mover staged falhou: $($_.Exception.Message); voltando a versao anterior"
    if (Test-Path -LiteralPath $Install) { Remove-Item -LiteralPath $Install -Recurse -Force -ErrorAction SilentlyContinue }
    Rename-Item -LiteralPath $old -NewName (Split-Path $Install -Leaf)
    Abrir $Install; exit 1
}
Log "nova versao no lugar"

# 4. abre e espera a confirmacao (o app novo grava o marcador quando sobe)
Remove-Item -LiteralPath $Marker -Force -ErrorAction SilentlyContinue
Abrir $Install
$fim = (Get-Date).AddSeconds($TimeoutOk)
while ((Get-Date) -lt $fim -and -not (Test-Path -LiteralPath $Marker)) { Start-Sleep -Milliseconds 500 }
if (Test-Path -LiteralPath $Marker) {
    Log "confirmado; removendo versao antiga"
    Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
    Log "ok"
    exit 0
}
Log "rollback: nova versao nao confirmou em ${TimeoutOk}s"
$prefixo = $Install + "\"
Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($prefixo, [StringComparison]::OrdinalIgnoreCase) } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
for ($i = 0; $i -lt 15; $i++) {
    Remove-Item -LiteralPath $Install -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $Install)) { break }
    Start-Sleep -Seconds 1
}
Rename-Item -LiteralPath $old -NewName (Split-Path $Install -Leaf)
Abrir $Install
Log "rollback concluido"
exit 2
