$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$env:THINKTANK_BUILD_CONSOLE = "1"
& $PythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath (Join-Path $ProjectRoot "debug-dist") `
    --workpath (Join-Path $ProjectRoot ".debug-build") `
    (Join-Path $ProjectRoot "build\portable.spec")
if ($LASTEXITCODE -ne 0) { throw "Diagnostic build failed." }
