# Snap7 loopback adapter

The `serve` command connects the real Snap7 native server to the validated
[S7 DB bridge](S7.md). It uses resourceless read/write callbacks; native workers
never hold pointers to Wasm memory. The current integration is a local test
endpoint, before live WinCC commissioning.

## Native dependency

Supply an explicit path to a trusted Snap7 shared library matching the process
architecture: `snap7.dll` on Windows or `libsnap7.so` on Linux. It is loaded only
by `serve`; offline commands do not need it. Missing files, wrong architecture,
missing API exports and initialization errors fail startup. The executable can
continue loading different project packages and maps without recompilation.

Obtain the library from the
[official Snap7 repository](https://github.com/davenardella/snap7). Windows x64
binaries are provided under `build/bin/win64`; Linux build instructions are in
the upstream README. Keep third-party downloads outside tracked paths. No native
library is distributed by this repository. Snap7 is LGPL-3.0; preserve its license
and meet its distribution requirements if bundling it with a deployment.

Tested upstream revision: `30f37da3114024a71ba93f7fd855c680b97a406f` (1.4.3).
Tested Windows x64 DLL SHA-256:
`a9d8c3395056c40006dfae0056b90baf4f3a2b51ac642f28d1d1117e9ba426a2`.
Rust compilation for Linux is checked separately; native Linux execution remains
to be validated on a Linux host.

## Running

```text
station-runtime serve PROGRAM.pkg MAP.json SNAP7_LIBRARY [PORT] [PERIOD_MS] [CYCLES]
```

Defaults: loopback `127.0.0.1:1102`, 100 ms, unlimited cycles (`0`). Ports below
1024 are rejected. No LAN bind option or port-102 listener is enabled in this
milestone. The adapter first checks that the port is available: Snap7's Windows
socket reuse otherwise permits duplicate listeners. Because the native API
cannot accept a pre-bound socket, a cross-process bind race remains possible;
do not launch competing instances for the same endpoint.

The first scan completes before the listener opens, so startup initialization
cannot reset newly accepted client writes. Subsequent cycles use the same paced
loop as monitoring: zero-filled ABI inputs, no catch-up bursts, no hard real-time
guarantee. Programs with their own time constants retain those constants.
`CYCLES` includes the startup scan. A finite run stops native workers before
releasing callback context and the native library. No bridge lock is held while
stopping the native server.

## Data exchange contract

- Supports DB reads/writes in bit, byte/character, word/integer and double-word/
  real transport sizes. Strings travel as ordinary S7 bytes, including their
  capacity and length headers. Bit requests address bits; other requests address
  bytes. Snap7 performs bit extraction after the read callback.
- Only mapped DBs are available. Unknown DBs, invalid ranges, I/Q/M areas, counters,
  timers, forbidden writes and invalid values fail the S7 item. A nonzero callback
  result becomes Snap7's generic item-unavailable error; it does not communicate
  a detailed Rust validation reason to the client.
- Reads copy the last published image under the bridge mutex. Writes use its
  validated pending image; a successful S7 response acknowledges admission, not
  completion of a scan or application-command execution. A read immediately
  after a write may still see the previous generation.
- Requests from different clients are serialized through the bridge; the scan
  owns its VM mutations. No socket send/receive happens while holding the Rust
  bridge mutex. A callback can wait for an in-progress scan.
- Each callback read is coherent. Multi-item and multi-PDU reads may span scan
  generations, and multi-item writes are not all-or-nothing transactions. Earlier
  accepted items are retained if a later item fails. Large transfers depend on
  native client/server PDU splitting; they are not one atomic snapshot/write.
- Faulted bridge reads fail instead of reporting old values as fresh. The CLI
  exits on a scan fault and shuts down its listener. The last local snapshot
  remains available only to code that still owns the bridge.

The server is limited to eight clients and a 480-byte negotiated PDU. Native
event and log queues are disabled; counters on stdout track successful callback
reads, accepted writes and rejected callbacks. Ten-second JSON reports include
cycle timing and native connection/status information. Keep project-specific
logs and traffic captures local.

## Scope of compatibility

This is DB data access, not a complete S7-416 CPU emulator. Snap7 handles ISO-on-
TCP negotiation and its built-in system requests. CPU identity, SZL data, clock
and START/STOP responses are native Snap7 behavior, not a model of the Wasm PLC
lifecycle. Do not use those commands to control the Wasm runtime. No DB resources
are registered with Snap7, so block directory/info services do not describe the
generated map even though direct DB reads work. Upload/download and device I/O
are not implemented by this adapter. These limitations require checking the
actual WinCC driver before commissioning.

The upstream [resourceless-server documentation](https://snap7.sourceforge.net/res_servers.html)
describes callback mode and the distinction between data access and native
system/block-info services.

## Validation

Set `SNAP7_TEST_LIBRARY` to the absolute library path and run `cargo test --locked`.
Without it, native integration tests announce that they were skipped; pure-Rust
checks still run. Native tests use temporary loopback ports, a separate Snap7
client object, controlled generic Wasm state and no project sources. They cover
bit positions, byte order, signed values, REAL/STRING, deferred publication,
read-only and malformed writes, bounds, fragmented reads, per-item results,
reconnects, concurrent clients, bind conflicts, faults and connected shutdown.

Station-specific validation uses a separate local client process and ignored
packages, maps, scenarios and reports. None of these may be committed. Passing
these tests does not establish live WinCC compatibility. Stop before connecting
the installed WinCC OA project until the user explicitly directs that step.
