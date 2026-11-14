# Portable station runtime

Rust runtime and experimental compiler for a supported subset of Siemens SCL.
Programs are delivered as portable WebAssembly packages. The Windows and Linux
runtime sources share the same package ABI; native executables are built per OS.

## Publication boundary

This repository contains compiler/runtime implementation and generic runtime tests.
SCL source files, copied fragments, synthetic SCL examples, station reports,
source-based test fixtures, generated station programs and archives are local only.
They must never be committed, pushed, attached to issues or pasted into PRs.

Local validation lives in ignored `.local-validation/`; builds live in `build/`.
The protected-source directory list lives in ignored `.publish-sources.json`,
a nonempty JSON array of absolute local source-directory paths. Every configured
directory must be accessible and contain sources. Neither the paths nor generated
fingerprints need to be published.

## Mandatory checks before publication

```text
git config core.hooksPath .githooks
python tools/check_publish.py --index
python tools/check_publish.py --revision HEAD --history
```

The pre-push hook checks the exact outgoing commit histories. The guard applies
an explicit public-path allowlist, checks text and decoded literals for embedded
source declarations, rejects binary/encoded payloads, and compares normalized
fragments against protected local files. Missing configuration, unavailable sources
or inspection errors block the push. Never bypass the hook.

Review remains mandatory: static scanning cannot prove absence of arbitrarily
obfuscated fragments. The CI structural-only check does not have protected sources
and must not be used as permission to push.

## Build and generic tests

```text
cargo build --locked
cargo test --locked
python -m unittest discover -s tests -p "test_*.py" -v
```

A local Windows Rust installation can be used through `cargo-local.ps1`.
Linux compilation was checked; executing the runtime on Linux remains pending.
The CI template is in `ci/github-actions.yml`. It is not active because the
current publishing token lacks the GitHub workflow permission needed to install it.

## Current implementation

- Located lexer/parser, case-insensitive symbols and typed expressions.
- Integer and boolean operations, structures and bounded multidimensional arrays.
- REAL arithmetic, bounded STRING operations and numeric local AT overlays.
- Conditional branches, case selection, loops, returns and linked VOID functions.
- Package validation, execution fuel, memory limits and persistent cycle state.
- Traps reject partial output publication and latch the runtime fault state.

Local tests cover generated cyclic programs, initialization, state retention,
array bounds and runtime faults. These checks are not a proof of equivalence
with STEP 7 or PLCSIM. Validation details and source-related reports stay local.

Full reachable cyclic programs can now be compiled for offline execution with
the explicitly selected `cyclic-sim` system profile. This profile records one
supported interrupt-class disable request; it does not emulate CPU interrupt
delivery or diagnostics. The default `strict` profile rejects that system call.
`--fuel-per-call` configures the package execution budget within the runtime's
existing limit. It is an instruction budget, not a real-time deadline.

Remaining work includes S7 memory serialization, Modbus master, Snap7 server,
scheduling/device fault policies and comparison against the reference PLC.
S7 status flags, general library/system functions and whole aggregate assignment
remain unsupported. String overflow and non-finite REAL results trap deliberately.
The internal memory layout is not the S7 DB layout. See `ABI.md` for the
experimental interface and limitations.

The public command-line tools read externally supplied project directories.
Use their `--help` output and direct all generated output to ignored `build/`.
Do not copy projects into tracked paths to make tests or CI self-contained.
