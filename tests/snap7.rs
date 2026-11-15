//! Native loopback integration tests. Set SNAP7_TEST_LIBRARY to a trusted library.
use libloading::Library;
use station_runtime::{
    Manifest, Package, Region, Runtime,
    s7::{Block, Bridge, Field, Kind, Map},
    snap7::Server,
};
use std::{
    ffi::{c_char, c_int, c_void},
    net::TcpListener,
    path::PathBuf,
    sync::{Arc, Mutex},
};

type Transfer =
    unsafe extern "system" fn(usize, c_int, c_int, c_int, c_int, c_int, *mut c_void) -> c_int;
struct Client {
    handle: usize,
    read: Transfer,
    write: Transfer,
    disconnect: unsafe extern "system" fn(usize) -> c_int,
    destroy: unsafe extern "system" fn(*mut usize),
    _library: Library,
}
impl Client {
    fn connect(path: &PathBuf, port: u16) -> Self {
        unsafe {
            let library = Library::new(path).unwrap();
            let create: unsafe extern "system" fn() -> usize =
                *library.get(b"Cli_Create\0").unwrap();
            let set: unsafe extern "system" fn(usize, c_int, *mut c_void) -> c_int =
                *library.get(b"Cli_SetParam\0").unwrap();
            let connect: unsafe extern "system" fn(usize, *const c_char, c_int, c_int) -> c_int =
                *library.get(b"Cli_ConnectTo\0").unwrap();
            let client = Self {
                handle: create(),
                read: *library.get(b"Cli_ReadArea\0").unwrap(),
                write: *library.get(b"Cli_WriteArea\0").unwrap(),
                disconnect: *library.get(b"Cli_Disconnect\0").unwrap(),
                destroy: *library.get(b"Cli_Destroy\0").unwrap(),
                _library: library,
            };
            let mut port = port;
            assert_eq!(set(client.handle, 2, (&mut port as *mut u16).cast()), 0);
            assert_eq!(connect(client.handle, c"127.0.0.1".as_ptr(), 0, 2), 0);
            client
        }
    }
    fn read(
        &self,
        db: i32,
        start: i32,
        amount: i32,
        word: i32,
        bytes: usize,
    ) -> Result<Vec<u8>, i32> {
        let mut data = vec![0; bytes];
        let code = unsafe {
            (self.read)(
                self.handle,
                0x84,
                db,
                start,
                amount,
                word,
                data.as_mut_ptr().cast(),
            )
        };
        if code == 0 { Ok(data) } else { Err(code) }
    }
    fn write(&self, area: i32, db: i32, start: i32, amount: i32, word: i32, data: &[u8]) -> i32 {
        let mut data = data.to_vec();
        unsafe {
            (self.write)(
                self.handle,
                area,
                db,
                start,
                amount,
                word,
                data.as_mut_ptr().cast(),
            )
        }
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        unsafe {
            (self.disconnect)(self.handle);
            (self.destroy)(&mut self.handle);
        }
    }
}

fn library() -> Option<PathBuf> {
    let path = std::env::var_os("SNAP7_TEST_LIBRARY").map(PathBuf::from);
    if path.is_none() {
        eprintln!("native Snap7 integration skipped: SNAP7_TEST_LIBRARY is unset");
    }
    path
}
fn fixture(scan: &str) -> Arc<Mutex<Bridge>> {
    let manifest = Manifest {
        abi_version: 1,
        name: "network-test".into(),
        kind: "cycle-smoke".into(),
        memory_bytes: 65536,
        fuel_per_call: 10000,
        inputs: Region { offset: 0, size: 0 },
        outputs: Region {
            offset: 16,
            size: 28,
        },
        fields: vec![],
        source_sha256: String::new(),
        module_sha256: String::new(),
    };
    let wasm = wat::parse_str(format!(
        r#"(module
      (memory (export "memory") 1 1)
      (func (export "abi_version") (result i32) (i32.const 1))
      (func (export "init") (result i32)
        (i32.store (i32.const 16) (i32.const 1))
        (i32.store (i32.const 24) (i32.const -2))
        (f32.store (i32.const 28) (f32.const 1.5))
        (i32.store8 (i32.const 32) (i32.const 5)) (i32.const 0))
      (func (export "scan") (param i32) (result i32) {scan}))"#
    ))
    .unwrap();
    let package = Package::decode(&Package::encode(manifest, wasm).unwrap()).unwrap();
    let fields = [
        ("Status", 16, 0, Kind::Bool, 0, false),
        ("Switch", 20, 7, Kind::Bool, 0, true),
        ("Signed", 24, 16, Kind::Int, 0, true),
        ("Float", 28, 32, Kind::Real, 0, true),
        ("Text", 32, 64, Kind::String, 5, true),
        ("Bits", 40, 128, Kind::Dword, 0, true),
    ]
    .into_iter()
    .map(|(name, offset, bit, kind, capacity, writable)| Field {
        name: name.into(),
        offset,
        bit,
        kind,
        capacity,
        writable,
        aliases: vec![],
    })
    .collect();
    let map = Map {
        signals: vec![],
        version: 1,
        module_sha256: package.manifest.module_sha256.clone(),
        output_offset: 16,
        output_size: 28,
        blocks: vec![Block {
            number: 7,
            size: 2048,
            fields,
        }],
    };
    Arc::new(Mutex::new(
        Bridge::new(Runtime::load(package).unwrap(), map).unwrap(),
    ))
}
fn listen(path: &PathBuf, bridge: Arc<Mutex<Bridge>>) -> (Server, u16) {
    let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = reservation.local_addr().unwrap().port();
    drop(reservation);
    (Server::start(path, port, bridge).unwrap(), port)
}

#[test]
fn native_bit_word_real_string_and_fragmented_reads() {
    let Some(path) = library() else { return };
    let bridge = fixture("(i32.const 0)");
    let (server, port) = listen(&path, bridge.clone());
    let client = Client::connect(&path, port);
    assert_eq!(client.read(7, 0, 1, 1, 1).unwrap(), vec![1]);
    assert_eq!(client.read(7, 7, 1, 1, 1).unwrap(), vec![0]);
    assert_eq!(client.read(7, 2, 1, 4, 2).unwrap(), vec![255, 254]);
    assert_eq!(client.read(7, 4, 1, 8, 4).unwrap(), 1.5f32.to_be_bytes());
    assert_eq!(client.write(0x84, 7, 7, 1, 1, &[1]), 0);
    assert_eq!(client.write(0x84, 7, 2, 1, 4, &[128, 0]), 0);
    assert_eq!(client.write(0x84, 7, 4, 1, 8, &(-12.5f32).to_be_bytes()), 0);
    assert_eq!(
        client.write(0x84, 7, 8, 7, 2, &[5, 3, b'a', b'b', b'c', 0, 0]),
        0
    );
    assert_eq!(
        client.write(0x84, 7, 16, 1, 6, &[0x89, 0xab, 0xcd, 0xef]),
        0
    );
    assert_eq!(client.read(7, 7, 1, 1, 1).unwrap(), vec![0]); // accepted, not published yet
    bridge.lock().unwrap().cycle(&[], 100).unwrap();
    assert_eq!(client.read(7, 7, 1, 1, 1).unwrap(), vec![1]);
    assert_eq!(client.read(7, 0, 1, 2, 1).unwrap(), vec![129]);
    assert_eq!(client.read(7, 2, 1, 4, 2).unwrap(), vec![128, 0]);
    assert_eq!(
        client.read(7, 4, 1, 8, 4).unwrap(),
        (-12.5f32).to_be_bytes()
    );
    assert_eq!(
        client.read(7, 8, 7, 2, 7).unwrap(),
        vec![5, 3, b'a', b'b', b'c', 0, 0]
    );
    let large = client.read(7, 0, 2048, 2, 2048).unwrap();
    assert_eq!(&large[16..20], &[0x89, 0xab, 0xcd, 0xef]);
    assert!(large[20..].iter().all(|b| *b == 0));
    assert_eq!(server.status().unwrap().writes, 5);
    assert!(server.status().unwrap().reads >= 10);
}

#[test]
fn native_rejects_invalid_writes_and_faulted_reads() {
    let Some(path) = library() else { return };
    let bridge = fixture("unreachable");
    let (server, port) = listen(&path, bridge.clone());
    let client = Client::connect(&path, port);
    for (area, db, start, word, data) in [
        (0x84, 7, 0, 1, vec![0]),
        (0x84, 8, 2, 2, vec![0]),
        (0x83, 7, 2, 2, vec![0]),
        (0x84, 7, 2048, 2, vec![0]),
        (0x84, 7, 4, 8, vec![0x7f, 0x80, 0, 0]),
        (0x84, 7, 8, 2, vec![4, 0, 0, 0, 0, 0, 0]),
    ] {
        let count = if word == 8 { 1 } else { data.len() as i32 };
        assert_ne!(client.write(area, db, start, count, word, &data), 0);
    }
    assert!(client.read(7, 2047, 2, 2, 2).is_err());
    assert!(bridge.lock().unwrap().cycle(&[], 100).is_err());
    assert!(client.read(7, 0, 1, 2, 1).is_err());
    assert!(server.status().unwrap().rejected >= 8);
}

#[test]
fn native_reconnect_concurrent_clients_and_shutdown() {
    let Some(path) = library() else { return };
    let bridge = fixture("(i32.const 0)");
    let (server, port) = listen(&path, bridge.clone());
    for _ in 0..3 {
        let client = Client::connect(&path, port);
        assert_eq!(client.read(7, 0, 1, 2, 1).unwrap(), vec![1]);
    }
    let threads: Vec<_> = (0..3)
        .map(|_| {
            let path = path.clone();
            std::thread::spawn(move || {
                let client = Client::connect(&path, port);
                for _ in 0..20 {
                    assert_eq!(client.read(7, 2, 1, 4, 2).unwrap(), vec![255, 254]);
                }
            })
        })
        .collect();
    for _ in 0..20 {
        bridge.lock().unwrap().cycle(&[], 100).unwrap();
    }
    for thread in threads {
        thread.join().unwrap();
    }
    let client = Client::connect(&path, port);
    drop(server); // worker shutdown must complete even with an idle connected client
    assert!(client.read(7, 0, 1, 2, 1).is_err());
}

#[test]
fn native_multi_item_results_and_bind_failure_cleanup() {
    let Some(path) = library() else { return };
    let bridge = fixture("(i32.const 0)");
    let (_server, port) = listen(&path, bridge.clone());
    assert!(Server::start(&path, port, fixture("(i32.const 0)")).is_err());
    let client = Client::connect(&path, port);
    #[repr(C)]
    struct Item {
        area: c_int,
        word: c_int,
        result: c_int,
        db: c_int,
        start: c_int,
        amount: c_int,
        data: *mut c_void,
    }
    let mut first = [0u8; 2];
    let mut second = [0u8; 2];
    let mut items = [
        Item {
            area: 0x84,
            word: 4,
            result: -1,
            db: 7,
            start: 2,
            amount: 1,
            data: first.as_mut_ptr().cast(),
        },
        Item {
            area: 0x84,
            word: 4,
            result: -1,
            db: 8,
            start: 2,
            amount: 1,
            data: second.as_mut_ptr().cast(),
        },
    ];
    unsafe {
        let read: unsafe extern "system" fn(usize, *mut Item, c_int) -> c_int =
            *client._library.get(b"Cli_ReadMultiVars\0").unwrap();
        assert_eq!(read(client.handle, items.as_mut_ptr(), 2), 0);
    }
    assert_eq!(items[0].result, 0);
    assert_ne!(items[1].result, 0);
    assert_eq!(first, [255, 254]);
    assert_eq!(client.read(7, 0, 1, 2, 1).unwrap(), vec![1]);
}
