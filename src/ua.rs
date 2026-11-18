//! OPC UA adapter for the published S7 image and scan-boundary write queue.
use crate::{
    modbus::{Device, SharedStatus},
    s7::{Bridge, Field, Kind},
};
use anyhow::{Result, ensure};
use opcua::{
    server::{
        Server, ServerBuilder, ServerHandle,
        address_space::{AccessLevel, Variable},
        diagnostics::NamespaceMetadata,
        node_manager::memory::{
            InMemoryNodeManager, InMemoryNodeManagerBuilder, InMemoryNodeManagerImpl,
            InMemoryNodeManagerImplBuilder, SimpleNodeManagerBuilder, SimpleNodeManagerImpl,
        },
    },
    types::{DataValue, NodeId, StatusCode, UAString, Variant},
};
use serde::Deserialize;
use std::{
    collections::{BTreeSet, HashMap},
    path::PathBuf,
    sync::{Arc, Mutex},
};

pub const NAMESPACE: &str = "urn:plc-wasm:tags";
use opcua::{
    core::sync::RwLock,
    server::{
        CreateMonitoredItem,
        address_space::AddressSpace,
        node_manager::{
            MonitoredItemRef, MonitoredItemUpdateRef, ParsedReadValueId, RequestContext,
            ServerContext, WriteNode,
        },
    },
    types::{AttributeId, MonitoringMode, NumericRange, TimestampsToReturn},
};

type WriteCallback = Box<dyn Fn(DataValue, &NumericRange) -> StatusCode + Send + Sync>;
/// Reuse library sampling, but do not notify subscriptions at write admission:
/// only the scan-published getter may produce a new value.
struct QueuedManager {
    simple: SimpleNodeManagerImpl,
    writes: Mutex<HashMap<NodeId, WriteCallback>>,
}
#[async_trait::async_trait]
impl InMemoryNodeManagerImpl for QueuedManager {
    async fn init(&self, space: &mut AddressSpace, context: ServerContext) {
        self.simple.init(space, context).await;
    }
    fn name(&self) -> &str {
        self.simple.name()
    }
    fn namespaces(&self) -> Vec<NamespaceMetadata> {
        self.simple.namespaces()
    }
    async fn read_values(
        &self,
        context: &RequestContext,
        space: &RwLock<AddressSpace>,
        nodes: &[&ParsedReadValueId],
        age: f64,
        timestamps: TimestampsToReturn,
    ) -> Vec<DataValue> {
        self.simple
            .read_values(context, space, nodes, age, timestamps)
            .await
    }
    async fn create_value_monitored_items(
        &self,
        context: &RequestContext,
        space: &RwLock<AddressSpace>,
        items: &mut [&mut &mut CreateMonitoredItem],
    ) {
        self.simple
            .create_value_monitored_items(context, space, items)
            .await;
    }
    async fn modify_monitored_items(
        &self,
        context: &RequestContext,
        items: &[&MonitoredItemUpdateRef],
    ) {
        self.simple.modify_monitored_items(context, items).await;
    }
    async fn set_monitoring_mode(
        &self,
        context: &RequestContext,
        mode: MonitoringMode,
        items: &[&MonitoredItemRef],
    ) {
        self.simple.set_monitoring_mode(context, mode, items).await;
    }
    async fn delete_monitored_items(&self, context: &RequestContext, items: &[&MonitoredItemRef]) {
        self.simple.delete_monitored_items(context, items).await;
    }
    async fn write(
        &self,
        context: &RequestContext,
        space: &RwLock<AddressSpace>,
        nodes: &mut [&mut WriteNode],
    ) -> std::result::Result<(), StatusCode> {
        let mut space = space.write();
        let tree = context.type_tree.read();
        let callbacks = self
            .writes
            .lock()
            .map_err(|_| StatusCode::BadInternalError)?;
        for node in nodes {
            let status = match space.validate_node_write(context, node.value(), &*tree) {
                Err(status) => status,
                Ok(_) if node.value().attribute_id != AttributeId::Value => {
                    StatusCode::BadNotWritable
                }
                Ok(_) => match callbacks.get(&node.value().node_id) {
                    Some(callback) => {
                        callback(node.value().value.clone(), &node.value().index_range)
                    }
                    None => StatusCode::BadNotWritable,
                },
            };
            node.set_status(status);
        }
        Ok(())
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub port: u16,
    pub pki_dir: PathBuf,
    /// Empty means the exported aliases in the S7 map; explicit paths also work.
    #[serde(default)]
    pub tags: Vec<String>,
}

pub fn decode(field: &Field, bytes: &[u8]) -> std::result::Result<Variant, StatusCode> {
    Ok(match field.kind {
        Kind::Bool => Variant::Boolean(bytes[0] != 0),
        Kind::Byte => Variant::Byte(bytes[0]),
        Kind::Word => Variant::UInt16(u16::from_be_bytes(bytes.try_into().unwrap())),
        Kind::Int => Variant::Int16(i16::from_be_bytes(bytes.try_into().unwrap())),
        Kind::Dword => Variant::UInt32(u32::from_be_bytes(bytes.try_into().unwrap())),
        Kind::Dint => Variant::Int32(i32::from_be_bytes(bytes.try_into().unwrap())),
        Kind::Real => Variant::Float(f32::from_be_bytes(bytes.try_into().unwrap())),
        Kind::String => {
            let (text, invalid) = encoding_rs::WINDOWS_1251
                .decode_without_bom_handling(&bytes[2..2 + bytes[1] as usize]);
            if invalid {
                return Err(StatusCode::BadDecodingError);
            }
            Variant::String(UAString::from(text.into_owned()))
        }
    })
}
pub fn encode(field: &Field, value: &Variant) -> std::result::Result<Vec<u8>, StatusCode> {
    Ok(match (field.kind, value) {
        (Kind::Bool, Variant::Boolean(v)) => vec![u8::from(*v)],
        (Kind::Byte, Variant::Byte(v)) => vec![*v],
        (Kind::Word, Variant::UInt16(v)) => v.to_be_bytes().to_vec(),
        (Kind::Int, Variant::Int16(v)) => v.to_be_bytes().to_vec(),
        (Kind::Dword, Variant::UInt32(v)) => v.to_be_bytes().to_vec(),
        (Kind::Dint, Variant::Int32(v)) => v.to_be_bytes().to_vec(),
        (Kind::Real, Variant::Float(v)) if v.is_finite() => v.to_be_bytes().to_vec(),
        (Kind::Real, Variant::Float(_)) => return Err(StatusCode::BadOutOfRange),
        (Kind::String, Variant::String(v)) => {
            let text: &str = v.as_ref();
            let (bytes, _, invalid) = encoding_rs::WINDOWS_1251.encode(text);
            if invalid || bytes.len() > field.capacity as usize {
                return Err(StatusCode::BadOutOfRange);
            }
            let mut data = vec![0; field.capacity as usize + 2];
            data[0] = field.capacity;
            data[1] = bytes.len() as u8;
            data[2..2 + bytes.len()].copy_from_slice(&bytes);
            data
        }
        _ => return Err(StatusCode::BadTypeMismatch),
    })
}

pub fn build(
    config: &Config,
    bridge: Arc<Mutex<Bridge>>,
    devices: &[(Device, SharedStatus)],
) -> Result<(Server, ServerHandle)> {
    ensure!(config.port != 0, "OPC UA port must be nonzero");
    let tags = {
        let guard = bridge
            .lock()
            .map_err(|_| anyhow::anyhow!("bridge poisoned"))?;
        let names: BTreeSet<String> = if config.tags.is_empty() {
            guard.exported_names().clone()
        } else {
            config.tags.iter().cloned().collect()
        };
        ensure!(
            !names.is_empty() && names.len() <= 20000,
            "OPC UA requires 1..20000 selected tags"
        );
        names
            .into_iter()
            .map(|name| {
                let (db, field) = guard.named(&name)?;
                let initial = decode(&field, &guard.read_named(&name)?)?;
                Ok((name, db, field, initial))
            })
            .collect::<Result<Vec<_>>>()?
    };
    let mut device_status = HashMap::new();
    for (device, status) in devices {
        for request in &device.requests {
            if matches!(
                request.operation,
                crate::modbus::Operation::WriteCoils | crate::modbus::Operation::WriteRegisters
            ) {
                continue;
            }
            for name in &request.fields {
                let offset = bridge.lock().unwrap().named(name)?.1.offset;
                device_status.insert(offset, status.clone());
            }
        }
    }
    let (server, handle) = ServerBuilder::new_anonymous("WPLC")
        .application_uri("urn:plc-wasm:server")
        .host("127.0.0.1")
        .port(config.port)
        .discovery_urls(vec![format!("opc.tcp://127.0.0.1:{}/", config.port)])
        .pki_dir(&config.pki_dir)
        .create_sample_keypair(true)
        .with_node_manager(InMemoryNodeManagerBuilder::new(
            |context: ServerContext, space: &mut AddressSpace| QueuedManager {
                simple: SimpleNodeManagerBuilder::new(
                    NamespaceMetadata {
                        namespace_uri: NAMESPACE.to_owned(),
                        ..Default::default()
                    },
                    "plc",
                )
                .build(context, space),
                writes: Mutex::new(HashMap::new()),
            },
        ))
        .build()
        .map_err(|error| anyhow::anyhow!(error))?;
    let manager = handle
        .node_managers()
        .get_of_type::<InMemoryNodeManager<QueuedManager>>()
        .unwrap();
    let ns = handle.get_namespace_index(NAMESPACE).unwrap();
    let mut folders = BTreeSet::new();
    for (name, db, field, initial) in tags {
        // Prefix keeps tag IDs disjoint from folders and diagnostic nodes.
        let id = NodeId::new(ns, format!("tag/{name}"));
        let parent = NodeId::new(ns, format!("db/{db}"));
        let writable = field.writable && !device_status.contains_key(&field.offset);
        {
            let mut space = manager.address_space().write();
            if folders.insert(db) {
                space.add_folder(
                    &parent,
                    if db == 0 {
                        "Process signals".to_owned()
                    } else {
                        format!("DB{db}")
                    },
                    if db == 0 {
                        "Process signals".to_owned()
                    } else {
                        format!("DB{db}")
                    },
                    &NodeId::objects_folder_id(),
                );
            }
            let mut variable = Variable::new(&id, name.as_str(), name.as_str(), initial);
            if writable {
                variable.set_access_level(AccessLevel::CURRENT_READ | AccessLevel::CURRENT_WRITE);
                variable
                    .set_user_access_level(AccessLevel::CURRENT_READ | AccessLevel::CURRENT_WRITE);
            }
            space.add_variables(vec![variable], &parent);
        }
        let read_bridge = bridge.clone();
        let read_name = name.clone();
        let read_field = field.clone();
        let status = device_status.get(&field.offset).cloned();
        manager
            .inner()
            .simple
            .add_read_callback(id.clone(), move |range, _, _| {
                if !range.is_none() {
                    return Err(StatusCode::BadIndexRangeInvalid);
                }
                if let Some(status) = &status {
                    if !status
                        .lock()
                        .map_err(|_| StatusCode::BadInternalError)?
                        .online
                    {
                        return Err(StatusCode::BadNoCommunication);
                    }
                }
                let guard = read_bridge
                    .lock()
                    .map_err(|_| StatusCode::BadInternalError)?;
                if guard.is_faulted() {
                    return Err(StatusCode::BadOutOfService);
                }
                let data = guard
                    .read_named(&read_name)
                    .map_err(|_| StatusCode::BadInternalError)?;
                Ok(DataValue::new_now(decode(&read_field, &data)?))
            });
        let write_bridge = bridge.clone();
        manager.inner().writes.lock().unwrap().insert(
            id,
            Box::new(move |value, range| {
                if !writable {
                    return StatusCode::BadNotWritable;
                }
                if !range.is_none() {
                    return StatusCode::BadIndexRangeInvalid;
                }
                let Some(value) = value.value else {
                    return StatusCode::BadTypeMismatch;
                };
                let bytes = match encode(&field, &value) {
                    Ok(v) => v,
                    Err(e) => return e,
                };
                let Ok(mut guard) = write_bridge.lock() else {
                    return StatusCode::BadInternalError;
                };
                if guard.is_faulted() {
                    return StatusCode::BadOutOfService;
                }
                match guard.queue_named(&name, &bytes) {
                    Ok(()) => StatusCode::Good,
                    Err(_) => StatusCode::BadNotWritable,
                }
            }),
        );
    }
    Ok((server, handle))
}
