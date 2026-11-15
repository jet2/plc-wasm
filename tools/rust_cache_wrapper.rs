//! Keep Cargo response files intact when sccache would expand them past Windows limits.
use std::{env, ffi::OsString, process::{Command, ExitCode}};

fn main() -> ExitCode {
    let args: Vec<OsString> = env::args_os().skip(1).collect();
    let Some(compiler) = args.first() else { return ExitCode::FAILURE };
    let response_file = args[1..].iter().any(|a| a.to_string_lossy().starts_with('@'));
    let cache = env::var_os("WPLC_SCCACHE").filter(|_| !response_file);
    let mut command = match cache {
        Some(cache) => { let mut c = Command::new(cache); c.args(&args); c }
        None => { let mut c = Command::new(compiler); c.args(&args[1..]); c }
    };
    match command.status() {
        Ok(status) => if status.success() { ExitCode::SUCCESS } else { ExitCode::FAILURE },
        Err(error) => { eprintln!("compiler wrapper: {error}"); ExitCode::FAILURE }
    }
}
