param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot
)

$ErrorActionPreference = "Stop"
$ResolvedRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$Executables = @(Get-ChildItem -LiteralPath $ResolvedRoot -File -Filter "*.exe")
if ($Executables.Count -ne 1) { throw "Expected exactly one launcher executable." }
$Executable = $Executables[0].FullName
$Previous = $env:THINKTANK_NO_BROWSER
$PreviousPort = $env:THINKTANK_PORT
$Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$Listener.Start()
$Port = ([Net.IPEndPoint]$Listener.LocalEndpoint).Port
$Listener.Stop()
$Url = "http://127.0.0.1:$Port"
$env:THINKTANK_NO_BROWSER = "1"
$env:THINKTANK_PORT = $Port.ToString()
$Process = Start-Process -FilePath $Executable -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -PassThru
try {
    $Deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $Deadline) {
        if ($Process.HasExited) { throw "Portable executable exited early with code $($Process.ExitCode)." }
        try {
            $Health = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 2
            if ($Health.status -eq "ok") { break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $Health -or $Health.status -ne "ok") { throw "Portable server did not become ready within 45 seconds." }
    foreach ($Path in @("/", "/settings", "/diagnostics")) {
        $Page = Invoke-WebRequest -UseBasicParsing -Uri "$Url$Path" -TimeoutSec 20
        if ($Page.StatusCode -ne 200 -or $Page.Content -notmatch "/static/app.css") {
            throw "Portable page smoke test failed: $Path"
        }
    }
    Write-Host "Portable cold start passed: $Url"
} finally {
    if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
    $env:THINKTANK_NO_BROWSER = $Previous
    $env:THINKTANK_PORT = $PreviousPort
}
