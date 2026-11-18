//! Offline S7 DB serialization and cycle-boundary writes. No network listener.
use crate::{Manifest, Runtime};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Kind {
    Bool,
    Byte,
    Word,
    Dword,
    Int,
    Dint,
    Real,
    String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Field {
    pub name: String,
    /// Absolute byte offset in Wasm memory.
    pub offset: u32,
    /// Absolute bit offset in this S7 DB; bit zero is the least significant bit.
    pub bit: u32,
    pub kind: Kind,
    pub capacity: u8,
    pub writable: bool,
    #[serde(default)]
    pub aliases: Vec<String>,
}

impl Field {
    fn bytes(&self) -> usize {
        match self.kind {
            Kind::Bool | Kind::Byte => 1,
            Kind::Word | Kind::Int => 2,
            Kind::String => self.capacity as usize + 2,
            _ => 4,
        }
    }
    fn internal_bytes(&self) -> usize {
        if self.kind == Kind::String {
            (self.bytes() + 3) / 4 * 4
        } else {
            4
        }
    }
    fn bits(&self) -> usize {
        if self.kind == Kind::Bool {
            1
        } else {
            self.bytes() * 8
        }
    }
    fn encode(&self, state: &[u8], base: u32, db: &mut [u8]) -> Result<()> {
        let offset = (self.offset - base) as usize;
        let byte = self.bit as usize / 8;
        if self.kind == Kind::String {
            let value = &state[offset..offset + self.bytes()];
            ensure!(
                value[0] == self.capacity && value[1] <= self.capacity,
                "invalid internal STRING"
            );
            db[byte..byte + self.bytes()].copy_from_slice(value);
            return Ok(());
        }
        let raw = u32::from_le_bytes(state[offset..offset + 4].try_into()?);
        match self.kind {
            Kind::Bool => {
                ensure!(raw <= 1, "noncanonical internal BOOL");
                let mask = 1 << (self.bit % 8);
                db[byte] = (db[byte] & !mask) | if raw != 0 { mask } else { 0 };
            }
            Kind::Real => {
                ensure!(f32::from_bits(raw).is_finite(), "non-finite REAL");
                db[byte..byte + 4].copy_from_slice(&raw.to_be_bytes());
            }
            _ => {
                let n = self.bytes();
                db[byte..byte + n].copy_from_slice(&raw.to_be_bytes()[4 - n..]);
            }
        }
        Ok(())
    }
    fn decode(&self, db: &[u8]) -> Result<Vec<u8>> {
        let byte = self.bit as usize / 8;
        let data = &db[byte..byte + self.bytes()];
        if self.kind == Kind::String {
            ensure!(
                data[0] == self.capacity && data[1] <= self.capacity,
                "invalid S7 STRING header"
            );
            let mut out = vec![0; self.internal_bytes()];
            out[..data.len()].copy_from_slice(data);
            return Ok(out);
        }
        let raw = match self.kind {
            Kind::Bool => ((data[0] >> (self.bit % 8)) & 1) as u32,
            Kind::Byte => data[0] as u32,
            Kind::Word => u16::from_be_bytes(data.try_into()?) as u32,
            Kind::Int => i16::from_be_bytes(data.try_into()?) as i32 as u32,
            _ => u32::from_be_bytes(data.try_into()?),
        };
        if self.kind == Kind::Real {
            ensure!(f32::from_bits(raw).is_finite(), "non-finite S7 REAL");
        }
        Ok(raw.to_le_bytes().to_vec())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Block {
    pub number: u16,
    pub size: u32,
    pub fields: Vec<Field>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Map {
    pub version: u32,
    pub module_sha256: String,
    pub output_offset: u32,
    pub output_size: u32,
    pub blocks: Vec<Block>,
    /// Named process signals stored in independent Wasm scalar slots (bit must be zero).
    #[serde(default)]
    pub signals: Vec<Field>,
}

impl Map {
    pub fn validate(&self, manifest: &Manifest) -> Result<()> {
        manifest.validate()?;
        ensure!(
            self.version == 1 && !self.blocks.is_empty(),
            "invalid S7 map version or empty map"
        );
        ensure!(
            self.module_sha256.len() == 64 && self.module_sha256 == manifest.module_sha256,
            "S7 map belongs to another Wasm module"
        );
        ensure!(
            self.output_offset == manifest.outputs.offset
                && self.output_size == manifest.outputs.size,
            "S7 map output region mismatch"
        );
        let mut internal = vec![false; self.output_size as usize];
        let mut numbers = HashSet::new();
        let mut names = HashSet::new();
        let mut count = 0usize;
        let mut total_size = 0usize;
        for (number, size, fields) in self
            .blocks
            .iter()
            .map(|b| (Some(b.number), b.size, b.fields.as_slice()))
            .chain(
                self.signals
                    .iter()
                    .map(|f| (None, 4, std::slice::from_ref(f))),
            )
        {
            if let Some(number) = number {
                ensure!(
                    number != 0 && numbers.insert(number),
                    "invalid or duplicate DB number"
                );
            } else {
                ensure!(
                    fields[0].bit == 0 && fields[0].kind != Kind::String,
                    "process signal must be a scalar at bit zero"
                );
            }
            ensure!((1..=65536).contains(&size), "invalid DB size");
            total_size += size as usize;
            ensure!(total_size <= 16 * 1024 * 1024, "S7 snapshot too large");
            let mut occupied = vec![false; size as usize * 8];
            for field in fields {
                count += 1;
                ensure!(count <= 1_000_000, "too many S7 fields");
                ensure!(
                    !field.name.is_empty()
                        && field.name.len() <= 1024
                        && names.insert(field.name.to_ascii_uppercase()),
                    "invalid or duplicate S7 field name"
                );
                ensure!(
                    field.aliases.len() <= 16
                        && field
                            .aliases
                            .iter()
                            .all(|s| !s.is_empty() && s.len() <= 1024),
                    "invalid aliases"
                );
                ensure!(
                    field.capacity <= 254 && (field.kind == Kind::String || field.capacity == 0),
                    "invalid capacity"
                );
                ensure!(
                    field.kind == Kind::Bool || field.bit % 8 == 0,
                    "non-BOOL field must be byte aligned"
                );
                if !matches!(field.kind, Kind::Bool | Kind::Byte) {
                    ensure!(field.bit % 16 == 0, "S7 scalar must be word aligned");
                }
                let start = field.bit as usize;
                let end = start
                    .checked_add(field.bits())
                    .context("S7 offset overflow")?;
                ensure!(end <= occupied.len(), "field outside DB");
                ensure!(
                    !occupied[start..end].iter().any(|v| *v),
                    "overlapping S7 fields"
                );
                occupied[start..end].fill(true);
                let offset = field
                    .offset
                    .checked_sub(self.output_offset)
                    .context("field before state")? as usize;
                let end = offset
                    .checked_add(field.internal_bytes())
                    .context("Wasm offset overflow")?;
                ensure!(
                    field.offset % 4 == 0 && end <= internal.len(),
                    "field outside or unaligned in state"
                );
                ensure!(
                    !internal[offset..end].iter().any(|v| *v),
                    "overlapping internal fields"
                );
                internal[offset..end].fill(true);
            }
        }
        Ok(())
    }
    fn snapshot(&self, state: &[u8]) -> Result<BTreeMap<u16, Vec<u8>>> {
        ensure!(state.len() == self.output_size as usize, "wrong state size");
        let mut result = BTreeMap::new();
        for block in &self.blocks {
            let mut data = vec![0; block.size as usize];
            for field in &block.fields {
                field.encode(state, self.output_offset, &mut data)?;
            }
            result.insert(block.number, data);
        }
        Ok(result)
    }
    fn signal_snapshot(&self, state: &[u8]) -> Result<BTreeMap<u32, Vec<u8>>> {
        self.signals
            .iter()
            .map(|field| {
                let mut bytes = vec![0; field.bytes()];
                field.encode(state, self.output_offset, &mut bytes)?;
                Ok((field.offset, bytes))
            })
            .collect()
    }
}

/// Owns the VM and immutable published DB images. Network adapters must copy reads.
pub struct Bridge {
    runtime: Runtime,
    map: Map,
    published: BTreeMap<u16, Vec<u8>>,
    pending: BTreeMap<u16, Vec<u8>>,
    faulted: bool,
    generation: u64,
    names: HashMap<String, Option<(usize, usize)>>,
    exported_names: BTreeSet<String>,
    device_pending: BTreeMap<u32, Vec<u8>>,
    published_signals: BTreeMap<u32, Vec<u8>>,
    signal_pending: BTreeMap<u32, Vec<u8>>,
}

impl Bridge {
    pub fn new(runtime: Runtime, map: Map) -> Result<Self> {
        map.validate(&runtime.manifest)?;
        ensure!(!runtime.is_faulted(), "cannot attach faulted runtime");
        let published = map.snapshot(runtime.outputs())?;
        let published_signals = map.signal_snapshot(runtime.outputs())?;
        let mut names = HashMap::new();
        for (block_index, block) in map.blocks.iter().enumerate() {
            for (field_index, field) in block.fields.iter().enumerate() {
                let location = Some((block_index, field_index));
                for name in std::iter::once(&field.name).chain(field.aliases.iter()) {
                    names
                        .entry(name.to_ascii_uppercase())
                        .and_modify(|old| {
                            if *old != location {
                                *old = None;
                            }
                        })
                        .or_insert(location);
                }
            }
        }
        for (index, field) in map.signals.iter().enumerate() {
            let location = Some((map.blocks.len(), index));
            for name in std::iter::once(&field.name).chain(field.aliases.iter()) {
                names
                    .entry(name.to_ascii_uppercase())
                    .and_modify(|old| {
                        if *old != location {
                            *old = None;
                        }
                    })
                    .or_insert(location);
            }
        }
        let mut exported_names = BTreeSet::new();
        let ambiguous: HashSet<_> = names
            .iter()
            .filter(|(_, loc)| loc.is_none())
            .map(|(name, _)| name.clone())
            .collect();
        for (bi, fields) in map
            .blocks
            .iter()
            .map(|block| &block.fields)
            .chain(std::iter::once(&map.signals))
            .enumerate()
        {
            for (fi, field) in fields.iter().enumerate() {
                for alias in &field.aliases {
                    if !ambiguous.contains(&alias.to_ascii_uppercase()) {
                        exported_names.insert(alias.clone());
                        continue;
                    }
                    let name = if let Some(block) = map.blocks.get(bi) {
                        let byte = field.bit / 8;
                        let address = match field.kind {
                            Kind::Bool => format!("X{byte}.{}", field.bit % 8),
                            Kind::Word | Kind::Int => format!("W{byte}"),
                            Kind::Real | Kind::Dword | Kind::Dint => format!("D{byte}"),
                            Kind::Byte | Kind::String => format!("B{byte}"),
                        };
                        format!("{alias}@DB{}.DB{address}", block.number)
                    } else {
                        field.name.clone()
                    };
                    let location = Some((bi, fi));
                    ensure!(
                        *names.entry(name.to_ascii_uppercase()).or_insert(location) == location,
                        "ambiguous qualified tag {name}"
                    );
                    exported_names.insert(name);
                }
            }
        }
        Ok(Self {
            runtime,
            map,
            published,
            pending: BTreeMap::new(),
            faulted: false,
            generation: 0,
            names,
            exported_names,
            device_pending: BTreeMap::new(),
            published_signals,
            signal_pending: BTreeMap::new(),
        })
    }
    pub fn map(&self) -> &Map {
        &self.map
    }
    pub fn exported_names(&self) -> &BTreeSet<String> {
        &self.exported_names
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
    pub fn is_faulted(&self) -> bool {
        self.faulted || self.runtime.is_faulted()
    }
    pub fn read_db(&self, db: u16, offset: usize, length: usize) -> Result<Vec<u8>> {
        let data = self.published.get(&db).context("unknown DB")?;
        let end = offset.checked_add(length).context("read overflow")?;
        Ok(data.get(offset..end).context("read outside DB")?.to_vec())
    }
    pub fn read_bit(&self, db: u16, byte: usize, bit: u8) -> Result<bool> {
        ensure!(bit < 8, "invalid bit");
        Ok(self.read_db(db, byte, 1)?[0] & (1 << bit) != 0)
    }
    pub fn named(&self, name: &str) -> Result<(u16, Field)> {
        let &(block_index, field_index) = self
            .names
            .get(&name.to_ascii_uppercase())
            .context("unknown field name")?
            .as_ref()
            .context("ambiguous tag alias; use complete field path")?;
        if block_index == self.map.blocks.len() {
            return Ok((0, self.map.signals[field_index].clone()));
        }
        let block = &self.map.blocks[block_index];
        Ok((block.number, block.fields[field_index].clone()))
    }
    /// Returns S7 bytes, or one canonical 0/1 byte for a BOOL.
    pub fn read_named(&self, name: &str) -> Result<Vec<u8>> {
        let (db, field) = self.named(name)?;
        if db == 0 {
            return Ok(self.published_signals[&field.offset].clone());
        }
        if field.kind == Kind::Bool {
            return Ok(vec![
                self.read_bit(db, field.bit as usize / 8, (field.bit % 8) as u8)? as u8,
            ]);
        }
        self.read_db(db, field.bit as usize / 8, field.bytes())
    }
    pub fn queue_named(&mut self, name: &str, data: &[u8]) -> Result<()> {
        ensure!(!self.is_faulted(), "bridge is faulted");
        let (db, field) = self.named(name)?;
        ensure!(field.writable, "field is read-only");
        ensure!(data.len() == field.bytes(), "wrong field size");
        if db == 0 {
            ensure!(field.kind != Kind::Bool || data[0] <= 1, "invalid BOOL");
            self.signal_pending
                .insert(field.offset, field.decode(data)?);
            return Ok(());
        }
        if field.kind == Kind::Bool {
            ensure!(data[0] <= 1, "BOOL must be 0 or 1");
            return self.queue_bit(
                db,
                field.bit as usize / 8,
                (field.bit % 8) as u8,
                data[0] != 0,
            );
        }
        self.queue_bytes(db, field.bit as usize / 8, data)
    }
    pub fn queue_bytes(&mut self, db: u16, offset: usize, data: &[u8]) -> Result<()> {
        self.queue(db, offset, data, None)
    }
    pub fn queue_bit(&mut self, db: u16, byte: usize, bit: u8, value: bool) -> Result<()> {
        ensure!(bit < 8, "invalid bit");
        self.queue(
            db,
            byte,
            &[if value { 1 << bit } else { 0 }],
            Some(1 << bit),
        )
    }
    fn queue(&mut self, db: u16, offset: usize, data: &[u8], bit_mask: Option<u8>) -> Result<()> {
        ensure!(!self.is_faulted(), "bridge is faulted; reload required");
        ensure!(!data.is_empty(), "empty write");
        let block = self
            .map
            .blocks
            .iter()
            .find(|b| b.number == db)
            .context("unknown DB")?;
        let mut candidate = self
            .pending
            .get(&db)
            .unwrap_or(&self.published[&db])
            .clone();
        let end = offset.checked_add(data.len()).context("write overflow")?;
        ensure!(end <= candidate.len(), "write outside DB");
        let mut allowed = vec![0u8; candidate.len()];
        for field in &block.fields {
            if field.writable {
                for bit in field.bit as usize..field.bit as usize + field.bits() {
                    allowed[bit / 8] |= 1 << (bit % 8);
                }
            }
        }
        for (i, value) in data.iter().enumerate() {
            let index = offset + i;
            let mask = bit_mask.unwrap_or(255);
            ensure!(
                allowed[index] & mask != 0,
                "write targets padding or read-only data"
            );
            ensure!(
                (candidate[index] ^ value) & mask & !allowed[index] == 0,
                "write changes read-only bits"
            );
            candidate[index] = (candidate[index] & !mask) | (value & mask);
        }
        // Decode before accepting any part of this request; malformed STRING/REAL is atomic failure.
        for field in &block.fields {
            if field.writable {
                field.decode(&candidate)?;
            }
        }
        self.pending.insert(db, candidate);
        Ok(())
    }
    pub fn discard_pending(&mut self) {
        self.pending.clear();
        self.device_pending.clear();
        self.signal_pending.clear();
    }
    /// Explicitly configured field inputs, independent of HMI write permissions.
    /// A failed batch changes nothing. Device input patches take priority over HMI writes.
    pub fn queue_device_inputs(&mut self, values: &[(String, Vec<u8>)]) -> Result<()> {
        ensure!(!self.is_faulted(), "bridge is faulted");
        let mut patches = BTreeMap::new();
        for (name, value) in values {
            let (db, field) = self.named(name)?;
            ensure!(value.len() == field.bytes(), "wrong device field size");
            let mut snapshot = if db == 0 {
                self.published_signals[&field.offset].clone()
            } else {
                self.published[&db].clone()
            };
            let byte = field.bit as usize / 8;
            if field.kind == Kind::Bool {
                ensure!(value[0] <= 1, "invalid device BOOL");
                let mask = 1 << (field.bit % 8);
                snapshot[byte] = (snapshot[byte] & !mask) | (value[0] << (field.bit % 8));
            } else {
                snapshot[byte..byte + value.len()].copy_from_slice(value);
            }
            ensure!(
                patches
                    .insert(field.offset, field.decode(&snapshot)?)
                    .is_none(),
                "duplicate device target"
            );
        }
        self.device_pending.extend(patches);
        Ok(())
    }
    pub fn cycle(&mut self, inputs: &[u8], elapsed_ms: u32) -> Result<()> {
        ensure!(!self.is_faulted(), "bridge is faulted; reload required");
        ensure!(
            inputs.len() == self.runtime.manifest.inputs.size as usize,
            "wrong input size"
        );
        ensure!(
            elapsed_ms > 0 && elapsed_ms <= i32::MAX as u32,
            "invalid elapsed time"
        );
        let mut patches = Vec::new();
        for block in &self.map.blocks {
            if let Some(data) = self.pending.get(&block.number) {
                for field in &block.fields {
                    if field.writable {
                        let value = field.decode(data)?;
                        if value != field.decode(&self.published[&block.number])? {
                            patches.push((field.offset, value));
                        }
                    }
                }
            }
        }
        patches.extend(
            self.signal_pending
                .iter()
                .map(|(offset, value)| (*offset, value.clone())),
        );
        patches.extend(
            self.device_pending
                .iter()
                .map(|(offset, value)| (*offset, value.clone())),
        );
        self.runtime.patch_state(&patches)?;
        self.pending.clear();
        self.device_pending.clear();
        self.signal_pending.clear();
        if let Err(error) = self.runtime.cycle(inputs, elapsed_ms) {
            self.faulted = true;
            return Err(error);
        }
        match self.map.snapshot(self.runtime.outputs()).and_then(|dbs| {
            self.map
                .signal_snapshot(self.runtime.outputs())
                .map(|signals| (dbs, signals))
        }) {
            Ok((snapshot, signals)) => {
                self.published = snapshot;
                self.published_signals = signals;
                self.generation += 1;
                Ok(())
            }
            Err(error) => {
                self.faulted = true;
                Err(error)
            }
        }
    }
}
