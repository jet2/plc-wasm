use station_runtime::s7::{Block, Bridge, Field, Kind, Map};
use station_runtime::{Manifest, Package, Region, Runtime};

fn fixture(scan: &str) -> (Runtime, Map) {
    let manifest = Manifest {
        abi_version: 1,
        name: "codec-test".into(),
        kind: "cycle-smoke".into(),
        memory_bytes: 65536,
        fuel_per_call: 10000,
        inputs: Region { offset: 0, size: 0 },
        outputs: Region {
            offset: 16,
            size: 48,
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
          (i32.store (i32.const 28) (i32.const 0x89abcdef))
          (f32.store (i32.const 32) (f32.const 1.5))
          (i32.store8 (i32.const 36) (i32.const 5))
          (i32.store (i32.const 44) (i32.const 255))
          (i32.store (i32.const 48) (i32.const 65535))
          (i32.store (i32.const 52) (i32.const -2147483648))
          (i32.const 0))
        (func (export "scan") (param i32) (result i32) {scan}))"#
    ))
    .unwrap();
    let package = Package::decode(&Package::encode(manifest, wasm).unwrap()).unwrap();
    let mut fields = Vec::new();
    for (name, offset, bit, kind, capacity, writable) in [
        ("Flags.Read", 16, 0, Kind::Bool, 0, false),
        ("Flags.Write", 20, 1, Kind::Bool, 0, true),
        ("Signed", 24, 16, Kind::Int, 0, true),
        ("Bits", 28, 32, Kind::Dword, 0, true),
        ("Float", 32, 64, Kind::Real, 0, true),
        ("Text", 36, 96, Kind::String, 5, true),
        ("Byte", 44, 160, Kind::Byte, 0, true),
        ("Word", 48, 176, Kind::Word, 0, true),
        ("Long", 52, 192, Kind::Dint, 0, true),
    ] {
        fields.push(Field {
            name: name.into(),
            offset,
            bit,
            kind,
            capacity,
            writable,
            aliases: vec![],
        });
    }
    let map = Map {
        signals: vec![],
        version: 1,
        module_sha256: package.manifest.module_sha256.clone(),
        output_offset: 16,
        output_size: 48,
        blocks: vec![Block {
            number: 17,
            size: 28,
            fields,
        }],
    };
    (Runtime::load(package).unwrap(), map)
}

#[test]
fn big_endian_widths_signs_bits_real_and_string_header() {
    let (runtime, map) = fixture("(i32.const 0)");
    let bridge = Bridge::new(runtime, map).unwrap();
    assert_eq!(
        bridge.read_db(17, 0, 12).unwrap(),
        vec![1, 0, 255, 254, 137, 171, 205, 239, 63, 192, 0, 0]
    );
    assert_eq!(
        bridge.read_named("text").unwrap(),
        vec![5, 0, 0, 0, 0, 0, 0]
    );
    assert_eq!(bridge.read_named("Long").unwrap(), vec![128, 0, 0, 0]);
    assert_eq!(bridge.read_named("Byte").unwrap(), vec![255]);
    assert_eq!(bridge.read_named("Word").unwrap(), vec![255, 255]);
    assert!(bridge.read_bit(17, 0, 0).unwrap());
    assert!(!bridge.read_bit(17, 0, 1).unwrap());
    assert!(bridge.read_db(17, usize::MAX, 2).is_err());
    assert!(bridge.read_db(18, 0, 1).is_err());
}

#[test]
fn pending_writes_are_invisible_until_cycle_and_keep_neighbor_bits() {
    let (runtime, map) = fixture("(i32.const 0)");
    let mut bridge = Bridge::new(runtime, map).unwrap();
    bridge.queue_bit(17, 0, 1, true).unwrap();
    bridge.queue_named("Signed", &[128, 0]).unwrap();
    bridge
        .queue_named("Text", &[5, 3, b'a', b'b', b'c', 0, 0])
        .unwrap();
    bridge.queue_bytes(17, 4, &[0x12, 0x34]).unwrap();
    bridge.queue_bytes(17, 6, &[0x56, 0x78]).unwrap();
    assert_eq!(bridge.read_db(17, 0, 4).unwrap(), vec![1, 0, 255, 254]);
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.generation(), 1);
    assert_eq!(
        bridge.read_db(17, 0, 8).unwrap(),
        vec![3, 0, 128, 0, 0x12, 0x34, 0x56, 0x78]
    );
    assert_eq!(
        bridge.read_named("Text").unwrap(),
        vec![5, 3, b'a', b'b', b'c', 0, 0]
    );
    bridge.queue_named("Flags.Write", &[0]).unwrap();
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_db(17, 0, 1).unwrap(), vec![1]);
}

#[test]
fn queued_values_reach_scan_with_sign_extension() {
    let (runtime, map) =
        fixture("(i32.store (i32.const 52) (i32.load (i32.const 24))) (i32.const 0)");
    let mut bridge = Bridge::new(runtime, map).unwrap();
    bridge.queue_named("Signed", &[255, 254]).unwrap();
    bridge.queue_named("Signed", &[128, 0]).unwrap();
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_named("Long").unwrap(), vec![255, 255, 128, 0]);
}

#[test]
fn malformed_and_unauthorized_writes_do_not_replace_valid_pending_data() {
    let (runtime, map) = fixture("(i32.const 0)");
    let mut bridge = Bridge::new(runtime, map).unwrap();
    bridge.queue_bit(17, 0, 1, true).unwrap();
    assert!(bridge.queue_bit(17, 0, 0, false).is_err());
    assert!(bridge.queue_bytes(17, 0, &[0]).is_err());
    assert!(bridge.queue_bytes(17, 1, &[0]).is_err());
    assert!(bridge.queue_named("Float", &[127, 128, 0, 0]).is_err());
    assert!(bridge.queue_named("Text", &[4, 0, 0, 0, 0, 0, 0]).is_err());
    assert!(bridge.queue_named("Text", &[5, 6, 0, 0, 0, 0, 0]).is_err());
    assert!(bridge.queue_bytes(17, 27, &[0, 0]).is_err());
    assert!(bridge.cycle(&[], 0).is_err());
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_db(17, 0, 1).unwrap(), vec![3]);
    bridge.queue_bit(17, 0, 1, false).unwrap();
    bridge.discard_pending();
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_db(17, 0, 1).unwrap(), vec![3]);
}

#[test]
fn traps_and_invalid_output_keep_last_published_generation() {
    for scan in [
        "unreachable",
        "(i32.const 1)",
        "(i32.store8 (i32.const 37) (i32.const 9)) (i32.const 0)",
    ] {
        let (runtime, map) = fixture(scan);
        let mut bridge = Bridge::new(runtime, map).unwrap();
        let old = bridge.read_db(17, 0, 28).unwrap();
        bridge.queue_named("Signed", &[0, 7]).unwrap();
        assert!(bridge.cycle(&[], 100).is_err());
        assert!(bridge.is_faulted());
        assert_eq!(bridge.generation(), 0);
        assert_eq!(bridge.read_db(17, 0, 28).unwrap(), old);
        assert!(bridge.queue_named("Signed", &[0, 8]).is_err());
        assert!(bridge.cycle(&[], 100).is_err());
    }
}

#[test]
fn maps_reject_wrong_module_overlaps_alignment_and_out_of_bounds() {
    let (runtime, map) = fixture("(i32.const 0)");
    for mutation in 0..7 {
        let mut bad = map.clone();
        match mutation {
            0 => bad.module_sha256 = "0".repeat(64),
            1 => bad.blocks[0].fields[1].bit = 0,
            2 => bad.blocks[0].fields[1].offset = 16,
            3 => bad.blocks[0].fields[2].bit = 8,
            4 => bad.blocks[0].fields[2].offset = 64,
            5 => bad.blocks[0].size = 1,
            _ => bad.output_size = 49,
        }
        assert!(bad.validate(&runtime.manifest).is_err());
    }
}

#[test]
fn alias_access_and_ambiguous_alias_rejection() {
    let (runtime, mut map) = fixture("(i32.const 0)");
    map.blocks[0].fields[2].aliases.push("Control".into());
    map.blocks[0].fields[3].aliases.push("Collision".into());
    map.blocks[0].fields[4].aliases.push("Collision".into());
    let mut bridge = Bridge::new(runtime, map).unwrap();
    bridge.queue_named("control", &[0, 9]).unwrap();
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_named("Signed").unwrap(), vec![0, 9]);
    assert!(bridge.read_named("Collision").is_err());
}

#[test]
fn all_scalar_widths_round_trip_without_changing_adjacent_bytes() {
    let (runtime, map) = fixture("(i32.const 0)");
    let mut bridge = Bridge::new(runtime, map).unwrap();
    let cases = [
        ("Byte", vec![0xA5]),
        ("Word", vec![0xAB, 0xCD]),
        ("Bits", vec![0xFE, 0xDC, 0xBA, 0x98]),
        ("Long", vec![0x7F, 0xFF, 0xFF, 0xFF]),
        ("Float", (-12.5f32).to_be_bytes().to_vec()),
        ("Text", vec![5, 5, 0xFF, b'a', 0, b'z', b'!']),
    ];
    for (name, data) in &cases {
        bridge.queue_named(name, data).unwrap();
    }
    // First byte would change a writable bit, but the second byte is padding.
    assert!(bridge.queue_bytes(17, 0, &[3, 1]).is_err());
    bridge.cycle(&[], 100).unwrap();
    assert_eq!(bridge.read_db(17, 0, 2).unwrap(), vec![1, 0]);
    for (name, data) in cases {
        assert_eq!(bridge.read_named(name).unwrap(), data);
    }
}
