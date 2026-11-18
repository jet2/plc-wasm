# Build the actual GUI, with cached dependencies and LLVM's MSVC-compatible linker.
param([string]$Project = (Join-Path (Split-Path $PSScriptRoot -Parent) '../wemu'))
$ErrorActionPreference = 'Stop'
$manifest = (Resolve-Path (Join-Path $Project 'Cargo.toml')).Path
$legacy = Test-Path -LiteralPath (Join-Path $Project 'tools/prepare_makepad.py')
$package = 'wemu'
if ($legacy) {
    python (Join-Path $Project 'tools/prepare_makepad.py')
    if ($LASTEXITCODE -ne 0) { throw 'Makepad preparation failed' }
    $package = 'wemulator'
}
$root = Split-Path $PSScriptRoot -Parent
$env:CARGO_HOME = Join-Path $root '.toolchain/cargo'
$env:RUSTUP_HOME = Join-Path $root '.toolchain/rustup'
$rustc = Join-Path $env:CARGO_HOME 'bin/rustc.exe'
$sysroot = & $rustc --print sysroot
if ($LASTEXITCODE -ne 0) { throw 'Rust toolchain is unavailable' }
$source = Join-Path $sysroot 'lib/rustlib/x86_64-pc-windows-msvc/bin/rust-lld.exe'
$linker = Join-Path $root '.toolchain/lld-link.exe'
if (!(Test-Path $linker) -or (Get-FileHash $source).Hash -ne (Get-FileHash $linker).Hash) {
    Copy-Item -LiteralPath $source -Destination $linker -Force
}
# Only the GUI receives this option, avoiding invalidation of all dependency artifacts.
$cargoArgs = @('rustc', '--release', '--locked', '--manifest-path', $manifest, '-p', $package)
if (!$legacy) { $cargoArgs += @('--bin', $package) }
$cargoArgs += @('-j', '2', '--', '-C', "linker=$linker")
& (Join-Path $root 'cargo-local.ps1') @cargoArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($legacy) {
    python (Join-Path $Project 'tools/package_makepad.py')
    exit $LASTEXITCODE
}
