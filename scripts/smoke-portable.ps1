param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot
)

$ErrorActionPreference = "Stop"
$ResolvedRoot = (Resolve-Path -LiteralPath $PackageRoot).Path
$Executables = @(Get-ChildItem -LiteralPath $ResolvedRoot -File -Filter "*.exe")
if ($Executables.Count -ne 1) { throw "Expected exactly one launcher executable." }
$Executable = $Executables[0].FullName
$BuildInfoPath = Join-Path $ResolvedRoot "BUILDINFO.json"
if (-not (Test-Path -LiteralPath $BuildInfoPath)) { throw "BUILDINFO.json not found." }
$ExpectedSchema = [int]((Get-Content -LiteralPath $BuildInfoPath -Raw | ConvertFrom-Json).database_schema)
$PreviousPdfSelfTest = $env:THINKTANK_PDF_SELF_TEST
try {
    $env:THINKTANK_PDF_SELF_TEST = "1"
    $PdfSelfTest = Start-Process -FilePath $Executable -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -PassThru -Wait
    if ($PdfSelfTest.ExitCode -ne 0) {
        throw "Portable AnyDoc native module and pypdf fallback self-test failed with code $($PdfSelfTest.ExitCode)."
    }
} finally {
    $env:THINKTANK_PDF_SELF_TEST = $PreviousPdfSelfTest
}
$PreviousBrowserSelfTest = $env:THINKTANK_BROWSER_SELF_TEST
try {
    $env:THINKTANK_BROWSER_SELF_TEST = "1"
    $BrowserSelfTest = Start-Process -FilePath $Executable -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -PassThru
    if (-not $BrowserSelfTest.WaitForExit(60000)) {
        Stop-Process -Id $BrowserSelfTest.Id -Force
        throw "Portable Scrapling loopback self-test exceeded 60 seconds."
    }
    if ($BrowserSelfTest.ExitCode -ne 0) {
        throw "Portable Scrapling driver and fingerprint data self-test failed with code $($BrowserSelfTest.ExitCode)."
    }
    Write-Host "Portable Scrapling loopback browser self-test passed."
} finally {
    $env:THINKTANK_BROWSER_SELF_TEST = $PreviousBrowserSelfTest
}
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
            if ($Health.status -eq "ok" -and $Health.database_version -eq $ExpectedSchema) { break }
        } catch { }
        Start-Sleep -Milliseconds 250
    }
    if (-not $Health -or $Health.status -ne "ok" -or $Health.database_version -ne $ExpectedSchema) { throw "Portable server did not become ready with database schema $ExpectedSchema within 45 seconds." }
    foreach ($Path in @("/", "/sources", "/settings", "/diagnostics")) {
        $Page = Invoke-WebRequest -UseBasicParsing -Uri "$Url$Path" -TimeoutSec 20
        if ($Page.StatusCode -ne 200 -or $Page.Content -notmatch "/static/app.css") {
            throw "Portable page smoke test failed: $Path"
        }
    }
    $Diagnostics = Invoke-WebRequest -UseBasicParsing -Uri "$Url/diagnostics" -TimeoutSec 45
    $ChromiumLabel = "$([char]0x6346)$([char]0x7ED1) Chromium"
    $ChromiumPattern = '(?s)<strong>' + [regex]::Escape($ChromiumLabel) + '</strong><span>(?<detail>.*?)</span><span class="status [^"]+">(?<status>[^<]+)</span>'
    $ChromiumResult = [regex]::Match(
        $Diagnostics.Content,
        $ChromiumPattern
    )
    if (-not $ChromiumResult.Success) {
        throw "Portable bundled Chromium diagnostic row was not found."
    }
    if ($ChromiumResult.Groups["status"].Value -ne "PASS") {
        $ChromiumDetail = [Net.WebUtility]::HtmlDecode($ChromiumResult.Groups["detail"].Value)
        throw "Portable bundled Chromium launch probe failed: $ChromiumDetail"
    }
    $SecondProcess = Start-Process -FilePath $Executable -WorkingDirectory $ResolvedRoot -WindowStyle Hidden -PassThru
    if (-not $SecondProcess.WaitForExit(10000)) {
        Stop-Process -Id $SecondProcess.Id -Force
        throw "Second portable launch did not return after detecting the running instance."
    }
    if ($SecondProcess.ExitCode -ne 0) {
        throw "Second portable launch exited with code $($SecondProcess.ExitCode)."
    }
    $HealthAfterSecondLaunch = Invoke-RestMethod -Uri "$Url/health" -TimeoutSec 5
    if ($HealthAfterSecondLaunch.status -ne "ok" -or $HealthAfterSecondLaunch.database_version -ne $ExpectedSchema) {
        throw "First portable instance stopped responding after the second launch."
    }
    Write-Host "Portable repeated launch passed: second process returned and first instance stayed healthy."
    Write-Host "Portable cold start passed: $Url"
} finally {
    if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
    $env:THINKTANK_NO_BROWSER = $Previous
    $env:THINKTANK_PORT = $PreviousPort
}
