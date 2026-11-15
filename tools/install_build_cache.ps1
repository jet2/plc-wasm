# Installs a pinned official sccache release locally; never changes system PATH.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$version = '0.19.0'
$archive = "sccache-v$version-x86_64-pc-windows-msvc.zip"
$base = "https://github.com/mozilla/sccache/releases/download/v$version/$archive"
$directory = Join-Path $root '.toolchain'
New-Item -ItemType Directory -Force $directory | Out-Null
$zip = Join-Path $directory $archive
$checksum = "$zip.sha256"
Invoke-WebRequest $base -OutFile $zip
Invoke-WebRequest "$base.sha256" -OutFile $checksum
$expected = (Get-Content $checksum -Raw).Trim().Split(' ')[0]
if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $expected) {
    throw 'sccache SHA-256 verification failed'
}
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $directory 'sccache') -Force
Write-Output 'sccache installed; cargo-local.ps1 will use it automatically.'
