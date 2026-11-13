# Gera dist\LiveHub\ e dist\LiveHub-<versao>-windows.zip
param([switch]$SemAssinatura)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$version = (& $py -c "import livehub; print(livehub.__version__)").Trim()

foreach ($d in "build", "dist") {
    $p = Join-Path $PSScriptRoot $d
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}

& $py -m PyInstaller LiveHub.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou" }

Copy-Item LEIA-ME.txt "dist\LiveHub\LEIA-ME.txt"
$zip = "dist\LiveHub-$version-windows.zip"
Compress-Archive -Path "dist\LiveHub" -DestinationPath $zip -Force
"{0}  ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB)
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$line = "$hash  LiveHub-$version-windows.zip"
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "$zip.sha256"), $line + "`n", (New-Object System.Text.ASCIIEncoding))
$line
if ($SemAssinatura) {
    Write-Warning "BUILD SEM ASSINATURA: NAO PUBLIQUE este build (o auto-update dos usuarios vai recusa-lo)."
} else {
    & $py tools/assinar.py $zip $version
    if ($LASTEXITCODE -ne 0) { throw "Assinatura falhou" }
    "{0}.sig: {1}" -f $zip, (Get-Content "$zip.sig" -Raw).Trim()
}
