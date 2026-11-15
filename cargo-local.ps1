# Use only when Rust was installed locally during bootstrap.
$ErrorActionPreference = 'Stop'
$env:CARGO_HOME = Join-Path $PSScriptRoot '.toolchain\cargo'
$env:RUSTUP_HOME = Join-Path $PSScriptRoot '.toolchain\rustup'
$env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
$cache = Get-ChildItem (Join-Path $PSScriptRoot '.toolchain\sccache') -Filter sccache.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cache) {
    $source = Join-Path $PSScriptRoot 'tools/rust_cache_wrapper.rs'
    $wrapper = Join-Path $PSScriptRoot '.toolchain/rust-cache-wrapper.exe'
    if (!(Test-Path $wrapper) -or (Get-Item $source).LastWriteTimeUtc -gt (Get-Item $wrapper).LastWriteTimeUtc) {
        & "$env:CARGO_HOME\bin\rustc.exe" --edition=2024 -O $source -o $wrapper
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    $env:WPLC_SCCACHE = $cache.FullName
    $env:RUSTC_WRAPPER = $wrapper
    # Keep incremental workspace builds; cache eligible dependencies and release crates.
    $env:SCCACHE_ALLOW_INCREMENTAL = '1'
}
& "$env:CARGO_HOME\bin\cargo.exe" @args
exit $LASTEXITCODE
