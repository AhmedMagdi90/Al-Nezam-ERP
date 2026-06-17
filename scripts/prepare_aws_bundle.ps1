param(
    [switch]$Zip
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $repoRoot "dist"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$bundleRoot = Join-Path $distRoot "aws_upload_$timestamp"
$zipPath = "$bundleRoot.zip"
$repoRootFull = [System.IO.Path]::GetFullPath($repoRoot)

$excludedDirectories = @(
    ".git",
    ".venv",
    "dist",
    "media",
    "staticfiles",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tmp_environment_access_setup_tests",
    ".tmp_onboarding_planner_tests",
    ".tmp_tenant_provision_tests",
    ".tmp_test_catalog"
)

$excludedFiles = @(
    ".env",
    "db.sqlite3",
    "*.pyc",
    "*.sqlite3",
    "*.log",
    "*.zip"
)

function Test-ExcludedPath {
    param(
        [string]$RelativePath
    )

    $segments = $RelativePath -split "[\\/]"
    foreach ($segment in $segments) {
        if ($excludedDirectories -contains $segment) {
            return $true
        }
    }

    $leaf = Split-Path -Leaf $RelativePath
    foreach ($pattern in $excludedFiles) {
        if ($leaf -like $pattern) {
            return $true
        }
    }

    return $false
}

function Get-RelativeRepoPath {
    param(
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.StartsWith($repoRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $fullPath.Substring($repoRootFull.Length).TrimStart([char[]]@('\', '/'))
    }
    return $fullPath
}

if (-not (Test-Path $distRoot)) {
    New-Item -ItemType Directory -Path $distRoot | Out-Null
}

if (Test-Path $bundleRoot) {
    Remove-Item -LiteralPath $bundleRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $bundleRoot | Out-Null

$files = Get-ChildItem -Path $repoRoot -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
    $relativePath = Get-RelativeRepoPath -Path $_.FullName
    -not (Test-ExcludedPath -RelativePath $relativePath)
}

foreach ($file in $files) {
    $relativePath = Get-RelativeRepoPath -Path $file.FullName
    $destinationPath = Join-Path $bundleRoot $relativePath
    $destinationDir = Split-Path -Parent $destinationPath

    if (-not (Test-Path $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }

    Copy-Item -LiteralPath $file.FullName -Destination $destinationPath -Force
}

if ($Zip) {
    if (Test-Path $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $bundleRootFull = [System.IO.Path]::GetFullPath($bundleRoot)
    $archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -Path $bundleRoot -Recurse -File -Force | ForEach-Object {
            $entryName = [System.IO.Path]::GetFullPath($_.FullName).Substring($bundleRootFull.Length).TrimStart([char[]]@('\', '/'))
            $entryName = $entryName -replace '\\', '/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive,
                $_.FullName,
                $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal
            ) | Out-Null
        }
    } finally {
        $archive.Dispose()
    }
}

Write-Host "Created upload bundle:" $bundleRoot
if ($Zip) {
    Write-Host "Created zip archive:" $zipPath
}
