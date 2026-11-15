//! Human-readable status for long-running WPLC modes.
use std::io::{self, IsTerminal, Write};

pub struct Dashboard {
    interactive: bool,
}

impl Dashboard {
    pub fn new() -> Self {
        Self {
            interactive: io::stdout().is_terminal() && enable_ansi(),
        }
    }

    pub fn refresh_interval(&self) -> std::time::Duration {
        std::time::Duration::from_secs(if self.interactive { 1 } else { 10 })
    }

    pub fn draw(&self, title: &str, sections: &[(&str, Vec<(String, String)>)]) -> io::Result<()> {
        let mut out = io::stdout().lock();
        if self.interactive {
            write!(out, "\x1b[H\x1b[2J")?;
        }
        writeln!(out, "WPLC  {title}")?;
        writeln!(out, "{:-<76}", "")?;
        for (heading, rows) in sections {
            writeln!(out, "{heading}")?;
            for (label, value) in rows {
                writeln!(out, "  {:<22} {}", shorten(label, 19), shorten(value, 46))?;
            }
            writeln!(out)?;
        }
        if self.interactive {
            writeln!(out, "Ctrl+C: stop WPLC")?;
        }
        out.flush()
    }
}

impl Default for Dashboard {
    fn default() -> Self {
        Self::new()
    }
}

fn shorten(text: &str, limit: usize) -> String {
    let mut chars = text.chars();
    let prefix: String = chars.by_ref().take(limit).collect();
    if chars.next().is_some() {
        format!("{prefix}...")
    } else {
        prefix
    }
}

pub fn bytes(bytes: u64) -> String {
    format!("{:.1} MiB", bytes as f64 / 1_048_576.0)
}

#[cfg(windows)]
fn enable_ansi() -> bool {
    use std::ffi::c_void;
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Coord {
        x: i16,
        y: i16,
    }
    #[repr(C)]
    #[derive(Clone, Copy)]
    struct Rect {
        left: i16,
        top: i16,
        right: i16,
        bottom: i16,
    }
    #[repr(C)]
    struct ConsoleInfo {
        size: Coord,
        cursor: Coord,
        attributes: u16,
        window: Rect,
        maximum_window_size: Coord,
    }
    unsafe extern "system" {
        fn GetStdHandle(kind: i32) -> *mut c_void;
        fn GetConsoleMode(handle: *mut c_void, mode: *mut u32) -> i32;
        fn SetConsoleMode(handle: *mut c_void, mode: u32) -> i32;
        fn GetConsoleScreenBufferInfo(handle: *mut c_void, info: *mut ConsoleInfo) -> i32;
        fn GetLargestConsoleWindowSize(handle: *mut c_void) -> Coord;
        fn SetConsoleScreenBufferSize(handle: *mut c_void, size: Coord) -> i32;
        fn SetConsoleWindowInfo(handle: *mut c_void, absolute: i32, rect: *const Rect) -> i32;
    }
    unsafe {
        let handle = GetStdHandle(-11);
        let mut mode = 0;
        let enabled = !handle.is_null()
            && GetConsoleMode(handle, &mut mode) != 0
            && SetConsoleMode(handle, mode | 0x0004) != 0;
        if enabled {
            let mut info = std::mem::zeroed::<ConsoleInfo>();
            if GetConsoleScreenBufferInfo(handle, &mut info) != 0 {
                let largest = GetLargestConsoleWindowSize(handle);
                let desired = 38.min(largest.y);
                if desired > info.window.bottom - info.window.top + 1 {
                    let size = Coord {
                        x: info.size.x,
                        y: info.size.y.max(desired),
                    };
                    if SetConsoleScreenBufferSize(handle, size) != 0 {
                        let window = Rect {
                            left: info.window.left,
                            top: 0,
                            right: info.window.right,
                            bottom: desired - 1,
                        };
                        let _ = SetConsoleWindowInfo(handle, 1, &window);
                    }
                }
            }
        }
        enabled
    }
}

#[cfg(not(windows))]
fn enable_ansi() -> bool {
    true
}

#[cfg(windows)]
pub fn resident_bytes() -> Option<u64> {
    use std::ffi::c_void;
    #[repr(C)]
    struct Counters {
        size: u32,
        page_faults: u32,
        peak_working_set: usize,
        working_set: usize,
        peak_paged_pool: usize,
        paged_pool: usize,
        peak_nonpaged_pool: usize,
        nonpaged_pool: usize,
        pagefile: usize,
        peak_pagefile: usize,
    }
    #[link(name = "psapi")]
    unsafe extern "system" {
        fn GetProcessMemoryInfo(process: *mut c_void, counters: *mut Counters, size: u32) -> i32;
    }
    unsafe extern "system" {
        fn GetCurrentProcess() -> *mut c_void;
    }
    let mut counters = Counters {
        size: std::mem::size_of::<Counters>() as u32,
        page_faults: 0,
        peak_working_set: 0,
        working_set: 0,
        peak_paged_pool: 0,
        paged_pool: 0,
        peak_nonpaged_pool: 0,
        nonpaged_pool: 0,
        pagefile: 0,
        peak_pagefile: 0,
    };
    let ok = unsafe { GetProcessMemoryInfo(GetCurrentProcess(), &mut counters, counters.size) };
    (ok != 0).then_some(counters.working_set as u64)
}

#[cfg(not(windows))]
pub fn resident_bytes() -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    let line = status.lines().find(|line| line.starts_with("VmRSS:"))?;
    line.split_whitespace()
        .nth(1)?
        .parse::<u64>()
        .ok()?
        .checked_mul(1024)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dashboard_values_remain_bounded() {
        assert_eq!(shorten("abc", 3), "abc");
        assert_eq!(shorten("abcdef", 3), "abc...");
        assert_eq!(bytes(1_048_576), "1.0 MiB");
    }
}
