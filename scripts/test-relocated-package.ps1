$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$Version = (Get-Content -LiteralPath (Join-Path $ProjectRoot "VERSION") -Raw).Trim()
$ZipPath = Join-Path $ProjectRoot "release\ThinkTankTracker-win-x64-portable-full-v$Version.zip"
$QaParent = Join-Path $ProjectRoot ".portable-qa"
$TargetName = "$([char]0x4E2D)$([char]0x6587) $([char]0x8DEF)$([char]0x5F84)"
$TargetRoot = Join-Path $QaParent $TargetName
$NormalizedTarget = [IO.Path]::GetFullPath($TargetRoot)
$NormalizedProject = [IO.Path]::GetFullPath($ProjectRoot) + [IO.Path]::DirectorySeparatorChar
if (-not $NormalizedTarget.StartsWith($NormalizedProject, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use relocation test path outside project: $NormalizedTarget"
}
if (Test-Path -LiteralPath $QaParent) {
    Remove-Item -LiteralPath $QaParent -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
try {
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $TargetRoot
    $PackageRoot = Get-ChildItem -LiteralPath $TargetRoot -Directory | Select-Object -First 1
    if (-not $PackageRoot) { throw "Extracted package directory not found." }
    & (Join-Path $PSScriptRoot "smoke-portable.ps1") -PackageRoot $PackageRoot.FullName
    Write-Host "Relocated package passed: $($PackageRoot.FullName)"
} finally {
    if (Test-Path -LiteralPath $QaParent) {
        Remove-Item -LiteralPath $QaParent -Recurse -Force
    }
}
