use anyhow::{Result, bail, ensure};
use serde::Deserialize;
use station_runtime::s7::{Bridge, Map};
use station_runtime::{Manifest, Package, Runtime};
use std::{env, fs, io::Read, path::Path};

fn bounded_read(path: impl AsRef<Path>) -> Result<Vec<u8>> {
    bounded_read_limit(path, 32 * 1024 * 1024)
}

fn bounded_read_limit(path: impl AsRef<Path>, limit: usize) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= limit, "file exceeds size limit");
    Ok(bytes)
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum S7Action {
    Read {
        db: u16,
        offset: usize,
        length: usize,
    },
    ReadNamed {
        name: String,
    },
    Write {
        db: u16,
        offset: usize,
        data: Vec<u8>,
    },
    WriteNamed {
        name: String,
        data: Vec<u8>,
    },
    WriteBit {
        db: u16,
        byte: usize,
        bit: u8,
        value: bool,
    },
    Cycle {
        elapsed_ms: u32,
    },
}

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("s7-run") if args.len() == 5 => {
            let runtime = Runtime::load(Package::decode(&bounded_read(&args[2])?)?)?;
            let inputs = vec![0; runtime.manifest.inputs.size as usize];
            let map: Map =
                serde_json::from_slice(&bounded_read_limit(&args[3], 64 * 1024 * 1024)?)?;
            let actions: Vec<S7Action> = serde_json::from_slice(&bounded_read(&args[4])?)?;
            ensure!(actions.len() <= 10000, "too many actions");
            let mut bridge = Bridge::new(runtime, map)?;
            let mut reads = Vec::new();
            for action in actions {
                match action {
                    S7Action::Read { db, offset, length } => reads.push(serde_json::json!({"generation":bridge.generation(), "data":bridge.read_db(db, offset, length)?})),
                    S7Action::ReadNamed { name } => reads.push(serde_json::json!({"generation":bridge.generation(), "data":bridge.read_named(&name)?})),
                    S7Action::Write { db, offset, data } => bridge.queue_bytes(db, offset, &data)?,
                    S7Action::WriteNamed { name, data } => bridge.queue_named(&name, &data)?,
                    S7Action::WriteBit { db, byte, bit, value } => bridge.queue_bit(db, byte, bit, value)?,
                    S7Action::Cycle { elapsed_ms } => bridge.cycle(&inputs, elapsed_ms)?,
                }
            }
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"mode":"offline-s7", "generation":bridge.generation(), "blocks":bridge.map().blocks.len(), "reads":reads})
                )?
            );
        }
        Some("pack") if args.len() == 5 => {
            let manifest: Manifest = serde_json::from_slice(&bounded_read(&args[2])?)?;
            let wasm = wat::parse_bytes(&bounded_read(&args[3])?)?.into_owned();
            let bytes = Package::encode(manifest, wasm)?;
            // Verify ABI, limits and initialization before writing an executable package.
            Runtime::load(Package::decode(&bytes)?)?;
            fs::write(&args[4], &bytes)?;
            println!("Packed {} bytes: {}", bytes.len(), args[4]);
        }
        Some("run") if (3..=4).contains(&args.len()) => {
            let cycles: u32 = args.get(3).map(|s| s.parse()).transpose()?.unwrap_or(1);
            ensure!((1..=10000).contains(&cycles), "cycles must be 1..10000");
            let package = Package::decode(&bounded_read(&args[2])?)?;
            let mut runtime = Runtime::load(package)?;
            let inputs = vec![0; runtime.manifest.inputs.size as usize];
            // Deterministic offline exercise, NOT a wall-clock/device scheduler.
            for _ in 0..cycles {
                runtime.cycle(&inputs, 100)?;
            }
            let mut values = serde_json::Map::new();
            for field in &runtime.manifest.fields {
                let offset = (field.offset - runtime.manifest.outputs.offset) as usize;
                let raw = u32::from_le_bytes(runtime.outputs()[offset..offset + 4].try_into()?);
                let value = match field.scl_type.as_str() {
                    "BOOL" => serde_json::Value::Bool(raw != 0),
                    "INT" | "DINT" => serde_json::json!(raw as i32),
                    _ => serde_json::json!(raw),
                };
                values.insert(field.name.clone(), value);
            }
            println!(
                "{}",
                serde_json::to_string_pretty(&serde_json::json!({
                    "name": runtime.manifest.name, "kind": runtime.manifest.kind,
                    "cycles": cycles, "mode": "offline-smoke", "values": values,
                }))?
            );
        }
        _ => bail!(
            "Usage: station-runtime pack MANIFEST.json MODULE.wat OUTPUT.pkg\n       station-runtime run PROGRAM.pkg [CYCLES]\n       station-runtime s7-run PROGRAM.pkg MAP.json ACTIONS.json\nOffline execution: no Modbus/Snap7 network drivers."
        ),
    }
    Ok(())
}
