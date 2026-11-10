# Gera dist\LiveHub\ e dist\LiveHub-<versao>-windows.zip
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
