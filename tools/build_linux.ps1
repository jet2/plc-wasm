# Build a glibc runtime that can load the native Snap7 server.
param([string]$Snap7Library)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    if (!$Snap7Library) {
        $Snap7Library = '.local-validation/vendor/snap7/build/bin/linux/libsnap7.so'
    }
    $library = (Resolve-Path -LiteralPath $Snap7Library).Path
    $header = [IO.File]::ReadAllBytes($library)
    if ($header.Length -lt 64 -or [BitConverter]::ToString($header,0,6) -ne '7F-45-4C-46-02-01' -or
        [BitConverter]::ToUInt16($header,18) -ne 62 -or [BitConverter]::ToUInt16($header,16) -ne 3) {
        throw 'Snap7 must be an x86-64 Linux shared library'
    }
    $env:CARGO_HOME = Join-Path $root '.toolchain\cargo'
    $env:RUSTUP_HOME = Join-Path $root '.toolchain\rustup'
    $env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
    & "$env:CARGO_HOME\bin\rustup.exe" target add x86_64-unknown-linux-gnu
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    if (!(Test-Path '.toolchain\zigpy\ziglang\zig.exe')) {
        python -m pip install --target .toolchain/zigpy ziglang==0.14.1
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    }
    $linker = Join-Path $root '.toolchain\linux-gcc.cmd'
    @('@echo off','"%~dp0zigpy\ziglang\zig.exe" cc -target x86_64-linux-gnu.2.28 %*') | Set-Content $linker -Encoding ascii
    & "$env:CARGO_HOME\bin\cargo.exe" rustc --locked --release --target x86_64-unknown-linux-gnu --bin station-runtime -j 2 '--' -C "linker=$linker" -C linker-flavor=gcc
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    $destination = 'build/linux-x86_64'
    New-Item -ItemType Directory -Force $destination | Out-Null
    Copy-Item -LiteralPath $library -Destination "$destination/libsnap7.so" -Force
    Copy-Item -LiteralPath 'target/x86_64-unknown-linux-gnu/release/station-runtime' -Destination "$destination/wplc" -Force
} finally {
    Pop-Location
}
