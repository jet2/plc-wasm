# OPC UA and Modbus TCP

The Rust executable can run OPC UA, Modbus TCP master and Snap7 together over
one Wasm station image. No recompilation of the station package is required.
Protocol settings and field-to-register mappings are external JSON.

```text
station-runtime gateway PROGRAM.pkg MAP.json PROTOCOLS.json [CYCLES]
```

`CYCLES=0` (default) runs until Ctrl+C. Finite runs include the startup scan.
The station menu's launch action offers mode 3 for this command. On first use
it creates `.local-validation/protocols.json` enabling OPC UA only. Add Modbus
device mappings and, if needed, the Snap7 library and port to that file. Keep
station-specific configurations local; do not publish program-derived maps.

Example configuration with generic field names (replace with names from your
station map):

```json
{
  "period_ms": 100,
  "opcua": {
    "port": 4841,
    "pki_dir": ".local-validation/pki",
    "tags": ["Input", "Command"]
  },
  "snap7": null,
  "modbus": [{
    "name": "device1",
    "address": "127.0.0.1",
    "port": 1502,
    "unit": 1,
    "poll_ms": 100,
    "timeout_ms": 1000,
    "requests": [
      {"operation": "read_holding_registers", "address": 0, "fields": ["Input"]},
      {"operation": "write_registers", "address": 10, "fields": ["Command"]}
    ]
  }]
}
```

Set `opcua` to null to disable it. Set `snap7` to an object containing `library`
(path to `snap7.dll` or `libsnap7.so`) and `port` (for example 102) to enable S7.
Paths in protocol JSON are relative to the process working directory; the menu
launches from the repository root. Absolute paths are also accepted. Native
Snap7 still has to match the host architecture. Modbus and OPC UA use Rust
libraries and do not require a separate native protocol DLL.

## OPC UA

- Endpoint: `opc.tcp://127.0.0.1:4841/` (configurable port). This initial endpoint
  is loopback-only, anonymous, SecurityPolicy None. Certificate/private-key files
  are generated in `pki_dir`; they do not turn this endpoint into encrypted or
  authenticated access. LAN binding and secure endpoint configuration are not
  implemented in this adapter yet.
- Namespace URI: `urn:plc-wasm:tags`. Resolve its namespace index through the
  server namespace array; do not assume an index across implementations.
- Stable string NodeId: `tag/<configured name>`. Browse folders are `DB<number>`
  under Objects. Canonical field paths and unambiguous aliases are supported.
- An empty `tags` list exposes exported aliases from the S7 map, not every
  internal field. An explicit list can expose other canonical paths. Startup
  rejects unknown/ambiguous names and lists outside 1..20000 selected names.
- BOOL/BYTE/WORD/INT/DWORD/DINT/REAL become Boolean/Byte/UInt16/Int16/UInt32/Int32/
  Float. Writes must use the exact UA type. Non-finite floats are rejected.
- S7 STRING uses CP1251, matching the SCL compiler, and is exposed as Unicode UA
  String. Writes exceeding capacity or containing characters not representable
  in CP1251 are rejected. Invalid encoded data returns BadDecodingError.
- Read/write and data-change subscriptions use published PLC values. Read-only
  map fields stay read-only. Fields fed by Modbus are read-only in OPC UA even
  if the HMI map otherwise permits writes. Other writes are queued for the next
  scan; a successful write response means admission, not completed execution.
  Write admission itself emits no speculative subscription value.
- Sampling is limited by the library to at least 100 ms. Multiple nodes and
  notifications may span PLC generations. Value timestamps are sampling times,
  not hardware acquisition timestamps. No historical access is implemented.
- Reads on a faulted bridge return BadOutOfService; the gateway exits on a runtime
  fault, disconnecting clients. Modbus-fed tags return
  BadNoCommunication before the first successful poll and after a failed poll;
  reconnect restores quality. The PLC retains the last value on device failure.

## Modbus TCP master

Supported operations: `read_coils` (FC01), `read_discrete_inputs` (FC02),
`read_holding_registers` (FC03), `read_input_registers` (FC04), `write_coils`
(FC15), `write_registers` (FC16). RTU and single-write FC05/FC06 are not included.

- A request's `address` is a zero-based protocol address, not the 40001 notation.
  Device `address` is an IP address, `port` is the TCP port, and `unit` is the
  Modbus Unit ID; all three are separate settings. DNS names are not supported.
  Unit 0/broadcast is rejected. Optional `enabled: false` keeps a draft device
  inactive: it is not polled and may have an empty request list. Default is true.
- Fields are packed consecutively: coils require BOOL; BYTE/WORD/INT consume
  one register; DWORD/DINT/REAL consume two. Strings and BOOL-in-register mappings
  are rejected. A BYTE input rejects register values greater than 255.
- Bytes within each register are big-endian. Optional `swap_words: true` swaps
  the two words of each 32-bit value, independently of other fields. There is no
  scaling or engineering-unit conversion in the transport layer.
- Request sizes, address ranges, types and duplicate input targets are validated
  before any device connection is made. Requests are not automatically split.
  A device allows 1..256 requests; up to 64 devices are supported.
- Each device has its own asynchronous worker and connection. Timeout and poll
  settings are 10..60000 ms. `poll_ms` is the pause after a poll finishes; slow
  requests therefore extend the device update interval. Network waits never
  hold the PLC mutex. Failed/timed-out connections are discarded and reconnected
  on the next poll. Explicit configured outputs are sent each successful poll.
- Outputs are captured from one published PLC image at the beginning of a poll.
  All input responses from a device poll are queued as one batch only if every
  request succeeds. Input types are validated before the batch is admitted.
  Physical writes already accepted by a device cannot be rolled back if a later
  request fails. Do not use cyclic outputs for non-idempotent command semantics.
- Explicit Modbus input mappings can populate fields that are read-only to HMI;
  they do not change S7 write permissions. Input patches take priority over
  queued HMI writes to the same field at the next PLC scan.
- On failure, the last PLC value is retained; no automatic zeroing, trip logic
  or equipment-specific safe-state policy is assumed. JSON runtime metrics
  report online state, success/failure counts, last success time and last error.
  Device mappings are not inferred from SCL communication blocks.

`Station.uso` can provide the logical slave/module/channel inventory and signal
names. It does not by itself define a Modbus register map. Do not assume that a
logical slave number is a Modbus Unit ID, or that a module/channel number is a
register address. Draft devices should remain `enabled: false` until their
connection settings and explicit requests are filled in. Keep any inventory
extracted from a station file local alongside its protocol configuration.

## Validation

`cargo test --locked` includes generic codec, validation, atomic input staging,
local TCP requests, exceptions, timeouts and reconnection tests. Native Snap7
tests additionally need `SNAP7_TEST_LIBRARY` as described in SNAP7.md.

For the independent protocol integration test, use an ignored local directory:

1. Set `PLC_PROTOCOL_FIXTURE_DIR` to that directory and run the Rust `protocols`
   test to export its generic Wasm package and map (no SCL source is used).
2. Install Python `asyncua` into an ignored local virtual environment.
3. Run `tests/protocol_network.py --runtime PATH --fixture DIRECTORY --snap7 LIBRARY`.

This runs a local Modbus simulator, an independent OPC UA client, and an optional
native Snap7 client against one finite gateway process. It checks browsing,
types, writes, subscriptions, shared S7 values, device failure quality and
recovery. All sockets use loopback; it does not touch WinCC or physical devices.

Protocol libraries: [async-opcua](https://github.com/FreeOpcUa/async-opcua)
and [tokio-modbus](https://github.com/slowtec/tokio-modbus).
