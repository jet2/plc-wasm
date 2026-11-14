# Use only when Rust was installed locally during bootstrap.
$ErrorActionPreference = 'Stop'
$env:CARGO_HOME = Join-Path $PSScriptRoot '.toolchain\cargo'
$env:RUSTUP_HOME = Join-Path $PSScriptRoot '.toolchain\rustup'
$env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
& "$env:CARGO_HOME\bin\cargo.exe" @args
exit $LASTEXITCODE
