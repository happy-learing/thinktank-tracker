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
$ZipPath = Join-Path $ReleaseRoot "$PackageName.zip"
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
if (Test-Path -LiteralPath $PackageRoot) {
    throw "Versioned package already exists and will not be overwritten: $PackageRoot. Bump VERSION before building."
}
if (Test-Path -LiteralPath $ZipPath) {
    throw "Release ZIP already exists and will not be overwritten: $ZipPath. Bump VERSION before building."
}
if (-not $SkipTests) {
    $TestTempRoot = Join-Path $ProjectRoot "workspace\build-test-temp"
    $TestRunRoot = Join-Path $TestTempRoot ([guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $TestRunRoot | Out-Null
    $PreviousTemp = $env:TEMP
    $PreviousTmp = $env:TMP
    try {
        $env:TEMP = $TestRunRoot
        $env:TMP = $TestRunRoot
        & $PythonPath -m pytest --basetemp (Join-Path $TestRunRoot "pytest")
        if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
    }
    finally {
        $env:TEMP = $PreviousTemp
        $env:TMP = $PreviousTmp
    }
}

foreach ($Target in @($BuildRoot)) {
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
$MoveSucceeded = $false
for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
    try {
        Move-Item -LiteralPath $PyInstallerOutput -Destination $PackageRoot -ErrorAction Stop
        $MoveSucceeded = $true
        break
    }
    catch {
        if ($Attempt -eq 5) { throw }
        Start-Sleep -Seconds 2
    }
}
if (-not $MoveSucceeded) {
    throw "Could not move PyInstaller output into the versioned package directory."
}

$RuntimeRoot = Join-Path $PackageRoot "runtime"
$WorkspaceRoot = Join-Path $PackageRoot "workspace"
New-Item -ItemType Directory -Force -Path $RuntimeRoot, $WorkspaceRoot | Out-Null
$BundledBrowserRoot = Join-Path $RuntimeRoot "browsers"
New-Item -ItemType Directory -Force -Path $BundledBrowserRoot | Out-Null
$BrowserComponents = @(
    Get-ChildItem -LiteralPath $BrowserRoot -Directory |
        Where-Object { $_.Name -match '^(chromium|chromium_headless_shell|ffmpeg|winldd)-\d+$' }
)
if (-not ($BrowserComponents | Where-Object { $_.Name -match '^chromium-\d+$' })) {
    throw "Playwright full Chromium is missing. Run scripts\install-browser.ps1 first."
}
foreach ($Component in $BrowserComponents) {
    Copy-Item -LiteralPath $Component.FullName -Destination $BundledBrowserRoot -Recurse
}
New-Item -ItemType Directory -Force -Path `
    (Join-Path $WorkspaceRoot "config"), `
    (Join-Path $WorkspaceRoot "cache\downloads"), `
    (Join-Path $WorkspaceRoot "exports"), `
    (Join-Path $WorkspaceRoot "backups"), `
    (Join-Path $WorkspaceRoot "logs") | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "VERSION") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $ProjectRoot "requirements.lock") -Destination $PackageRoot
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.txt") -Destination $PackageRoot
$BuildSupportRoot = Join-Path $PackageRoot "build-support"
New-Item -ItemType Directory -Force -Path $BuildSupportRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\build-portable.ps1") -Destination $BuildSupportRoot
Copy-Item -LiteralPath (Join-Path $ProjectRoot "build\portable.spec") -Destination $BuildSupportRoot
Copy-Item -LiteralPath (Join-Path $ProjectRoot "pyproject.toml") -Destination $BuildSupportRoot
$PortableGuides = @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "docs") -File -Filter "*.txt")
if ($PortableGuides.Count -ne 1) {
    throw "Expected exactly one portable guide text file."
}
Copy-Item -LiteralPath $PortableGuides[0].FullName -Destination $PackageRoot
$ExampleDirectoryName = -join @(
    [char]0x793A, [char]0x4F8B, [char]0x6587, [char]0x4EF6
)
$ReportExampleName = (-join @(
    [char]0x667A, [char]0x5E93, [char]0x5468, [char]0x89C8,
    [char]0x683C, [char]0x5F0F, [char]0x793A, [char]0x4F8B
)) + ".docx"
$ReportExampleSource = Join-Path `
    (Join-Path (Join-Path $ProjectRoot "docs") $ExampleDirectoryName) `
    $ReportExampleName
if (-not (Test-Path -LiteralPath $ReportExampleSource)) {
    throw "User-facing report example not found: $ReportExampleSource"
}
$ExampleRoot = Join-Path $PackageRoot $ExampleDirectoryName
New-Item -ItemType Directory -Force -Path $ExampleRoot | Out-Null
Copy-Item -LiteralPath $ReportExampleSource -Destination $ExampleRoot
$VerifyName = "$([char]0x9A8C)$([char]0x8BC1)$([char]0x5B8C)$([char]0x6574)$([char]0x6027).ps1"
Copy-Item -LiteralPath (Join-Path $ProjectRoot "scripts\verify-package.ps1") -Destination (Join-Path $PackageRoot $VerifyName)

$BuildInfo = [ordered]@{
    product = "ThinkTankTracker"
    version = $Version
    database_schema = 10
    platform = "Windows x64"
    build_time = (Get-Date).ToUniversalTime().ToString("o")
    python = (& $PythonPath --version 2>&1).ToString()
    dependency_lock = "requirements.lock"
    pdf_parser = "firecrawl-anydoc==0.2.4 with pypdf fallback"
    browser_runtime = "chromium (full)"
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

Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Portable package: $ZipPath"
