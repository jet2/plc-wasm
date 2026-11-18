use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, BufRead},
    path::{Path, PathBuf},
    sync::mpsc,
    thread,
    time::Duration,
};

#[derive(Default, Clone, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Mode {
    #[default]
    Gateway,
    Serve,
    Monitor,
}

#[derive(Default, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Settings {
    package: Option<PathBuf>,
    mode: Mode,
    protocols: Option<PathBuf>,
    snap7: Option<PathBuf>,
    port: Option<u16>,
    period_ms: Option<u32>,
    ip: Option<std::net::Ipv4Addr>,
    rack: Option<u8>,
    slot: Option<u8>,
    cycles: u64,
    autostart: bool,
}

impl Settings {
    fn save(&self, path: &Path) -> Result<()> {
        let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
        fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        fs::rename(tmp, path)?;
        Ok(())
    }

    fn arguments(&self, base: &Path) -> Result<Vec<String>> {
        let pkg = base.join(
            self.package
                .as_ref()
                .context("Выберите программу станции")?,
        );
        let path = |p: &Path| p.to_string_lossy().into_owned();
        let mode = match self.mode {
            Mode::Gateway => "gateway",
            Mode::Serve => "serve",
            Mode::Monitor => "monitor",
        };
        let mut args = vec![
            "wplc".into(),
            mode.into(),
            path(&pkg),
            path(&pkg.with_file_name("s7-map.json")),
        ];
        match self.mode {
            Mode::Gateway => args.push(path(
                &self
                    .protocols
                    .as_ref()
                    .map(|p| base.join(p))
                    .unwrap_or_else(|| pkg.with_file_name("protocols.json")),
            )),
            Mode::Serve => {
                args.push(path(
                    &base.join(
                        self.snap7
                            .as_ref()
                            .context("Укажите библиотеку snap7 в настройках")?,
                    ),
                ));
                args.push(self.port.unwrap_or(102).to_string());
            }
            Mode::Monitor => {}
        }
        if !matches!(self.mode, Mode::Gateway) {
            args.push(self.period_ms.unwrap_or(100).to_string());
        }
        args.push(self.cycles.to_string());
        if matches!(self.mode, Mode::Serve) {
            args.extend([
                self.ip.unwrap_or(std::net::Ipv4Addr::LOCALHOST).to_string(),
                self.rack.unwrap_or(0).to_string(),
                self.slot.unwrap_or(2).to_string(),
            ]);
        }
        Ok(args)
    }
}

fn station_description(code: &str, caption: &str) -> Option<String> {
    let code = code.trim();
    let caption = caption.trim();
    if code.is_empty()
        || !code.bytes().all(|b| b.is_ascii_digit())
        || caption.is_empty()
        || caption.chars().any(char::is_control)
    {
        return None;
    }
    Some(format!("Код {code} — {caption}"))
}

fn project_description(package: &Path) -> Option<String> {
    let build = package.ancestors().find(|p| {
        p.file_name()
            .is_some_and(|n| n.eq_ignore_ascii_case("wplc-build"))
    })?;
    let mut project = None;
    for entry in fs::read_dir(build.parent()?).ok()? {
        let path = entry.ok()?.path();
        if path
            .extension()
            .is_some_and(|ext| ext.eq_ignore_ascii_case("mpr"))
            && path.is_file()
        {
            if project.is_some() {
                return None;
            }
            project = Some(path);
        }
    }
    let bytes = super::bounded_read(project?).ok()?;
    let text = match std::str::from_utf8(&bytes) {
        Ok(text) => std::borrow::Cow::Borrowed(text.trim_start_matches('\u{feff}')),
        Err(_) => {
            encoding_rs::WINDOWS_1251
                .decode_without_bom_handling(&bytes)
                .0
        }
    };
    let (mut code, mut caption, mut station) = ("", "", false);
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {
            station = line.eq_ignore_ascii_case("[station]");
        } else if station && let Some((key, value)) = line.split_once('=') {
            if key.trim().eq_ignore_ascii_case("Code") {
                code = value;
            }
            if key.trim().eq_ignore_ascii_case("Caption") {
                caption = value;
            }
        }
    }
    station_description(code, caption)
}

pub(super) fn station_name(path: &Path, manifest: &str) -> String {
    manifest
        .split_once(':')
        .and_then(|(code, name)| station_description(code, name))
        .or_else(|| project_description(path))
        .unwrap_or_else(|| {
            format!(
                "Имя и код станции не указаны (пакет: {})",
                manifest.escape_debug()
            )
        })
}

fn show_station_title(station: &str) {
    use std::io::IsTerminal;
    if !io::stdout().is_terminal() {
        return;
    }
    let title: String = format!("WPLC — {station}")
        .chars()
        .filter(|c| !c.is_control())
        .collect();
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn SetConsoleTitleW(title: *const u16) -> i32;
        }
        let wide: Vec<_> = title.encode_utf16().chain(Some(0)).collect();
        unsafe {
            SetConsoleTitleW(wide.as_ptr());
        }
    }
    #[cfg(not(windows))]
    {
        use std::io::Write;
        let _ = write!(io::stdout().lock(), "\x1b]0;{title}\x07");
    }
}

fn selected_station(base: &Path, settings: &Settings) -> String {
    let Some(path) = &settings.package else {
        return "программа не выбрана".into();
    };
    let path = base.join(path);
    match super::bounded_read(&path).and_then(|bytes| super::Package::decode(&bytes)) {
        Ok(package) => station_name(&path, &package.manifest.name),
        Err(e) => format!("Ошибка чтения пакета: {e}"),
    }
}

fn choose(
    settings: &Settings,
    file: &Path,
    input: &mpsc::Receiver<String>,
) -> Result<Option<Settings>> {
    let base = file.parent().unwrap();
    let mut dir = settings
        .package
        .as_ref()
        .and_then(|p| base.join(p).parent().map(Path::to_owned))
        .filter(|p| p.is_dir())
        .unwrap_or_else(|| base.to_owned());
    loop {
        println!("{}  [..] вверх, [0] отмена", dir.display());
        let mut entries = Vec::new();
        for entry in fs::read_dir(&dir)? {
            let p = entry?.path();
            if p.is_dir() || is_package(&p) {
                entries.push(p);
            }
        }
        entries.sort();
        for (i, p) in entries.iter().enumerate() {
            println!(
                "{}  {}{}",
                i + 1,
                p.file_name().unwrap().to_string_lossy(),
                if p.is_dir() { "/" } else { "" }
            );
        }
        println!("Введите номер или путь к .pkg:");
        let Ok(line) = input.recv() else {
            return Ok(None);
        };
        let s = line.trim().trim_matches('"');
        if s.is_empty() || s == "0" {
            return Ok(None);
        }
        if s == ".." {
            if let Some(parent) = dir.parent() {
                dir = parent.to_owned();
            }
            continue;
        }
        let p = if let Ok(i) = s.parse::<usize>() {
            entries
                .get(i.checked_sub(1).context("Нет такого номера")?)
                .context("Нет такого номера")?
                .clone()
        } else {
            dir.join(s)
        };
        let p = p.canonicalize()?;
        if p.is_dir() {
            dir = p;
            continue;
        }
        ensure!(is_package(&p), "Нужен файл .pkg");
        println!("ЗАГРУЗКА {}", p.display());
        let runtime = super::Runtime::load(super::Package::decode(&super::bounded_read(&p)?)?)?;
        let map = serde_json::from_slice(&super::bounded_read_limit(
            p.with_file_name("s7-map.json"),
            64 * 1024 * 1024,
        )?)?;
        super::Bridge::new(runtime, map)?;
        let mut new = settings.clone();
        if settings
            .package
            .as_ref()
            .and_then(|old| base.join(old).canonicalize().ok())
            .as_ref()
            != Some(&p)
        {
            new.protocols = None;
        }
        new.package = Some(p);
        new.save(file)?;
        return Ok(Some(new));
    }
}

fn is_package(p: &Path) -> bool {
    p.extension().is_some_and(|x| x.eq_ignore_ascii_case("pkg"))
}

pub fn run() -> Result<()> {
    let file = std::env::current_exe()?.with_file_name("wplc.json");
    let base = file.parent().unwrap();
    let mut settings: Settings = match fs::read(&file) {
        Ok(bytes) => serde_json::from_slice(&bytes).with_context(|| file.display().to_string())?,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let s = Settings::default();
            s.save(&file)?;
            s
        }
        Err(e) => return Err(e.into()),
    };
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        for line in io::stdin().lock().lines().map_while(Result::ok) {
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let mut launch = settings.autostart && settings.package.is_some();
    let mut station = selected_station(base, &settings);
    show_station_title(&station);
    loop {
        if launch {
            launch = false;
            let args = settings.arguments(base)?;
            println!(
                "ЗАГРУЗКА  |  {station}\n{}  |  3 + Enter — остановить",
                args[2]
            );
            let (stop, signal) = tokio::sync::watch::channel(false);
            let worker = thread::spawn(move || super::execute(&args, signal));
            let mut eof = false;
            while !worker.is_finished() {
                if eof {
                    thread::sleep(Duration::from_millis(50));
                    continue;
                }
                match rx.recv_timeout(Duration::from_millis(50)) {
                    Ok(s) if s.trim() == "3" => {
                        println!("ОСТАНОВКА  |  {station}");
                        stop.send_replace(true);
                        worker.thread().unpark();
                        break;
                    }
                    Ok(_) | Err(mpsc::RecvTimeoutError::Timeout) => {}
                    Err(mpsc::RecvTimeoutError::Disconnected) => eof = true,
                }
            }
            let result = worker
                .join()
                .map_err(|_| anyhow::anyhow!("Сбой потока станции"))?;
            if let Err(e) = result {
                if eof {
                    return Err(e);
                }
                eprintln!("Ошибка запуска: {e:#}");
            }
        }
        println!(
            "ОСТАНОВЛЕНО  |  {station}\n{}\n1 — выбрать .pkg   2 — запустить   0 — выход",
            settings
                .package
                .as_deref()
                .map(|p| p.display().to_string())
                .unwrap_or_default()
        );
        let Ok(line) = rx.recv() else {
            break;
        };
        match line.trim() {
            "0" => break,
            "1" => match choose(&settings, &file, &rx) {
                Ok(Some(new)) => {
                    settings = new;
                    station = selected_station(base, &settings);
                    show_station_title(&station);
                }
                Ok(None) => {}
                Err(e) => eprintln!("Ошибка загрузки: {e:#}"),
            },
            "2" if settings.package.is_some() => launch = true,
            _ => println!("Выберите один из пунктов меню"),
        }
    }
    Ok(())
}
