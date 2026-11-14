# Experimental station ABI v1

## Package

One `.pkg` file; integers in the container header are little-endian.

| Offset | Size | Meaning |
|---|---:|---|
| 0 | 4 | ASCII `SWP1` |
| 4 | 4 | JSON manifest byte length |
| 8 | 4 | Wasm module byte length |
| 12 | JSON length | UTF-8 manifest |
| 12 + JSON length | Wasm length | Core WebAssembly binary v1 |

Trailing bytes and truncation are errors. Package limit: 32 MiB; manifest
limit: 1 MiB. The module SHA-256 is stored in the manifest and verified.
This is corruption detection, not authentication or a digital signature.
The manifest also records a source digest for traceability.

`abi_version` must be 1. Currently accepted kinds are `initializer-smoke`
and `cycle-smoke`; neither enables real device I/O. The maximum permitted
linear memory is 16 MiB. The manifest specifies the exact initial memory
size and the host prevents growth beyond it. Fuel must be 1..100000000.

The runtime accepts no imports, WASI, tables or additional memories.
Instantiation/start execution is fuel-limited as well. The compiler emits
no start function. Exports:

```text
memory        : linear memory
abi_version() : i32             // returns 1
init()        : i32             // 0 = success
scan(i32)     : i32             // elapsed milliseconds; 0 = success
```

The host resolves typed exports and validates memory before calling init.
Each init/scan receives a fresh fuel budget. Fuel is an engine-specific
instruction budget, not a portable measure of elapsed time or a hard
real-time guarantee.

## State and publication

Inputs and outputs are disjoint `(offset, size)` regions. Empty regions
are allowed. The host copies a complete input snapshot before calling
scan. On success it copies the output region into host-owned memory.
Internal Wasm state persists between cycles.

If scan returns nonzero or traps, the host latches a fault and leaves the
previous successful output snapshot unchanged. It does not retry against
partially modified VM state. Reload is required. The previous snapshot is
available for diagnostics; future device adapters must consult fault status
and implement a separately specified output policy, not blindly transmit it.

`init` failure rejects the module entirely. No external publication occurs.
The offline CLI supplies zero-valued inputs and deterministic 100 ms deltas.
It does not implement pacing, deadlines, watchdog hardware or reconnects.

## Initializer compiler layout

Every field of `TMPR_Params` occupies one 4-byte little-endian internal slot.
INT values are sign-extended to i32, BOOL is 0/1, other accepted integer
types use their numeric bit representation. The complete structure is
initialized from UDT defaults, then `parInit` assignments run in source order.
DB overrides, implicit cross-type field conversion and all control flow are
rejected by this milestone's compiler. Boolean numeric literals 0 and 1 are
accepted to match the existing initialization source.

`fields` describes the names/types/offsets for test output. It is explicitly
**not S7 DB layout**. No Snap7 server may expose this buffer as DB2 without
an additional generated S7 codec. Wasm memory addresses must never be held
by an independent native network worker; adapters use owned snapshots and
queued commands.

## Cyclic integer compiler (second milestone)

`compile_program.py` emits the same ABI v1 and `cycle-smoke` kind. The native
runtime is unchanged. `init` applies compiled UDT defaults, calls the requested
initialization functions in order, then applies explicit offline test presets.
`scan` calls the requested entry function. Called functions are linked into the
same module; recursive call graphs and unresolved externals are rejected.

Structures are concatenations of internal fields; integer/BOOL fields occupy
four bytes, arrays use row-major element strides and declared lower bounds.
REAL storage and empty STRING defaults may be reserved inside structures;
computation on these types is rejected. This internal layout still has no S7
packing, physical address aliasing, device I/O or externally writable inputs.

Supported control flow: IF/ELSIF/ELSE, CASE with integer labels/ranges,
FOR with a nonzero constant BY, WHILE, RETURN, EXIT. FOR captures the final
bound at entry and traps if advancing its control variable would overflow.
Dynamic indexing checks the declared bounds, not just the Wasm memory size.
Scalar VAR_TEMP values are local to each invocation and carry validity flags;
reading one before assignment on the executed path traps. This is deliberate
diagnostic behavior, not an emulation of uninitialized S7 stack contents.

Integer expressions promote INT to DINT when required. INT operations and
integer assignments normalize to the destination width; DINT uses i32 wrapping
arithmetic. Division by zero traps. S7 status flags/ENO and its complete error
behavior are not implemented; overflow behavior still requires PLC comparison.
Boolean operations evaluate both operands. Mixed signed/bit-string operations
require explicit conversions. Implemented conversion functions are INT_TO_DINT,
DINT_TO_INT and DWORD_TO_DINT. Literal 0/1 can be assigned/compared to BOOL.

The generated code is an experimental integer execution model, not a verified
replacement for STEP 7. Compiler diagnostics identify original source locations;
runtime traps do not yet carry SCL source maps. The language reference for
operator precedence and integer division is Siemens
[S7-SCL V5.3 manual, chapters 11 and 12](https://cache.industry.siemens.com/dl/files/793/5581793/att_66783/v1/SCL_e.pdf).

## Remaining runtime integration

The compiler will eventually produce the full cyclic station program and a
verified external S7 memory layout. Modbus remains a master/client owned by
Rust, and Snap7 serves S7 reads/writes independently. Network delays must not
block the logical cycle; commands are accepted at defined cycle boundaries.
Device addresses, stale-input handling, command acknowledgements, fault
output policy and persistent-state migration remain to be specified.

The implemented engine APIs are documented by the Wasmi project:
[fuel configuration](https://docs.rs/wasmi/2.0.0/wasmi/struct.Config.html),
[store limits](https://docs.rs/wasmi/2.0.0/wasmi/struct.StoreLimitsBuilder.html).
