# Build the actual GUI, with cached dependencies and LLVM's MSVC-compatible linker.
param([string]$Project = (Join-Path (Split-Path $PSScriptRoot -Parent) '../WEmulator'))
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$env:CARGO_HOME = Join-Path $root '.toolchain/cargo'
$env:RUSTUP_HOME = Join-Path $root '.toolchain/rustup'
$rustc = Join-Path $env:CARGO_HOME 'bin/rustc.exe'
$sysroot = & $rustc --print sysroot
if ($LASTEXITCODE -ne 0) { throw 'Rust toolchain is unavailable' }
$source = Join-Path $sysroot 'lib/rustlib/x86_64-pc-windows-msvc/bin/rust-lld.exe'
$linker = Join-Path $root '.toolchain/lld-link.exe'
Copy-Item -LiteralPath $source -Destination $linker -Force
# Only the GUI receives this option, avoiding invalidation of all dependency artifacts.
$cargoArgs = @('rustc', '--release', '--locked', '--manifest-path', (Join-Path $Project 'Cargo.toml'), '-p', 'wemulator', '-j', '2', '--', '-C', "linker=$linker")
& (Join-Path $root 'cargo-local.ps1') @cargoArgs
exit $LASTEXITCODE
