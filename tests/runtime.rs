use station_runtime::{Manifest, Package, Region, Runtime};

fn manifest() -> Manifest {
    Manifest {
        abi_version: 1,
        name: "test".into(),
        kind: "cycle-smoke".into(),
        memory_bytes: 65536,
        fuel_per_call: 1000,
        inputs: Region { offset: 0, size: 4 },
        outputs: Region { offset: 4, size: 4 },
        fields: vec![],
        source_sha256: String::new(),
        module_sha256: String::new(),
    }
}

fn module(scan: &str) -> Vec<u8> {
    wat::parse_str(format!(
        r#"(module
      (memory (export "memory") 1 1)
      (func (export "abi_version") (result i32) i32.const 1)
      (func (export "init") (result i32)
        (i32.store (i32.const 4) (i32.const 7)) (i32.const 0))
      (func (export "scan") (param i32) (result i32) {scan})
    )"#
    ))
    .unwrap()
}

fn load(scan: &str) -> Runtime {
    let bytes = Package::encode(manifest(), module(scan)).unwrap();
    Runtime::load(Package::decode(&bytes).unwrap()).unwrap()
}

#[test]
fn input_snapshot_persistent_state_and_elapsed_time() {
    let mut runtime = load(
        r#"
      (i32.store (i32.const 4)
        (i32.add (i32.load (i32.const 4))
          (i32.add (i32.load (i32.const 0)) (local.get 0))))
      (i32.const 0)"#,
    );
    assert_eq!(runtime.outputs(), &7u32.to_le_bytes());
    assert_eq!(
        runtime.cycle(&3u32.to_le_bytes(), 100).unwrap(),
        &110u32.to_le_bytes()
    );
    assert_eq!(
        runtime.cycle(&2u32.to_le_bytes(), 50).unwrap(),
        &162u32.to_le_bytes()
    );
}

#[test]
fn fuel_exhaustion_latches_fault_without_publishing_partial_output() {
    let mut runtime = load(
        r#"
      (i32.store (i32.const 4) (i32.const 999))
      (loop $forever (br $forever)) (i32.const 0)"#,
    );
    assert!(runtime.cycle(&[0; 4], 100).is_err());
    assert!(runtime.is_faulted());
    assert_eq!(runtime.outputs(), &7u32.to_le_bytes());
    assert!(runtime.cycle(&[0; 4], 100).is_err());
}

#[test]
fn error_status_and_oob_trap_discard_partial_output() {
    for body in [
        "(i32.store (i32.const 4) (i32.const 999)) (i32.const 5)",
        "(i32.store (i32.const 4) (i32.const 999)) (i32.load (i32.const 65536))",
    ] {
        let mut runtime = load(body);
        assert!(runtime.cycle(&[0; 4], 100).is_err());
        assert_eq!(runtime.outputs(), &7u32.to_le_bytes());
        assert!(runtime.is_faulted());
    }
}

#[test]
fn bad_inputs_do_not_execute_the_program() {
    let mut runtime = load("i32.const 0");
    assert!(runtime.cycle(&[], 100).is_err());
    assert!(runtime.cycle(&[0; 4], 0).is_err());
    assert_eq!(runtime.outputs(), &7u32.to_le_bytes());
    assert!(!runtime.is_faulted());
}

#[test]
fn package_integrity_lengths_and_abi_are_validated() {
    let good = Package::encode(manifest(), module("i32.const 0")).unwrap();
    for length in [0, 4, 11, good.len() - 1] {
        assert!(Package::decode(&good[..length]).is_err());
    }
    let mut corrupt = good.clone();
    *corrupt.last_mut().unwrap() ^= 1;
    assert!(Package::decode(&corrupt).is_err());
    let mut trailing = good;
    trailing.push(0);
    assert!(Package::decode(&trailing).is_err());
    let mut unsupported = manifest();
    unsupported.abi_version = 2;
    assert!(Package::encode(unsupported, module("i32.const 0")).is_err());
    let mut overlapping = manifest();
    overlapping.outputs.offset = 0;
    assert!(Package::encode(overlapping, module("i32.const 0")).is_err());
}

#[test]
fn invalid_abi_imports_and_initialization_are_rejected() {
    let cases = [
        r#"(module (import "wasi_snapshot_preview1" "proc_exit" (func (param i32))))"#,
        r#"(module (memory (export "memory") 2))"#,
        r#"(module (memory (export "memory") 1)
          (func (export "abi_version") (result i32) i32.const 2))"#,
        r#"(module (memory (export "memory") 1)
          (func (export "abi_version") (result i32) i32.const 1)
          (func (export "scan") (param i32) (result i32) i32.const 0)
          (func (export "init") (result i32) (loop $l (br $l)) i32.const 0))"#,
    ];
    for source in cases {
        let bytes = Package::encode(manifest(), wat::parse_str(source).unwrap()).unwrap();
        assert!(Runtime::load(Package::decode(&bytes).unwrap()).is_err());
    }
}
