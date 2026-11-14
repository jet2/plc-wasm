//! Experimental ABI v1. No device I/O is enabled in this milestone.
use anyhow::{Context, Result, bail, ensure};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use wasmi::{
    Config, Engine, Linker, Memory, Module, Store, StoreLimits, StoreLimitsBuilder, TypedFunc,
};

pub const ABI_VERSION: u32 = 1;
const MAX_PACKAGE: usize = 32 * 1024 * 1024;
const MAX_MEMORY: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Region {
    pub offset: u32,
    pub size: u32,
}

impl Region {
    fn range(&self) -> Result<std::ops::Range<usize>> {
        let end = self
            .offset
            .checked_add(self.size)
            .context("region overflow")?;
        ensure!(
            end as usize <= MAX_MEMORY,
            "region exceeds host memory limit"
        );
        Ok(self.offset as usize..end as usize)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Field {
    pub name: String,
    pub offset: u32,
    pub scl_type: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub abi_version: u32,
    pub name: String,
    pub kind: String,
    pub memory_bytes: u32,
    pub fuel_per_call: u64,
    pub inputs: Region,
    pub outputs: Region,
    #[serde(default)]
    pub fields: Vec<Field>,
    #[serde(default)]
    pub source_sha256: String,
    #[serde(default)]
    pub module_sha256: String,
}

impl Manifest {
    pub fn validate(&self) -> Result<()> {
        ensure!(
            self.abi_version == ABI_VERSION,
            "unsupported package ABI {}",
            self.abi_version
        );
        ensure!(
            matches!(self.kind.as_str(), "initializer-smoke" | "cycle-smoke"),
            "unsupported program kind"
        );
        ensure!(
            self.memory_bytes > 0 && self.memory_bytes as usize <= MAX_MEMORY,
            "invalid memory limit"
        );
        ensure!(
            (1..=100_000_000).contains(&self.fuel_per_call),
            "invalid fuel budget"
        );
        let input = self.inputs.range()?;
        let output = self.outputs.range()?;
        ensure!(
            input.end <= self.memory_bytes as usize && output.end <= self.memory_bytes as usize,
            "region exceeds memory"
        );
        ensure!(
            input.is_empty()
                || output.is_empty()
                || input.end <= output.start
                || output.end <= input.start,
            "input/output regions overlap"
        );
        let mut names = std::collections::HashSet::new();
        let mut offsets = std::collections::HashSet::new();
        for field in &self.fields {
            ensure!(
                names.insert(field.name.to_ascii_lowercase()),
                "duplicate field name"
            );
            ensure!(
                offsets.insert(field.offset) && field.offset % 4 == 0,
                "overlapping or unaligned field"
            );
            ensure!(
                matches!(
                    field.scl_type.as_str(),
                    "BOOL" | "BYTE" | "WORD" | "DWORD" | "INT" | "DINT"
                ),
                "unsupported field type"
            );
            let end = field.offset.checked_add(4).context("field overflow")? as usize;
            ensure!(
                field.offset as usize >= output.start && end <= output.end,
                "field outside output snapshot"
            );
        }
        Ok(())
    }
}

pub struct Package {
    pub manifest: Manifest,
    pub wasm: Vec<u8>,
}

impl Package {
    pub fn encode(mut manifest: Manifest, wasm: Vec<u8>) -> Result<Vec<u8>> {
        manifest.validate()?;
        ensure!(
            wasm.starts_with(b"\0asm\x01\0\0\0"),
            "expected binary WebAssembly v1"
        );
        manifest.module_sha256 = format!("{:x}", Sha256::digest(&wasm));
        let json = serde_json::to_vec(&manifest)?;
        ensure!(
            json.len() <= 1024 * 1024 && json.len() + wasm.len() + 12 <= MAX_PACKAGE,
            "package too large"
        );
        let mut bytes = b"SWP1".to_vec();
        bytes.extend_from_slice(&(json.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&(wasm.len() as u32).to_le_bytes());
        bytes.extend(json);
        bytes.extend(wasm);
        Ok(bytes)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(
            bytes.len() >= 12 && bytes.len() <= MAX_PACKAGE,
            "invalid package size"
        );
        ensure!(&bytes[..4] == b"SWP1", "invalid package magic");
        let json_len = u32::from_le_bytes(bytes[4..8].try_into()?) as usize;
        let wasm_len = u32::from_le_bytes(bytes[8..12].try_into()?) as usize;
        ensure!(
            json_len <= 1024 * 1024 && wasm_len <= MAX_PACKAGE,
            "invalid section length"
        );
        let end = 12usize.checked_add(json_len).context("length overflow")?;
        ensure!(
            end.checked_add(wasm_len) == Some(bytes.len()),
            "truncated package or trailing data"
        );
        let manifest: Manifest = serde_json::from_slice(&bytes[12..end])?;
        manifest.validate()?;
        let wasm = bytes[end..].to_vec();
        ensure!(
            wasm.starts_with(b"\0asm\x01\0\0\0"),
            "expected binary WebAssembly v1"
        );
        ensure!(
            manifest.module_sha256 == format!("{:x}", Sha256::digest(&wasm)),
            "module checksum mismatch"
        );
        Ok(Self { manifest, wasm })
    }
}

/// All VM mutations are owned by this object. Only successful snapshots leave it.
pub struct Runtime {
    store: Store<StoreLimits>,
    memory: Memory,
    scan: TypedFunc<i32, i32>,
    pub manifest: Manifest,
    outputs: Vec<u8>,
    faulted: bool,
}

impl Runtime {
    pub fn load(package: Package) -> Result<Self> {
        package.manifest.validate()?;
        let manifest = package.manifest;
        let mut config = Config::default();
        config.consume_fuel(true);
        let engine = Engine::new(&config);
        let module = Module::new(&engine, &package.wasm)?;
        // No WASI, filesystem, sockets, host callbacks or imported memory in ABI v1.
        ensure!(
            module.imports().next().is_none(),
            "imports are not allowed in ABI v1"
        );
        let limits = StoreLimitsBuilder::new()
            .memory_size(manifest.memory_bytes as usize)
            .memories(1)
            .tables(0)
            .instances(1)
            .build();
        let mut store = Store::new(&engine, limits);
        store.limiter(|limits| limits);
        store.set_fuel(manifest.fuel_per_call)?;
        let instance =
            Linker::<StoreLimits>::new(&engine).instantiate_and_start(&mut store, &module)?;
        let memory = instance
            .get_memory(&store, "memory")
            .context("missing memory export")?;
        ensure!(
            memory.data(&store).len() == manifest.memory_bytes as usize,
            "memory size differs from manifest"
        );
        let abi = instance.get_typed_func::<(), i32>(&store, "abi_version")?;
        ensure!(
            abi.call(&mut store, ())? == ABI_VERSION as i32,
            "module ABI mismatch"
        );
        let init = instance.get_typed_func::<(), i32>(&store, "init")?;
        let scan = instance.get_typed_func::<i32, i32>(&store, "scan")?;
        store.set_fuel(manifest.fuel_per_call)?;
        ensure!(init.call(&mut store, ())? == 0, "init returned error");
        let outputs = memory.data(&store)[manifest.outputs.range()?].to_vec();
        Ok(Self {
            store,
            memory,
            scan,
            manifest,
            outputs,
            faulted: false,
        })
    }

    pub fn cycle(&mut self, inputs: &[u8], elapsed_ms: u32) -> Result<&[u8]> {
        ensure!(!self.faulted, "runtime is faulted; reload required");
        ensure!(
            inputs.len() == self.manifest.inputs.size as usize,
            "wrong input snapshot size"
        );
        ensure!(
            elapsed_ms > 0 && elapsed_ms <= i32::MAX as u32,
            "invalid elapsed time"
        );
        self.memory.write(
            &mut self.store,
            self.manifest.inputs.offset as usize,
            inputs,
        )?;
        self.store.set_fuel(self.manifest.fuel_per_call)?;
        match self.scan.call(&mut self.store, elapsed_ms as i32) {
            Ok(0) => {}
            Ok(code) => {
                self.faulted = true;
                bail!("scan returned error {code}; output snapshot not published");
            }
            Err(error) => {
                self.faulted = true;
                return Err(error).context("scan trapped; output snapshot not published");
            }
        }
        self.outputs
            .copy_from_slice(&self.memory.data(&self.store)[self.manifest.outputs.range()?]);
        Ok(&self.outputs)
    }

    pub fn outputs(&self) -> &[u8] {
        &self.outputs
    }
    pub fn is_faulted(&self) -> bool {
        self.faulted
    }
}
