use station_runtime::{
    Manifest, Package, Region, Runtime,
    modbus::{self, Device, Operation, Request},
    s7::{Block, Bridge, Field, Kind, Map},
    ua,
};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

fn fixture() -> (Vec<u8>, Map) {
    let manifest = Manifest {
        abi_version: 1,
        name: "protocol-test".into(),
        kind: "cycle-smoke".into(),
        memory_bytes: 65536,
        fuel_per_call: 10000,
        inputs: Region { offset: 0, size: 0 },
        outputs: Region {
            offset: 16,
            size: 32,
        },
        fields: vec![],
        source_sha256: String::new(),
        module_sha256: String::new(),
    };
    let wasm = wat::parse_str(
        r#"(module
        (memory (export "memory") 1 1)
        (func (export "abi_version") (result i32) (i32.const 1))
        (func (export "init") (result i32)
          (i32.store (i32.const 20) (i32.const 7))
          (f32.store (i32.const 24) (f32.const 1.5))
          (i32.store8 (i32.const 32) (i32.const 8)) (i32.const 0))
        (func (export "scan") (param i32) (result i32) (i32.const 0)))"#,
    )
    .unwrap();
    let package = Package::encode(manifest, wasm).unwrap();
    let hash = Package::decode(&package).unwrap().manifest.module_sha256;
    let fields = [
        ("Input", 16, 0, Kind::Word, 0, false),
        ("Command", 20, 16, Kind::Word, 0, true),
        ("Real", 24, 32, Kind::Real, 0, true),
        ("Flag", 28, 64, Kind::Bool, 0, false),
        ("Text", 32, 80, Kind::String, 8, true),
    ]
    .into_iter()
    .map(|(name, offset, bit, kind, capacity, writable)| Field {
        name: name.into(),
        offset,
        bit,
        kind,
        capacity,
        writable,
        aliases: vec![format!("Alias{name}")],
    })
    .collect();
    (
        package,
        Map {
            version: 1,
            module_sha256: hash,
            output_offset: 16,
            output_size: 32,
            blocks: vec![Block {
                number: 1,
                size: 20,
                fields,
            }],
        },
    )
}
fn bridge() -> Bridge {
    let (package, map) = fixture();
    Bridge::new(
        Runtime::load(Package::decode(&package).unwrap()).unwrap(),
        map,
    )
    .unwrap()
}
fn device(address: std::net::SocketAddr) -> Device {
    Device {
        name: "local".into(),
        enabled: true,
        address: address.ip(),
        port: address.port(),
        unit: 1,
        poll_ms: 20,
        timeout_ms: 100,
        requests: vec![Request {
            operation: Operation::ReadHoldingRegisters,
            address: 0,
            fields: vec!["Input".into()],
            swap_words: false,
        }],
    }
}

#[test]
fn device_input_batch_preserves_hmi_permissions_and_is_atomic() {
    let mut bridge = bridge();
    assert!(bridge.queue_named("Input", &[0, 42]).is_err());
    bridge
        .queue_device_inputs(&[("Input".into(), vec![0, 42])])
        .unwrap();
    assert_eq!(bridge.read_named("Input").unwrap(), [0, 0]);
    assert!(
        bridge
            .queue_device_inputs(&[
                ("Input".into(), vec![0, 99]),
                ("Real".into(), f32::NAN.to_be_bytes().to_vec())
            ])
            .is_err()
    );
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_named("Input").unwrap(), [0, 42]);
    assert!(bridge.queue_named("Input", &[0, 1]).is_err());
    assert!(
        bridge
            .queue_device_inputs(&[("Flag".into(), vec![2])])
            .is_err()
    );
}

#[test]
fn mapping_rejects_duplicates_types_and_ranges() {
    let bridge = bridge();
    let mut config = device("127.0.0.1:1502".parse().unwrap());
    modbus::validate(&[config.clone()], &bridge).unwrap();
    let mut draft = config.clone();
    draft.enabled = false;
    draft.requests.clear();
    modbus::validate(&[draft.clone()], &bridge).unwrap();
    draft.enabled = true;
    assert!(modbus::validate(&[draft], &bridge).is_err());
    config.requests[0].fields.push("AliasInput".into());
    assert!(modbus::validate(&[config.clone()], &bridge).is_err());
    config.requests[0].fields = vec!["Text".into()];
    assert!(modbus::validate(&[config.clone()], &bridge).is_err());
    config.requests[0].fields = vec!["Real".into()];
    config.requests[0].address = 65535;
    assert!(modbus::validate(&[config.clone()], &bridge).is_err());
    config.requests[0].address = 0;
    config.requests[0].operation = Operation::ReadCoils;
    assert!(modbus::validate(&[config], &bridge).is_err());
}

#[test]
fn ua_types_and_string_bounds() {
    use opcua::types::{StatusCode, Variant};
    let bridge = bridge();
    for field in &bridge.map().blocks[0].fields {
        let bytes = bridge.read_named(&field.name).unwrap();
        assert_eq!(
            ua::encode(field, &ua::decode(field, &bytes).unwrap()).unwrap(),
            bytes
        );
    }
    let field = bridge.named("Real").unwrap().1;
    assert_eq!(
        ua::encode(&field, &Variant::Double(1.0)),
        Err(StatusCode::BadTypeMismatch)
    );
    assert_eq!(
        ua::encode(&field, &Variant::Float(f32::NAN)),
        Err(StatusCode::BadOutOfRange)
    );
    let field = bridge.named("Text").unwrap().1;
    let russian = Variant::String("Привет".into());
    let encoded = ua::encode(&field, &russian).unwrap();
    assert_eq!(&encoded[2..8], &[0xcf, 0xf0, 0xe8, 0xe2, 0xe5, 0xf2]);
    assert_eq!(ua::decode(&field, &encoded).unwrap(), russian);
    assert_eq!(
        ua::encode(&field, &Variant::String("\u{1f600}".into())),
        Err(StatusCode::BadOutOfRange)
    );
    assert_eq!(
        ua::encode(&field, &Variant::String("123456789".into())),
        Err(StatusCode::BadOutOfRange)
    );
}

async fn reply(stream: &mut tokio::net::TcpStream, body: &[u8]) -> Vec<u8> {
    let mut header = [0; 7];
    stream.read_exact(&mut header).await.unwrap();
    let length = u16::from_be_bytes([header[4], header[5]]) as usize - 1;
    let mut request = vec![0; length];
    stream.read_exact(&mut request).await.unwrap();
    header[4..6].copy_from_slice(&((body.len() + 1) as u16).to_be_bytes());
    stream.write_all(&header).await.unwrap();
    stream.write_all(body).await.unwrap();
    request
}

#[tokio::test]
async fn modbus_reads_writes_and_scan_boundary_over_tcp() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut device = device(listener.local_addr().unwrap());
    device.requests.extend([
        Request {
            operation: Operation::ReadDiscreteInputs,
            address: 3,
            fields: vec!["Flag".into()],
            swap_words: false,
        },
        Request {
            operation: Operation::WriteRegisters,
            address: 5,
            fields: vec!["Command".into(), "Real".into()],
            swap_words: true,
        },
        Request {
            operation: Operation::WriteCoils,
            address: 9,
            fields: vec!["Flag".into()],
            swap_words: false,
        },
    ]);
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        assert_eq!(reply(&mut stream, &[3, 2, 0, 42]).await, [3, 0, 0, 0, 1]);
        assert_eq!(reply(&mut stream, &[2, 1, 1]).await, [2, 0, 3, 0, 1]);
        assert_eq!(
            reply(&mut stream, &[16, 0, 5, 0, 3]).await,
            [16, 0, 5, 0, 3, 6, 0, 7, 0, 0, 0x3f, 0xc0]
        );
        assert_eq!(
            reply(&mut stream, &[15, 0, 9, 0, 1]).await,
            [15, 0, 9, 0, 1, 1, 0]
        );
    });
    let bridge = Arc::new(Mutex::new(bridge()));
    modbus::validate(&[device.clone()], &bridge.lock().unwrap()).unwrap();
    let mut context =
        tokio_modbus::client::tcp::connect_slave(device.endpoint(), tokio_modbus::slave::Slave(1))
            .await
            .unwrap();
    modbus::poll_device(&mut context, &device, &bridge)
        .await
        .unwrap();
    server.await.unwrap();
    let mut bridge = bridge.lock().unwrap();
    assert_eq!(bridge.read_named("Input").unwrap(), [0, 0]);
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_named("Input").unwrap(), [0, 42]);
    assert_eq!(bridge.read_named("Flag").unwrap(), [1]);
}

#[tokio::test]
async fn modbus_exception_discards_whole_input_batch() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut device = device(listener.local_addr().unwrap());
    device.requests.push(Request {
        operation: Operation::ReadInputRegisters,
        address: 9,
        fields: vec!["Command".into()],
        swap_words: false,
    });
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.unwrap();
        reply(&mut stream, &[3, 2, 0, 42]).await;
        reply(&mut stream, &[0x84, 2]).await;
    });
    let bridge = Arc::new(Mutex::new(bridge()));
    let mut context = tokio_modbus::client::tcp::connect(device.endpoint())
        .await
        .unwrap();
    assert!(
        modbus::poll_device(&mut context, &device, &bridge)
            .await
            .is_err()
    );
    server.await.unwrap();
    let mut bridge = bridge.lock().unwrap();
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_named("Input").unwrap(), [0, 0]);
}

#[tokio::test]
async fn worker_reconnects_after_timeout_and_stops() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let device = device(listener.local_addr().unwrap());
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        tokio::time::sleep(Duration::from_millis(160)).await;
        drop(stream);
        let (mut stream, _) = listener.accept().await.unwrap();
        reply(&mut stream, &[3, 2, 0, 42]).await;
        tokio::time::sleep(Duration::from_millis(300)).await;
    });
    let bridge = Arc::new(Mutex::new(bridge()));
    let status = Arc::new(Mutex::new(modbus::Status::default()));
    let (stop, receiver) = tokio::sync::watch::channel(false);
    let worker = tokio::spawn(modbus::worker(
        device,
        bridge.clone(),
        status.clone(),
        receiver,
    ));
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if status.lock().unwrap().successes > 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert!(status.lock().unwrap().failures > 0);
    stop.send(true).unwrap();
    worker.await.unwrap();
    server.await.unwrap();
    assert!(!status.lock().unwrap().online);
}

#[test]
fn optional_export_for_independent_clients() {
    if let Ok(directory) = std::env::var("PLC_PROTOCOL_FIXTURE_DIR") {
        let directory = std::path::PathBuf::from(directory);
        std::fs::create_dir_all(&directory).unwrap();
        let (package, map) = fixture();
        std::fs::write(directory.join("program.pkg"), package).unwrap();
        std::fs::write(
            directory.join("s7-map.json"),
            serde_json::to_vec(&map).unwrap(),
        )
        .unwrap();
    }
}
