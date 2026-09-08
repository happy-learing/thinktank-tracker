$ErrorActionPreference = "Stop"
$PackageRoot = (Resolve-Path $PSScriptRoot).Path
$ChecksumPath = Join-Path $PackageRoot "SHA256SUMS.txt"
$NoticePath = Join-Path $PackageRoot "THIRD_PARTY_NOTICES.txt"
if (-not (Test-Path -LiteralPath $ChecksumPath)) {
    throw "SHA256SUMS.txt not found."
}
if (-not (Test-Path -LiteralPath $NoticePath)) {
    throw "THIRD_PARTY_NOTICES.txt not found."
}
$ReportTemplatePath = Join-Path $PackageRoot "app\thinktank_tracker\assets\report_template.docx"
if (-not (Test-Path -LiteralPath $ReportTemplatePath)) {
    throw "Built-in report_template.docx not found."
}
$ExampleDirectoryName = -join @(
    [char]0x793A, [char]0x4F8B, [char]0x6587, [char]0x4EF6
)
$ReportExampleName = (-join @(
    [char]0x667A, [char]0x5E93, [char]0x5468, [char]0x89C8,
    [char]0x683C, [char]0x5F0F, [char]0x793A, [char]0x4F8B
)) + ".docx"
$ReportExamplePath = Join-Path `
    (Join-Path $PackageRoot $ExampleDirectoryName) `
    $ReportExampleName
if (-not (Test-Path -LiteralPath $ReportExamplePath)) {
    throw "User-facing report example not found."
}
$FullChromium = @(
    Get-ChildItem -LiteralPath (Join-Path $PackageRoot "runtime\browsers") `
        -File -Filter "chrome.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '[\\/]chromium-\d+[\\/]chrome-win64[\\/]chrome\.exe$' }
)
if ($FullChromium.Count -lt 1) {
    throw "Bundled full Chromium executable not found."
}
$NativeModules = @(Get-ChildItem -LiteralPath $PackageRoot -File -Filter "_anydoc.pyd" -Recurse)
if ($NativeModules.Count -ne 1) {
    throw "Expected exactly one bundled anydoc native module; found $($NativeModules.Count)."
}
$MetadataDirectories = @(
    Get-ChildItem -LiteralPath $PackageRoot -Directory -Filter "firecrawl_anydoc-*.dist-info" -Recurse
)
if ($MetadataDirectories.Count -ne 1) {
    throw "Expected exactly one firecrawl-anydoc metadata directory; found $($MetadataDirectories.Count)."
}
$Failures = @()
foreach ($Line in Get-Content -LiteralPath $ChecksumPath) {
    if (-not $Line.Trim()) { continue }
    $Parts = $Line -split "  ", 2
    $Expected = $Parts[0]
    $Relative = $Parts[1]
    $Target = Join-Path $PackageRoot $Relative
    if (-not (Test-Path -LiteralPath $Target)) {
        $Failures += "Missing: $Relative"
        continue
    }
    $Actual = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) { $Failures += "Mismatch: $Relative" }
}
if ($Failures.Count -gt 0) {
    $Failures | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host "All package files passed SHA-256 verification."
