//! Dynamically loaded Snap7 resourceless server. The VM never exposes pointers.
use crate::s7::Bridge;
use anyhow::{Context, Result, ensure};
use libloading::Library;
use serde::Serialize;
use std::ffi::{c_char, c_int, c_void};
use std::marker::PhantomData;
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::Path;
use std::rc::Rc;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};

#[repr(C)]
#[derive(Clone, Copy)]
struct Tag {
    area: c_int,
    db: c_int,
    start: c_int,
    amount: c_int,
    word_len: c_int,
}

type Callback =
    unsafe extern "system" fn(*mut c_void, c_int, c_int, *mut Tag, *mut c_void) -> c_int;
type Create = unsafe extern "system" fn() -> usize;
type Destroy = unsafe extern "system" fn(*mut usize);
type Stop = unsafe extern "system" fn(usize) -> c_int;
type StartTo = unsafe extern "system" fn(usize, *const c_char) -> c_int;
type SetParam = unsafe extern "system" fn(usize, c_int, *mut c_void) -> c_int;
type SetMask = unsafe extern "system" fn(usize, c_int, u32) -> c_int;
type SetCallback = unsafe extern "system" fn(usize, Callback, *mut c_void) -> c_int;
type GetStatus = unsafe extern "system" fn(usize, *mut c_int, *mut c_int, *mut c_int) -> c_int;

struct Api {
    create: Create,
    destroy: Destroy,
    stop: Stop,
    start: StartTo,
    parameter: SetParam,
    mask: SetMask,
    callback: SetCallback,
    status: GetStatus,
    _library: Library,
}

impl Api {
    fn load(path: &Path) -> Result<Self> {
        // Only an explicit, existing native library is loaded; no PATH search.
        let path = path.canonicalize().context("Snap7 library path")?;
        // SAFETY: caller selects a trusted Snap7 library implementing its stable C ABI.
        unsafe {
            let library = Library::new(path)?;
            Ok(Self {
                create: *library.get(b"Srv_Create\0")?,
                destroy: *library.get(b"Srv_Destroy\0")?,
                stop: *library.get(b"Srv_Stop\0")?,
                start: *library.get(b"Srv_StartTo\0")?,
                parameter: *library.get(b"Srv_SetParam\0")?,
                mask: *library.get(b"Srv_SetMask\0")?,
                callback: *library.get(b"Srv_SetRWAreaCallback\0")?,
                status: *library.get(b"Srv_GetStatus\0")?,
                _library: library,
            })
        }
    }
}

struct ContextState {
    bridge: Arc<Mutex<Bridge>>,
    closed: AtomicBool,
    reads: AtomicU64,
    writes: AtomicU64,
    rejected: AtomicU64,
}

fn request_shape(tag: Tag) -> Result<(u16, usize, usize, bool)> {
    ensure!(tag.area == 0x84, "only DB area is supported");
    ensure!(
        (1..=65535).contains(&tag.db) && tag.start >= 0 && tag.amount > 0,
        "invalid tag"
    );
    let bit = tag.word_len == 1;
    let width = match tag.word_len {
        1..=3 => 1usize,
        4 | 5 => 2,
        6..=8 => 4,
        _ => 0,
    };
    ensure!(
        width != 0 && (!bit || tag.amount == 1),
        "unsupported word length or bit count"
    );
    let bytes = (tag.amount as usize)
        .checked_mul(width)
        .context("request overflow")?;
    ensure!(bytes <= 960, "request exceeds PDU limit");
    let start = if bit {
        tag.start as usize / 8
    } else {
        tag.start as usize
    };
    ensure!(
        start.checked_add(bytes).is_some_and(|end| end <= 65536),
        "request outside DB limits"
    );
    Ok((tag.db as u16, start, bytes, bit))
}

fn exchange(bridge: &mut Bridge, operation: c_int, tag: Tag, data: &mut [u8]) -> Result<()> {
    ensure!(
        !bridge.is_faulted(),
        "faulted state must not be reported as fresh"
    );
    let (db, start, bytes, bit) = request_shape(tag)?;
    ensure!(data.len() == bytes, "wrong callback buffer length");
    match operation {
        0 => {
            // Snap7 masks and shifts a bit READ after the callback. Return the whole byte!
            data.copy_from_slice(&bridge.read_db(db, start, bytes)?);
            Ok(())
        }
        1 if bit => {
            ensure!(data[0] <= 1, "noncanonical bit write");
            bridge.queue_bit(db, start, (tag.start % 8) as u8, data[0] != 0)
        }
        1 => bridge.queue_bytes(db, start, data),
        _ => anyhow::bail!("unsupported callback operation"),
    }
}

unsafe extern "system" fn rw_callback(
    user: *mut c_void,
    _sender: c_int,
    operation: c_int,
    tag: *mut Tag,
    data: *mut c_void,
) -> c_int {
    if user.is_null() || tag.is_null() || data.is_null() {
        return 1;
    }
    // SAFETY: the boxed context lives until after Srv_Stop and Srv_Destroy finish.
    let context = unsafe { &*(user.cast::<ContextState>()) };
    let result = catch_unwind(AssertUnwindSafe(|| -> Result<()> {
        ensure!(!context.closed.load(Ordering::Acquire), "server is closing");
        // Snap7 may pack the native struct; no Rust reference to it is retained.
        let tag = unsafe { tag.read_unaligned() };
        let (_, _, bytes, _) = request_shape(tag)?;
        ensure!(matches!(operation, 0 | 1), "invalid operation");
        // SAFETY: Snap7's synchronous callback provides amount * word-size bytes.
        // It owns this buffer; no reference or pointer escapes this function.
        let data = unsafe { std::slice::from_raw_parts_mut(data.cast::<u8>(), bytes) };
        let mut bridge = context
            .bridge
            .lock()
            .map_err(|_| anyhow::anyhow!("bridge lock poisoned"))?;
        exchange(&mut bridge, operation, tag, data)
    }));
    if matches!(result, Ok(Ok(()))) {
        if operation == 0 {
            context.reads.fetch_add(1, Ordering::Relaxed);
        } else {
            context.writes.fetch_add(1, Ordering::Relaxed);
        }
        0
    } else {
        context.rejected.fetch_add(1, Ordering::Relaxed);
        1 // Snap7 maps a rejected callback to an unsuccessful S7 item result.
    }
}

#[derive(Debug, Serialize)]
pub struct Status {
    pub server: i32,
    pub cpu: i32,
    pub clients: i32,
    pub reads: u64,
    pub writes: u64,
    pub rejected: u64,
}

/// Native server lifetime is tied to its creating thread and callback context.
pub struct Server {
    api: Api,
    handle: usize,
    context: Box<ContextState>,
    _thread_bound: PhantomData<Rc<()>>,
}

impl Server {
    /// The initial integration deliberately only listens on loopback, never port 102.
    pub fn start(library: &Path, port: u16, bridge: Arc<Mutex<Bridge>>) -> Result<Self> {
        fn require_send_sync<T: Send + Sync>() {}
        require_send_sync::<ContextState>();
        ensure!(
            port >= 1024,
            "test listener requires an unprivileged port (>=1024)"
        );
        // Snap7 enables SO_REUSEADDR, which can allow duplicate listeners on Windows.
        // Fail early for an occupied endpoint. Its API has no socket handoff, so a
        // cross-process race between releasing this reservation and native bind remains.
        let reservation = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port))
            .context("Snap7 loopback port is unavailable")?;
        let api = Api::load(library)?;
        let handle = unsafe { (api.create)() };
        ensure!(handle != 0, "Snap7 server allocation failed");
        let mut server = Self {
            api,
            handle,
            context: Box::new(ContextState {
                bridge,
                closed: AtomicBool::new(false),
                reads: AtomicU64::new(0),
                writes: AtomicU64::new(0),
                rejected: AtomicU64::new(0),
            }),
            _thread_bound: PhantomData,
        };
        let mut port = port;
        let mut clients = 8i32;
        let mut pdu = 480i32;
        // SAFETY: native object and boxed context remain stable throughout server lifetime.
        unsafe {
            check(
                (server.api.parameter)(handle, 1, (&mut port as *mut u16).cast()),
                "local port",
            )?;
            check(
                (server.api.parameter)(handle, 11, (&mut clients as *mut i32).cast()),
                "client limit",
            )?;
            check(
                (server.api.parameter)(handle, 10, (&mut pdu as *mut i32).cast()),
                "PDU size",
            )?;
            // Avoid an unconsumed native event log growing during continuous operation.
            check((server.api.mask)(handle, 0, 0), "event mask")?;
            check((server.api.mask)(handle, 1, 0), "log mask")?;
            check(
                (server.api.callback)(
                    handle,
                    rw_callback,
                    (&mut *server.context as *mut ContextState).cast(),
                ),
                "RW callback",
            )?;
            drop(reservation);
            check((server.api.start)(handle, c"127.0.0.1".as_ptr()), "listen")?;
        }
        Ok(server)
    }
    pub fn status(&self) -> Result<Status> {
        let (mut server, mut cpu, mut clients) = (0, 0, 0);
        check(
            unsafe { (self.api.status)(self.handle, &mut server, &mut cpu, &mut clients) },
            "status",
        )?;
        Ok(Status {
            server,
            cpu,
            clients,
            reads: self.context.reads.load(Ordering::Relaxed),
            writes: self.context.writes.load(Ordering::Relaxed),
            rejected: self.context.rejected.load(Ordering::Relaxed),
        })
    }
}

fn check(result: c_int, operation: &str) -> Result<()> {
    ensure!(result == 0, "Snap7 {operation} failed: 0x{result:08x}");
    Ok(())
}

impl Drop for Server {
    fn drop(&mut self) {
        self.context.closed.store(true, Ordering::Release);
        // No bridge lock is held here. Stop joins native workers before context/library release.
        unsafe {
            (self.api.stop)(self.handle);
            (self.api.destroy)(&mut self.handle);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn callback_shape_checks_and_units() {
        let tag = Tag {
            area: 0x84,
            db: 9,
            start: 23,
            amount: 1,
            word_len: 1,
        };
        assert_eq!(request_shape(tag).unwrap(), (9, 2, 1, true));
        assert_eq!(
            request_shape(Tag {
                start: 4,
                amount: 3,
                word_len: 4,
                ..tag
            })
            .unwrap(),
            (9, 4, 6, false)
        );
        for bad in [
            Tag { area: 0x83, ..tag },
            Tag { amount: 2, ..tag },
            Tag { amount: -1, ..tag },
            Tag { start: -1, ..tag },
            Tag { db: 0, ..tag },
            Tag { word_len: 0, ..tag },
            Tag {
                word_len: 2,
                amount: 961,
                ..tag
            },
            Tag {
                word_len: 4,
                start: 65535,
                ..tag
            },
        ] {
            assert!(request_shape(bad).is_err());
        }
    }
}
