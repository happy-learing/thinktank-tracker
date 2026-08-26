param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Version = (Get-Content -LiteralPath (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$BrowserRoot = Join-Path $ProjectRoot "runtime\browsers"
$BuildRoot = Join-Path $ProjectRoot ".build"
$DistRoot = Join-Path $ProjectRoot "dist"
$ReleaseRoot = Join-Path $ProjectRoot "release"
$PackageName = "ThinkTankTracker-win-x64-portable-full-v$Version"
$PackageRoot = Join-Path $DistRoot $PackageName
$SpecPath = Join-Path $ProjectRoot "build\portable.spec"

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "Only Windows x64 builds are supported."
}
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw "Virtual environment not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $BrowserRoot)) {
    throw "Bundled Chromium is missing. Run scripts\install-browser.ps1 first."
}
if (-not $SkipTests) {
    & $PythonPath -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
}

foreach ($Target in @($BuildRoot, $PackageRoot)) {
    $Parent = Split-Path -Parent $Target
    if ($Parent -notin @($ProjectRoot, $DistRoot)) {
        throw "Refusing to clean path outside project build roots: $Target"
    }
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}
New-Item -ItemType Directory -Force -Path $BuildRoot, $DistRoot, $ReleaseRoot | Out-Null

& $PythonPath -m PyInstaller --noconfirm --clean --distpath $DistRoot --workpath $BuildRoot $SpecPath
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
$PyInstallerOutput = Join-Path $DistRoot "ThinkTankTracker"
if (-not (Test-Path -LiteralPath $PyInstallerOutput)) {
    throw "PyInstaller output not found: $PyInstallerOutput"
}
Move-Item -LiteralPath $PyInstallerOutput -Destination $PackageRoot

$RuntimeRoot = Join-Path $PackageRoot "runtime"
$WorkspaceRoot = Join-Path $PackageRoot "workspace"
New-Item -ItemType Directory -Force -Path $RuntimeRoot, $WorkspaceRoot | Out-Null
Copy-Item -LiteralPath $BrowserRoot -Destination (Join-Path $RuntimeRoot "browsers") -Recurse
$BundledLinkMetadata = Join-Path $RuntimeRoot "browsers\.links"
if (Test-Path -LiteralPath $BundledLinkMetadata) {
    Remove-Item -LiteralPath $BundledLinkMetadata -Recurse -Force
}
New-Item -ItemType Directory -Force -Path `
    (Join-Path $WorkspaceRoot "config"), `
    (Join-Path $WorkspaceRoot "cache\downloads"), `
    (Join-Path $WorkspaceRoot "exports"), `
    (Join-Path $WorkspaceRoot "backups"), `
    (Join-Path $WorkspaceRoot "logs") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "VERSION") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $ProjectRoot "requirements.lock") -Destination $PackageRoot
$PortableGuides = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "docs") -File -Filter "*.txt")
if ($PortableGuides.Count -ne 1) {
    throw "Expected exactly one portable guide text file."
}
Copy-Item -LiteralPath $PortableGuides[0].FullName -Destination $PackageRoot
$VerifyName = "$([char]0x9A8C)$([char]0x8BC1)$([char]0x5B8C)$([char]0x6574)$([char]0x6027).ps1"
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\verify-package.ps1") -Destination (Join-Path $PackageRoot $VerifyName)

$BuildInfo = [ordered]@{
    product = "ThinkTankTracker"
    version = $Version
    database_schema = 1
    platform = "Windows x64"
    build_time = (Get-Date).ToUniversalTime().ToString("o")
    python = (& $PythonPath --version 2>&1).ToString()
    dependency_lock = "requirements.lock"
}
$BuildInfo | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PackageRoot "BUILDINFO.json") -Encoding UTF8

$ChecksumPath = Join-Path $PackageRoot "SHA256SUMS.txt"
$Lines = Get-ChildItem -LiteralPath $PackageRoot -File -Recurse |
    Where-Object { $_.FullName -ne $ChecksumPath } |
    Sort-Object FullName |
    ForEach-Object {
        $RootPrefix = $PackageRoot.TrimEnd("\") + "\"
        if (-not $_.FullName.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Package file escaped package root: $($_.FullName)"
        }
        $Relative = $_.FullName.Substring($RootPrefix.Length).Replace("\", "/")
        "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $Relative
    }
$Lines | Set-Content -LiteralPath $ChecksumPath -Encoding UTF8

$ZipPath = Join-Path $ReleaseRoot "$PackageName.zip"
if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Portable package: $ZipPath"
