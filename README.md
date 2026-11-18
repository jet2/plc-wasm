# WPLC

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
- S7 DB serialization, named aliases and queued writes at scan boundaries.
- Snap7 loopback server with validated read/write callbacks and native client tests.

Local tests cover generated cyclic programs, initialization, state retention,
array bounds and runtime faults. These checks are not a proof of equivalence
with STEP 7 or PLCSIM. Validation details and source-related reports stay local.

Full reachable cyclic programs can now be compiled for offline execution with
the explicitly selected `cyclic-sim` system profile. This profile records one
supported interrupt-class disable request; it does not emulate CPU interrupt
delivery or diagnostics. The default `strict` profile rejects that system call.
`--fuel-per-call` configures the package execution budget within the runtime's
existing limit. It is an instruction budget, not a real-time deadline.

The offline S7 adapter generates a separate DB map, checks exported read/write
addresses and translates snapshots and queued writes. See [S7.md](S7.md) for
generation, limits, access policy and the `s7-run` replay command.

The `serve` command exposes mapped DBs on a separate loopback test port through
a dynamically loaded native Snap7 library. See [SNAP7.md](SNAP7.md) for setup,
the supported data access contract and CPU/system-service limitations.

OPC UA and Modbus TCP master can run concurrently with Snap7 using `gateway`.
See [PROTOCOLS.md](PROTOCOLS.md) for JSON settings, mappings, access policy and tests.

Remaining work includes extended live WinCC commissioning,
scheduling/device fault policies and comparison against the reference PLC.
S7 status flags, general library/system functions and whole aggregate assignment
remain unsupported. String overflow and non-finite REAL results trap deliberately.
The internal memory layout is not the S7 DB layout. See `ABI.md` for the
experimental interface and limitations.

The public command-line tools read externally supplied project directories.
Use their `--help` output and direct all generated output to ignored `build/`.
Do not copy projects into tracked paths to make tests or CI self-contained.

## Station menu

Run `python tools/station_menu.py` for the Russian-language interactive menu:
recursive package discovery, station rebuild and visible wplc launch. The default
search folder is stored as `stations_folder` in `tools/menu-settings.json`,
next to the menu script. On first launch the menu creates it with the absolute path to
`build/`. Relative paths in this file are resolved against the repository root.
Menu item 5 saves the selected folder for subsequent launches. `--root PATH`
overrides it for one run without changing the file. At the user's request,
`tools/menu-settings.json` is tracked with the tools, including machine-specific
paths and station catalog metadata. Review changes before publishing; source
contents and compiled station artifacts remain prohibited. Python caches are ignored.

Menu item 6 recursively finds source projects: a `Program` directory must contain
nonempty `Source` (or `Soure`) and `Tables` directories. It replaces `source_stations`
in `tools/menu-settings.json`, then reloads that file.
Item 7 selects a saved source project for compilation, including projects that
have never been built. It asks for the cyclic entry function, initialization
functions, compiler profile and fuel budget; known local build reports supply
defaults when available. Builds go to `wplc-build/<timestamp>/` beside the station's
`.mpr` file and generate an S7 map. The menu finds the nearest ancestor of the
source `Program` directory containing an `.mpr` file (case-insensitive extension).
If none is found, compilation fails before writing output. Successful builds
are registered immediately. Generated station artifacts remain local and must
not be published.

The menu groups actions into compiled stations, source stations and controls,
and shows the saved counts. Items 2, 3 and 7 display the corresponding list
directly from settings; opening these subsections never triggers a search.
Settings are reloaded each time. Missing packages remain visible as unavailable;
the selected package is checked before launch or rebuild. An empty catalog
prompts the user to run an explicit search.

Item 1 refreshes and saves the package catalog. Recursive search checks files
directly in `stations_folder` and all its subfolders. The menu rereads this path
before searching, so edits to settings take effect without restarting.
Each successful search replaces its corresponding list completely, including
clearing it when nothing is found. Item 1 replaces only `compiled_stations`;
item 6 replaces only `source_stations`. Previous entries outside the search tree
are not carried over. Directory traversal or file-read errors abort the search
and leave the settings file unchanged. Invalid package formats are skipped.
The completed result is saved atomically. Successful compilation separately
registers its new package without searching or clearing other entries.
Items 2 and 3
select saved packages for rebuilding and launching. An explicit search removes
missing or malformed files from the catalog. Source projects are marked as compiled only when a known local
source-to-package association exists. This indicates an available build, not
that it is current with the source files. The folder discovery rule alone does
not guarantee that a project has valid SCL or all tables required by compilation.

The menu starts WPLC in a separate console on Windows and a separate terminal
session on Linux. WEmulator starts as an independent GUI process with its output
written under `build/live/logs/`. Closing the menu leaves both processes running.
On Linux without a terminal, WPLC also runs independently and writes a log in
that directory; menu item 4 stops it.

`python tools/station_menu.py --list` shows the saved package catalog without
searching or entering the menu.
Discovery checks package framing and reads the embedded manifest; the runtime
performs full package validation at launch.

Rebuild requires the original `build.json` next to the selected package and the
external project folder containing `Source` and `Tables`. It preserves entry,
initialization order, presets, watches, system profile and fuel budget. Source
paths and runtime/library preferences are saved only in ignored
`.local-validation/station-menu.json`. Rebuilds use the same station-local
`wplc-build/<timestamp>/` location beside `.mpr`; existing packages and running stations are
not replaced. A failed build may leave diagnostic artifacts in its new folder.
Hand-built packages without a compiler report cannot be rebuilt by this menu.

S7/OPC UA maps include read-only route aliases from `RouteTags.asc` and control
heartbeat aliases from the exported `*_OPC_Config.csv`. Every exported address
is checked against the compiled DB layout. Old packages need recompilation for
WEmulator's route controls and standalone operator heartbeat; PLC interlocks
and route logic remain in the original compiled program.

Launch requires an adjacent `s7-map.json`, and offers Snap7 (localhost, default
port 102) or offline monitoring. An occupied port rejects the launch; the menu
does not stop existing processes. Each launch gets its own executable copy under
`build/live/menu/`. Windows opens a new console; Linux uses an available terminal
emulator, falling back to an independent process with a log under `build/live/logs/`.
New menu compilations use station-local `wplc-build/current` and `wplc-build/previous`.
Compilation finishes in a staging directory before the current build is promoted;
on failure the current build stays available. The menu groups saved builds by source
station and always launches `current` when present. Older timestamped directories
remain untouched during the transition, but are not offered as launch choices.
The console shows one status table that refreshes every second: scan timing,
memory, S7 clients and reads/writes, OPC UA, and each Modbus device. Redirected
output receives a plain table every ten seconds. Stop wplc with Ctrl+C in its
window or item 4 in the menu. Runtime errors appear there; requesting a window
is not proof of a successful server startup. Existing WinCC settings are not modified.

On Windows, `powershell -File tools/build_linux.ps1` builds a dynamically linked
x86-64 Linux runtime and packages `wplc` and `libsnap7.so` in `build/linux-x86_64`.
Build the Snap7 library with `make` in the vendor's `build/linux` directory on
Linux, or pass its path to the script with `-Snap7Library`. The runtime uses
glibc and the library built with g++ requires the Linux C++ runtime.
In the station's `protocols.json`, set `"snap7": {"library":"./libsnap7.so","port":102}`.
Run `./start.sh` from that directory. It starts `./wplc` without arguments.

Menu item 4 lists running `wplc` instances whose executables are under this
repository's `build/live/`, including instances launched before opening the menu.
Select an instance by its PID and command line, or enter 0 to cancel. Other
applications and copies outside that directory are excluded. The menu checks
executable path and process creation identity again before stopping, using a
stable process handle to avoid terminating a reused PID. Windows uses targeted
process termination; Linux uses SIGTERM via pidfd (Python 3.9+ and a kernel with
pidfd support). This stops execution and releases network sockets; it does not
save PLC memory or execute a PLC shutdown scan. No other instance is stopped.

Menu item 8 deletes multiple saved builds. Select numbers such as `1,3-5`,
`все` / `all`, or `0` to cancel. The menu shows the exact directories and requires
typing `УДАЛИТЬ` before deleting anything. Only separate timestamped build folders
under station-local `wplc-build/` beside `.mpr`, or historical repository
`build/compiled/` and `build/recompiled/`, are eligible. Folders with unexpected
files, subdirectories, symlinks or Windows reparse points are rejected. Older
fixed-location builds are excluded. All selected folders are checked before
deletion; builds referenced by discovered running wplc instances are rejected.
Do not launch a selected build concurrently with deletion.

Deletion removes the known build artifacts and their now-empty folders; source
projects and `.mpr` files are not deleted. Completed removals are removed from
both local catalogs and source associations. If a later deletion fails, already
completed removals remain deleted and are reported; the batch is not transactional.

### Выбор программы в WPLC

После release-сборки запустите `target/release/wplc.exe` в Windows или `target/release/wplc` в Linux без аргументов. При первом запуске рядом с исполняемым файлом создаётся `wplc.json`. В консоли нажмите **1** и Enter, выберите файл `.pkg` по номеру либо введите путь. WPLC проверяет программу и соседний `s7-map.json`, затем сохраняет выбор. **2** запускает станцию, **3** и Enter останавливают её и возвращают меню для смены программы. Панель показывает RUN и номер цикла после запуска. В режиме gateway настройки протоколов по умолчанию берутся из соседнего с пакетом `protocols.json`. Пункт запуска в меню станций подготавливает настройки экземпляра с автоматическим запуском.
