use anyhow::{Result, bail, ensure};
use serde::Deserialize;
use station_runtime::s7::{Bridge, Map};
use station_runtime::snap7::Server;
use station_runtime::{Manifest, Package, Runtime};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
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
        Some("serve") if (5..=8).contains(&args.len()) => {
            let port: u16 = args.get(5).map(|s| s.parse()).transpose()?.unwrap_or(1102);
            let period_ms: u32 = args.get(6).map(|s| s.parse()).transpose()?.unwrap_or(100);
            let limit: u64 = args.get(7).map(|s| s.parse()).transpose()?.unwrap_or(0);
            ensure!(
                (1..=60000).contains(&period_ms),
                "period must be 1..60000 ms"
            );
            let runtime = Runtime::load(Package::decode(&bounded_read(&args[2])?)?)?;
            let inputs = vec![0; runtime.manifest.inputs.size as usize];
            let map: Map =
                serde_json::from_slice(&bounded_read_limit(&args[3], 64 * 1024 * 1024)?)?;
            let mut bridge = Bridge::new(runtime, map)?;
            // Complete startup before accepting client writes that initialization could reset.
            bridge.cycle(&inputs, period_ms)?;
            let bridge = Arc::new(Mutex::new(bridge));
            let server = Server::start(Path::new(&args[4]), port, bridge.clone())?;
            println!(
                "{}",
                serde_json::json!({"event":"listening", "pid":std::process::id(),
                "address":"127.0.0.1", "port":port, "period_ms":period_ms, "generation":1})
            );
            let period = Duration::from_millis(period_ms as u64);
            let mut deadline = Instant::now() + period;
            let mut reported = Instant::now();
            let (mut count, mut total_ms, mut maximum_ms, mut overruns) =
                (0u64, 0.0f64, 0.0f64, 0u64);
            loop {
                if limit != 0
                    && bridge
                        .lock()
                        .map_err(|_| anyhow::anyhow!("bridge poisoned"))?
                        .generation()
                        >= limit
                {
                    break;
                }
                std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
                let start = Instant::now();
                let generation = {
                    let mut guard = bridge
                        .lock()
                        .map_err(|_| anyhow::anyhow!("bridge poisoned"))?;
                    guard.cycle(&inputs, period_ms)?;
                    guard.generation()
                };
                let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
                count += 1;
                total_ms += elapsed_ms;
                maximum_ms = maximum_ms.max(elapsed_ms);
                deadline += period;
                if Instant::now() > deadline {
                    overruns += 1;
                    deadline = Instant::now();
                }
                if reported.elapsed() >= Duration::from_secs(10)
                    || limit != 0 && generation >= limit
                {
                    println!(
                        "{}",
                        serde_json::json!({"event":"network_cycles", "generation":generation,
                        "mean_cycle_ms":total_ms/count as f64, "max_cycle_ms":maximum_ms,
                        "missed_deadlines":overruns, "snap7":server.status()?})
                    );
                    reported = Instant::now();
                    count = 0;
                    total_ms = 0.0;
                    maximum_ms = 0.0;
                    overruns = 0;
                }
            }
            // Native worker threads stop before the shared bridge is released.
            drop(server);
        }
        Some("monitor") if (4..=6).contains(&args.len()) => {
            let period_ms: u32 = args.get(4).map(|s| s.parse()).transpose()?.unwrap_or(100);
            let limit: u64 = args.get(5).map(|s| s.parse()).transpose()?.unwrap_or(0);
            ensure!(
                (1..=60000).contains(&period_ms),
                "period must be 1..60000 ms"
            );
            let started = Instant::now();
            let runtime = Runtime::load(Package::decode(&bounded_read(&args[2])?)?)?;
            let inputs = vec![0; runtime.manifest.inputs.size as usize];
            let memory_bytes = runtime.manifest.memory_bytes;
            let state_bytes = runtime.manifest.outputs.size;
            let map: Map =
                serde_json::from_slice(&bounded_read_limit(&args[3], 64 * 1024 * 1024)?)?;
            let mut bridge = Bridge::new(runtime, map)?;
            println!(
                "{}",
                serde_json::json!({"event":"ready", "pid":std::process::id(),
                "mode":"offline-monitor", "period_ms":period_ms, "load_ms":started.elapsed().as_secs_f64()*1000.0,
                "wasm_memory_bytes":memory_bytes, "state_bytes":state_bytes, "blocks":bridge.map().blocks.len(),
                "fields":bridge.map().blocks.iter().map(|b| b.fields.len()).sum::<usize>()})
            );
            let period = Duration::from_millis(period_ms as u64);
            let mut deadline = Instant::now();
            let mut reported = Instant::now();
            let mut count = 0u64;
            let mut total_ms = 0.0f64;
            let mut maximum_ms = 0.0f64;
            let mut overruns = 0u64;
            loop {
                std::thread::sleep(deadline.saturating_duration_since(Instant::now()));
                let cycle_start = Instant::now();
                bridge.cycle(&inputs, period_ms)?;
                let elapsed = cycle_start.elapsed();
                count += 1;
                total_ms += elapsed.as_secs_f64() * 1000.0;
                maximum_ms = maximum_ms.max(elapsed.as_secs_f64() * 1000.0);
                deadline += period;
                if Instant::now() > deadline {
                    overruns += 1;
                    // Skip missed deadlines rather than execute a burst of catch-up scans.
                    deadline = Instant::now();
                }
                if bridge.generation() == 1
                    || reported.elapsed() >= Duration::from_secs(10)
                    || limit != 0 && bridge.generation() >= limit
                {
                    println!(
                        "{}",
                        serde_json::json!({"event":"cycles", "generation":bridge.generation(),
                        "uptime_s":started.elapsed().as_secs_f64(), "samples":count,
                        "mean_cycle_ms":total_ms/count as f64, "max_cycle_ms":maximum_ms,
                        "missed_deadlines":overruns})
                    );
                    count = 0;
                    total_ms = 0.0;
                    maximum_ms = 0.0;
                    overruns = 0;
                    reported = Instant::now();
                }
                if limit != 0 && bridge.generation() >= limit {
                    break;
                }
            }
        }
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
            "Usage: station-runtime pack MANIFEST.json MODULE.wat OUTPUT.pkg\n       station-runtime run PROGRAM.pkg [CYCLES]\n       station-runtime s7-run PROGRAM.pkg MAP.json ACTIONS.json\n       station-runtime monitor PROGRAM.pkg MAP.json [PERIOD_MS] [CYCLES]\n       station-runtime serve PROGRAM.pkg MAP.json SNAP7_LIBRARY [PORT] [PERIOD_MS] [CYCLES]\nDefaults: loopback port 1102, 100 ms, unlimited cycles (0). No Modbus driver."
        ),
    }
    Ok(())
}
