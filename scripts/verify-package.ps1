$ErrorActionPreference = "Stop"
$PackageRoot = (Resolve-Path $PSScriptRoot).Path
$ChecksumPath = Join-Path $PackageRoot "SHA256SUMS.txt"
if (-not (Test-Path -LiteralPath $ChecksumPath)) {
    throw "SHA256SUMS.txt not found."
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
