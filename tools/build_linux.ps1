# Build the standalone x86_64 Linux WPLC binary from Windows.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$cargoHome = Join-Path $root '.toolchain\cargo'
$rustupHome = Join-Path $root '.toolchain\rustup'
$env:CARGO_HOME = $cargoHome
$env:RUSTUP_HOME = $rustupHome
& (Join-Path $cargoHome 'bin\rustup.exe') target add x86_64-unknown-linux-musl
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$sysroot = (& (Join-Path $cargoHome 'bin\rustc.exe') --print sysroot).Trim()
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$selfContained = Join-Path $sysroot 'lib\rustlib\x86_64-unknown-linux-musl\lib\self-contained'
if (!(Test-Path (Join-Path $selfContained 'libc.a'))) { throw 'musl libc archive missing' }
# musl provides dlopen in libc; libloading still asks the linker for -ldl.
$libdl = Join-Path $selfContained 'libdl.a'
$archive = [System.Text.Encoding]::ASCII.GetBytes("!<arch>`n")
if (!(Test-Path $libdl)) { [System.IO.File]::WriteAllBytes($libdl, $archive) }
if ([System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($libdl)) -ne "!<arch>`n") {
    throw "Unexpected libdl archive: $libdl"
}
$env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER = Join-Path $sysroot 'lib\rustlib\x86_64-pc-windows-msvc\bin\rust-lld.exe'
if (!(Test-Path $env:CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER)) { throw 'rust-lld.exe missing' }
Push-Location $root
try {
    & (Join-Path $root 'cargo-local.ps1') build --release --locked --target x86_64-unknown-linux-musl -j 2
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $source = Join-Path $root 'target\x86_64-unknown-linux-musl\release\station-runtime'
    $destination = Join-Path $root 'build\linux-x86_64'
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Copy-Item -LiteralPath $source -Destination (Join-Path $destination 'wplc') -Force
    Get-FileHash (Join-Path $destination 'wplc') -Algorithm SHA256
} finally {
    Pop-Location
}
