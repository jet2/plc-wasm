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
    loop {
        if launch {
            launch = false;
            let args = settings.arguments(base)?;
            println!("ЗАГРУЗКА {}  |  3 + Enter — остановить", args[2]);
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
                        println!("ОСТАНОВКА");
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
            "ОСТАНОВЛЕНО  |  {}\n1 — выбрать .pkg   2 — запустить   0 — выход",
            settings
                .package
                .as_deref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "программа не выбрана".into())
        );
        let Ok(line) = rx.recv() else {
            break;
        };
        match line.trim() {
            "0" => break,
            "1" => match choose(&settings, &file, &rx) {
                Ok(Some(new)) => settings = new,
                Ok(None) => {}
                Err(e) => eprintln!("Ошибка загрузки: {e:#}"),
            },
            "2" if settings.package.is_some() => launch = true,
            _ => println!("Выберите один из пунктов меню"),
        }
    }
    Ok(())
}
