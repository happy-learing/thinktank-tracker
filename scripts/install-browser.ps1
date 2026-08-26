param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Python runtime not found: $PythonPath"
}
$BrowserRoot = Join-Path $ProjectRoot "runtime\browsers"
New-Item -ItemType Directory -Force -Path $BrowserRoot | Out-Null
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRoot
& $PythonPath -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed with exit code $LASTEXITCODE"
}
$LinkMetadata = Join-Path $BrowserRoot ".links"
if (Test-Path -LiteralPath $LinkMetadata) {
    Remove-Item -LiteralPath $LinkMetadata -Recurse -Force
}
Write-Host "Chromium installed at $BrowserRoot"
