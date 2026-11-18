//! Concurrent adapters sharing one PLC image and one scan scheduler.
use crate::{
    dashboard::Dashboard, dashboard::bytes, dashboard::resident_bytes, modbus, s7::Bridge, snap7,
    ua,
};
use anyhow::{Result, ensure};
use serde::Deserialize;
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snap7Config {
    pub library: PathBuf,
    pub port: u16,
    #[serde(default = "localhost")]
    pub ip: std::net::Ipv4Addr,
    #[serde(default)]
    pub rack: u8,
    #[serde(default = "default_slot")]
    pub slot: u8,
}
fn localhost() -> std::net::Ipv4Addr {
    std::net::Ipv4Addr::LOCALHOST
}
fn default_slot() -> u8 {
    2
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub period_ms: u32,
    pub opcua: Option<ua::Config>,
    pub snap7: Option<Snap7Config>,
    #[serde(default)]
    pub modbus: Vec<modbus::Device>,
}

pub async fn run(
    mut bridge: Bridge,
    inputs: Vec<u8>,
    config: Config,
    cycles: u64,
    station: String,
    wasm_bytes: u32,
    state_bytes: u32,
) -> Result<()> {
    ensure!(
        (1..=60000).contains(&config.period_ms),
        "period must be 1..60000 ms"
    );
    ensure!(
        config.opcua.is_some()
            || config.snap7.is_some()
            || config.modbus.iter().any(|device| device.enabled),
        "no adapters configured"
    );
    modbus::validate(&config.modbus, &bridge)?;
    bridge.cycle(&inputs, config.period_ms)?;
    let bridge = Arc::new(Mutex::new(bridge));
    let devices: Vec<_> = config
        .modbus
        .into_iter()
        .filter(|device| device.enabled)
        .map(|device| (device, Arc::new(Mutex::new(modbus::Status::default()))))
        .collect();
    let opcua_endpoint = config
        .opcua
        .as_ref()
        .map(|settings| format!("opc.tcp://127.0.0.1:{}/", settings.port));
    let snap7_endpoint = config
        .snap7
        .as_ref()
        .map(|s| format!("{}:{}  rack {}  slot {}", s.ip, s.port, s.rack, s.slot));
    let ua = if let Some(settings) = config.opcua {
        let (server, handle) = ua::build(&settings, bridge.clone(), &devices)?;
        // Keep ownership of the bound socket; no bind-probe race.
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", settings.port)).await?;
        Some((server, handle, listener))
    } else {
        None
    };
    let snap7 = config
        .snap7
        .map(|s| {
            ensure!(s.rack < 8 && s.slot < 32, "invalid S7 rack or slot");
            snap7::Server::start_at(&s.library, s.ip, s.port, bridge.clone())
        })
        .transpose()?;
    let (stop, receiver) = tokio::sync::watch::channel(false);
    let workers: Vec<_> = devices
        .iter()
        .map(|(device, status)| {
            tokio::spawn(modbus::worker(
                device.clone(),
                bridge.clone(),
                status.clone(),
                receiver.clone(),
            ))
        })
        .collect();
    let (ua_handle, mut ua_task) = match ua {
        Some((server, handle, listener)) => {
            (Some(handle), Some(tokio::spawn(server.run_with(listener))))
        }
        None => (None, None),
    };
    let dashboard = Dashboard::new();
    let started = Instant::now();
    let draw = |generation: u64, mean: f64, maximum: f64, overruns: u64| {
        let mut plc = vec![
            ("Station".into(), station.clone()),
            (
                "PID / uptime".into(),
                format!(
                    "{} / {:.0} s",
                    std::process::id(),
                    started.elapsed().as_secs_f64()
                ),
            ),
            (
                "Scan".into(),
                format!("RUN  #{generation}  period {} ms", config.period_ms),
            ),
            (
                "Cycle mean / max".into(),
                format!("{mean:.2} / {maximum:.2} ms"),
            ),
            (
                "Scan load / misses".into(),
                format!(
                    "{:.1}% / {overruns}",
                    mean * 100.0 / config.period_ms as f64
                ),
            ),
            (
                "Wasm / PLC state".into(),
                format!(
                    "{} / {}",
                    bytes(wasm_bytes as u64),
                    bytes(state_bytes as u64)
                ),
            ),
        ];
        if let Some(rss) = resident_bytes() {
            plc.push(("Process RSS".into(), bytes(rss)));
        }
        let mut links = Vec::new();
        links.push((
            "OPC UA".into(),
            opcua_endpoint
                .as_ref()
                .map_or("off".into(), |url| format!("LISTEN {url}")),
        ));
        if let Some(server) = &snap7 {
            match server.status() {
                Ok(s) => {
                    links.push((
                        "S7 listener".into(),
                        snap7_endpoint.clone().unwrap_or_default(),
                    ));
                    links.push((
                        "S7 clients / reads".into(),
                        format!("{} / {}", s.clients, s.reads),
                    ));
                    links.push((
                        "S7 writes / denied".into(),
                        format!("{} / {}", s.writes, s.rejected),
                    ));
                }
                Err(error) => links.push(("S7".into(), format!("ERROR {error}"))),
            }
        } else {
            links.push(("S7".into(), "off".into()));
        }
        let online = devices
            .iter()
            .filter(|(_, status)| status.lock().unwrap().online)
            .count();
        let mut modbus = vec![(
            "Devices online".into(),
            format!("{online}/{}", devices.len()),
        )];
        for (device, status) in &devices {
            let s = status.lock().unwrap();
            let detail = if s.online {
                format!(
                    "ON {} U{} ok{} err{}",
                    device.endpoint(),
                    device.unit,
                    s.successes,
                    s.failures
                )
            } else {
                format!(
                    "OFF {} U{} err{} {}",
                    device.endpoint(),
                    device.unit,
                    s.failures,
                    s.last_error.as_deref().unwrap_or("waiting")
                )
            };
            modbus.push((device.name.clone(), detail));
        }
        let _ = dashboard.draw(
            "station gateway",
            &[
                ("PLC", plc),
                ("Connections", links),
                ("Modbus TCP master", modbus),
            ],
        );
    };
    draw(1, 0.0, 0.0, 0);
    let result = async {
        let period = Duration::from_millis(config.period_ms as u64);
        let mut interval = tokio::time::interval_at(tokio::time::Instant::now() + period, period);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut report = Instant::now();
        let (mut count, mut total, mut max, mut overruns) = (0u64, 0.0f64, 0.0f64, 0u64);
        loop {
            if cycles != 0 && bridge.lock().unwrap().generation() >= cycles {
                break;
            }
            tokio::select! {
                result = tokio::signal::ctrl_c() => { result?; break; }
                _ = interval.tick() => {}
            }
            if let Some(task) = &ua_task {
                ensure!(!task.is_finished(), "OPC UA server exited unexpectedly");
            }
            ensure!(
                workers.iter().all(|w| !w.is_finished()),
                "Modbus worker exited unexpectedly"
            );
            let start = Instant::now();
            let generation = {
                let mut guard = bridge
                    .lock()
                    .map_err(|_| anyhow::anyhow!("bridge poisoned"))?;
                guard.cycle(&inputs, config.period_ms)?;
                guard.generation()
            };
            let elapsed = start.elapsed().as_secs_f64() * 1000.0;
            count += 1;
            total += elapsed;
            max = max.max(elapsed);
            if elapsed > config.period_ms as f64 {
                overruns += 1;
            }
            if report.elapsed() >= dashboard.refresh_interval()
                || cycles != 0 && generation >= cycles
            {
                draw(generation, total / count as f64, max, overruns);
                report = Instant::now();
                count = 0;
                total = 0.0;
                max = 0.0;
            }
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;
    let _ = stop.send(true);
    for worker in workers {
        let _ = worker.await;
    }
    if let Some(handle) = ua_handle {
        handle.cancel();
    }
    if let Some(mut task) = ua_task.take() {
        match tokio::time::timeout(Duration::from_secs(5), &mut task).await {
            Ok(Ok(Ok(()))) => {}
            Ok(other) if result.is_ok() => anyhow::bail!("OPC UA shutdown failed: {other:?}"),
            Err(_) => {
                task.abort();
                let _ = task.await;
            }
            _ => {}
        }
    }
    drop(snap7);
    result
}
