use anyhow::{Result, bail, ensure};
use station_runtime::{Manifest, Package, Runtime};
use std::{env, fs, io::Read, path::Path};

fn bounded_read(path: impl AsRef<Path>) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(32 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 32 * 1024 * 1024, "file exceeds size limit");
    Ok(bytes)
}

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    match args.get(1).map(String::as_str) {
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
            "Usage: station-runtime pack MANIFEST.json MODULE.wat OUTPUT.pkg\n       station-runtime run PROGRAM.pkg [CYCLES]\nOffline prototype: no Modbus/Snap7 drivers or full-station compiler yet."
        ),
    }
    Ok(())
}
