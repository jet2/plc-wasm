//! Explicitly mapped Modbus TCP master. No network I/O under the PLC mutex.
use crate::s7::{Bridge, Kind};
use anyhow::{Context as _, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    net::{IpAddr, SocketAddr},
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio_modbus::{
    client::{Context, Reader, Writer, tcp},
    slave::Slave,
};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Operation {
    ReadCoils,
    ReadDiscreteInputs,
    ReadHoldingRegisters,
    ReadInputRegisters,
    WriteCoils,
    WriteRegisters,
}
impl Operation {
    fn bits(self) -> bool {
        matches!(
            self,
            Self::ReadCoils | Self::ReadDiscreteInputs | Self::WriteCoils
        )
    }
    fn input(self) -> bool {
        !matches!(self, Self::WriteCoils | Self::WriteRegisters)
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub operation: Operation,
    /// Zero-based protocol address, not 40001 notation.
    pub address: u16,
    pub fields: Vec<String>,
    #[serde(default)]
    pub swap_words: bool,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Device {
    pub name: String,
    #[serde(default = "enabled_by_default")]
    pub enabled: bool,
    pub address: IpAddr,
    pub port: u16,
    pub unit: u8,
    pub poll_ms: u64,
    pub timeout_ms: u64,
    pub requests: Vec<Request>,
}
fn enabled_by_default() -> bool {
    true
}
impl Device {
    pub fn endpoint(&self) -> SocketAddr {
        SocketAddr::new(self.address, self.port)
    }
}
#[derive(Clone, Default, Debug, Serialize)]
pub struct Status {
    pub online: bool,
    pub successes: u64,
    pub failures: u64,
    pub last_success_unix_ms: u64,
    pub last_error: Option<String>,
}
pub type SharedStatus = Arc<Mutex<Status>>;

fn width(kind: Kind, bits: bool) -> Result<usize> {
    if bits {
        ensure!(kind == Kind::Bool, "coil mapping requires BOOL");
        return Ok(1);
    }
    Ok(match kind {
        Kind::Byte | Kind::Word | Kind::Int => 1,
        Kind::Dword | Kind::Dint | Kind::Real => 2,
        _ => anyhow::bail!("register mapping requires BYTE/WORD/INT/DWORD/DINT/REAL"),
    })
}

pub fn validate(devices: &[Device], bridge: &Bridge) -> Result<()> {
    ensure!(devices.len() <= 64, "too many Modbus devices");
    let mut names = HashSet::new();
    let mut targets = HashSet::new();
    for device in devices {
        if !device.enabled {
            continue;
        }
        ensure!(
            !device.name.is_empty() && device.name.len() <= 128 && names.insert(&device.name),
            "duplicate or invalid device name"
        );
        ensure!(
            device.port != 0 && !device.address.is_unspecified(),
            "invalid device address"
        );
        ensure!(device.unit != 0, "broadcast unit is not supported");
        ensure!(
            (10..=60000).contains(&device.poll_ms) && (10..=60000).contains(&device.timeout_ms),
            "invalid Modbus timing"
        );
        ensure!(
            !device.requests.is_empty() && device.requests.len() <= 256,
            "invalid Modbus request count"
        );
        for request in &device.requests {
            ensure!(!request.fields.is_empty(), "empty Modbus mapping");
            let mut quantity = 0;
            for name in &request.fields {
                let (_, field) = bridge.named(name)?;
                quantity += width(field.kind, request.operation.bits())?;
                if request.operation.input() {
                    ensure!(
                        targets.insert(field.offset),
                        "multiple Modbus input mappings for one PLC field"
                    );
                }
            }
            let max = match request.operation {
                Operation::WriteCoils => 1968,
                Operation::WriteRegisters => 123,
                operation if operation.bits() => 2000,
                _ => 125,
            };
            ensure!(
                quantity <= max && request.address as usize + quantity <= 65536,
                "Modbus request exceeds protocol range"
            );
            ensure!(
                !request.swap_words || !request.operation.bits(),
                "word order does not apply to coils"
            );
        }
    }
    Ok(())
}

/// One device poll is staged locally and admitted atomically only after all requests succeed.
pub async fn poll_device(
    context: &mut Context,
    device: &Device,
    bridge: &Arc<Mutex<Bridge>>,
) -> Result<()> {
    let plans = {
        let guard = bridge
            .lock()
            .map_err(|_| anyhow::anyhow!("bridge poisoned"))?;
        ensure!(!guard.is_faulted(), "PLC faulted");
        device
            .requests
            .iter()
            .map(|request| {
                request
                    .fields
                    .iter()
                    .map(|name| {
                        let (_, field) = guard.named(name)?;
                        Ok((field.kind, guard.read_named(name)?))
                    })
                    .collect::<Result<Vec<_>>>()
            })
            .collect::<Result<Vec<_>>>()?
    };
    let mut incoming = Vec::new();
    for (request, fields) in device.requests.iter().zip(plans) {
        let quantity: usize = fields
            .iter()
            .map(|(kind, _)| width(*kind, request.operation.bits()))
            .collect::<Result<Vec<_>>>()?
            .iter()
            .sum();
        let quantity = u16::try_from(quantity)?;
        let future = async {
            if request.operation.bits() {
                let values = match request.operation {
                    Operation::ReadCoils => context.read_coils(request.address, quantity).await??,
                    Operation::ReadDiscreteInputs => {
                        context
                            .read_discrete_inputs(request.address, quantity)
                            .await??
                    }
                    Operation::WriteCoils => {
                        let bits: Vec<_> = fields.iter().map(|(_, bytes)| bytes[0] != 0).collect();
                        context
                            .write_multiple_coils(request.address, &bits)
                            .await??;
                        return Ok(Vec::new());
                    }
                    _ => unreachable!(),
                };
                ensure!(values.len() == fields.len(), "wrong coil response length");
                return Ok(values
                    .into_iter()
                    .map(|v| vec![u8::from(v)])
                    .collect::<Vec<_>>());
            }
            let words = match request.operation {
                Operation::ReadHoldingRegisters => {
                    context
                        .read_holding_registers(request.address, quantity)
                        .await??
                }
                Operation::ReadInputRegisters => {
                    context
                        .read_input_registers(request.address, quantity)
                        .await??
                }
                Operation::WriteRegisters => {
                    let mut words = Vec::new();
                    for (_, bytes) in &fields {
                        let mut part: Vec<u16> = if bytes.len() == 1 {
                            vec![bytes[0] as u16]
                        } else {
                            bytes
                                .chunks_exact(2)
                                .map(|b| u16::from_be_bytes([b[0], b[1]]))
                                .collect()
                        };
                        if request.swap_words && part.len() == 2 {
                            part.reverse();
                        }
                        words.extend(part);
                    }
                    context
                        .write_multiple_registers(request.address, &words)
                        .await??;
                    return Ok(Vec::new());
                }
                _ => unreachable!(),
            };
            ensure!(
                words.len() == quantity as usize,
                "wrong register response length"
            );
            let mut cursor = 0;
            let mut values = Vec::new();
            for (kind, _) in &fields {
                let count = width(*kind, false)?;
                let mut part = words[cursor..cursor + count].to_vec();
                cursor += count;
                if request.swap_words && count == 2 {
                    part.reverse();
                }
                let bytes = if *kind == Kind::Byte {
                    vec![u8::try_from(part[0]).context("register does not fit BYTE")?]
                } else {
                    part.into_iter().flat_map(u16::to_be_bytes).collect()
                };
                values.push(bytes);
            }
            Ok::<_, anyhow::Error>(values)
        };
        let values = tokio::time::timeout(Duration::from_millis(device.timeout_ms), future)
            .await
            .context("Modbus request timeout")??;
        incoming.extend(request.fields.iter().cloned().zip(values));
    }
    bridge
        .lock()
        .map_err(|_| anyhow::anyhow!("bridge poisoned"))?
        .queue_device_inputs(&incoming)?;
    Ok(())
}

pub async fn worker(
    device: Device,
    bridge: Arc<Mutex<Bridge>>,
    status: SharedStatus,
    mut stop: tokio::sync::watch::Receiver<bool>,
) {
    let mut context = None;
    loop {
        if *stop.borrow() {
            break;
        }
        let result = tokio::select! {
            _ = stop.changed() => break,
            result = async {
                if context.is_none() {
                    context = Some(tokio::time::timeout(Duration::from_millis(device.timeout_ms),
                        tcp::connect_slave(device.endpoint(), Slave(device.unit))).await.context("Modbus connect timeout")??);
                }
                poll_device(context.as_mut().unwrap(), &device, &bridge).await
            } => result,
        };
        {
            let mut state = status.lock().unwrap();
            match result {
                Ok(()) => {
                    state.online = true;
                    state.successes += 1;
                    state.last_error = None;
                    state.last_success_unix_ms = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis() as u64;
                }
                Err(error) => {
                    context = None; // Discard uncertain transport state after errors/timeouts.
                    state.online = false;
                    state.failures += 1;
                    state.last_error = Some(error.to_string());
                }
            }
        }
        tokio::select! {
            _ = stop.changed() => break,
            _ = tokio::time::sleep(Duration::from_millis(device.poll_ms)) => {}
        }
    }
    status.lock().unwrap().online = false;
}
