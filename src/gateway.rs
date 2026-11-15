//! Concurrent adapters sharing one PLC image and one scan scheduler.
use crate::{modbus, s7::Bridge, snap7, ua};
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

pub async fn run(mut bridge: Bridge, inputs: Vec<u8>, config: Config, cycles: u64) -> Result<()> {
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
        .map(|settings| snap7::Server::start(&settings.library, settings.port, bridge.clone()))
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
    println!(
        "{}",
        serde_json::json!({"event":"gateway_started","pid":std::process::id(),"period_ms":config.period_ms,"opcua":ua_handle.is_some(),"opcua_endpoint":opcua_endpoint,"snap7":snap7.is_some(),"modbus_devices":devices.len()})
    );
    let result = async {
        let period = Duration::from_millis(config.period_ms as u64);
        let mut interval = tokio::time::interval_at(tokio::time::Instant::now() + period, period);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut report = Instant::now();
        let (mut count, mut total, mut max, mut overruns) = (0u64, 0.0f64, 0.0f64, 0u64);
        loop {
            if cycles != 0 && bridge.lock().unwrap().generation() >= cycles { break; }
            tokio::select! {
                result = tokio::signal::ctrl_c() => { result?; break; }
                _ = interval.tick() => {}
            }
            if let Some(task) = &ua_task { ensure!(!task.is_finished(), "OPC UA server exited unexpectedly"); }
            ensure!(workers.iter().all(|w| !w.is_finished()), "Modbus worker exited unexpectedly");
            let start = Instant::now();
            let generation = {
                let mut guard = bridge.lock().map_err(|_| anyhow::anyhow!("bridge poisoned"))?;
                guard.cycle(&inputs, config.period_ms)?;
                guard.generation()
            };
            let elapsed = start.elapsed().as_secs_f64() * 1000.0;
            count += 1; total += elapsed; max = max.max(elapsed);
            if elapsed > config.period_ms as f64 { overruns += 1; }
            if report.elapsed() >= Duration::from_secs(10) || cycles != 0 && generation >= cycles {
                let statuses: Vec<_> = devices.iter().map(|(device, status)| serde_json::json!({"name":device.name,"status":*status.lock().unwrap()})).collect();
                println!("{}", serde_json::json!({"event":"gateway_cycles","generation":generation,"mean_cycle_ms":total/count as f64,"max_cycle_ms":max,"cycle_overruns":overruns,"modbus":statuses}));
                report = Instant::now(); count = 0; total = 0.0; max = 0.0;
            }
        }
        Ok::<(), anyhow::Error>(())
    }.await;
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
