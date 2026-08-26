$env:THINKTANK_NO_BROWSER = "1"
$env:THINKTANK_PORT = "61327"
& (Join-Path (Split-Path -Parent $PSScriptRoot) "debug-dist\ThinkTankTrackerDebug\ThinkTankTrackerDebug.exe")
