$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot "runtime\browsers"
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") `
    (Join-Path $PSScriptRoot "smoke_browser.py") `
    "http://127.0.0.1:8765/" `
    (Join-Path $ProjectRoot "workspace\browser-smoke.png")
if ($LASTEXITCODE -ne 0) { throw "Bundled Chromium smoke test failed." }
