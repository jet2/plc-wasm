"""Interactive local station discovery, rebuild and visible runtime launcher."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
import re
from pathlib import Path
import shutil
import signal
import socket
import struct
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.local-validation' / 'station-menu.json'
SETTINGS = Path(__file__).resolve().with_name('menu-settings.json')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def load_settings():
    settings = read_json(SETTINGS) if SETTINGS.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError('menu-settings.json должен содержать JSON-объект.')
    return settings


def save_settings(settings):
    temporary = SETTINGS.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(SETTINGS)


def save_search_folder(root):
    settings = load_settings()
    settings['stations_folder'] = str(root.resolve())
    save_settings(settings)


def search_folder(override=None):
    if override is not None:
        return override.expanduser().resolve()
    if not SETTINGS.exists():
        save_search_folder(ROOT / 'build')
    settings = read_json(SETTINGS)
    if not isinstance(settings, dict):
        raise ValueError('menu-settings.json должен содержать JSON-объект.')
    value = settings.get('stations_folder')
    if not isinstance(value, str) or not value.strip():
        raise ValueError('В menu-settings.json требуется строка stations_folder с путём к папке.')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else ROOT / path).resolve()


def package_manifest(path):
    with path.open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b'SWP1':
            raise ValueError(f'Invalid package header: {path}')
        length, module_size = struct.unpack('<II', header[4:])
        if not 0 < length <= 1024 * 1024 or path.stat().st_size != 12 + length + module_size:
            raise ValueError(f'Invalid package length: {path}')
        manifest = json.loads(stream.read(length))
        if not isinstance(manifest, dict):
            raise ValueError(f'Invalid package manifest: {path}')
        return manifest


def search_error(error):
    raise error


def discover(root):
    result = []
    for directory, _, files in os.walk(root, onerror=search_error):
        for filename in files:
            path = Path(directory) / filename
            if path.suffix.lower() != '.pkg':
                continue
            try:
                result.append((path.resolve(), package_manifest(path)))
            except ValueError:
                continue  # Not a valid package; filesystem errors abort the search.
    return sorted(result)


def show(stations):
    for number, (path, manifest) in enumerate(stations, 1):
        mapped = 'S7' if path.with_name('s7-map.json').is_file() else 'без карты S7'
        try:
            size = f'{path.stat().st_size / 1048576:.2f} МиБ'
        except OSError:
            size = 'файл недоступен'
        print(f'{number}) {path.parent.name} | {manifest.get("name", "?")} | '
              f'{size} | {mapped}\n   {path}')
    if not stations:
        print('В настройках нет скомпилированных станций. Выполните поиск (пункт 1).')


def nonempty_directory(directory):
    try:
        metadata = directory.stat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and any(directory.iterdir())


def source_directory(program):
    for name in ('Source', 'Soure'):
        directory = program / name
        if nonempty_directory(directory):
            return directory
    return None


def discover_sources(root):
    result = []
    for directory, children, _ in os.walk(root, onerror=search_error):
        program = Path(directory)
        if program.name.lower() != 'program':
            continue
        tables = program / 'Tables'
        if source_directory(program) is not None and nonempty_directory(tables):
            result.append(dict(name=program.parent.name, program=str(program.resolve())))
            children[:] = []
    return sorted(result, key=lambda station: station['program'])


def update_source_links(settings):
    for source in settings.get('source_stations', []):
        source['packages'] = [item['package'] for item in settings.get('compiled_stations', [])
                              if item.get('program') == source['program']]
        source['compiled'] = bool(source['packages'])


def package_record(path, manifest, program):
    return dict(package=str(path), name=manifest.get('name', path.stem), program=program,
                s7_map=path.with_name('s7-map.json').is_file())


def refresh_catalog(root, config, include_sources=False):
    if not root.is_dir():
        raise ValueError(f'????? ?????? ?? ???????: {root}')
    settings = load_settings()
    # Build the complete replacement before touching the settings file.
    if include_sources:
        settings['source_stations'] = discover_sources(root)
        settings['source_search_folder'] = str(root)
    else:
        associations = {item['package']: item.get('program')
                        for item in settings.get('compiled_stations', [])}
        associations.update(config.get('sources', {}))
        settings['compiled_stations'] = [package_record(path, manifest, associations.get(str(path)))
                                         for path, manifest in discover(root)]
    update_source_links(settings)
    save_settings(settings)
    return load_settings()


def register_build(package, program):
    # Successful compilation adds exactly its output, without running a search.
    settings = load_settings()
    record = package_record(package, package_manifest(package), str(program))
    settings['compiled_stations'] = [item for item in settings.get('compiled_stations', [])
                                     if item['package'] != str(package)] + [record]
    update_source_links(settings)
    save_settings(settings)


def show_sources(settings):
    stations = settings.get('source_stations', [])
    for index, source in enumerate(stations, 1):
        status = 'есть сборка' if source.get('compiled') else 'не скомпилирована'
        print(f'{index}) {source["name"]} | {status}\n   {source["program"]}')
    if not stations:
        print('Исходные станции не найдены. Выполните поиск (пункт 6).')
    return stations


def saved_packages(settings):
    return [(Path(item['package']), {'name': item.get('name', '?')})
            for item in settings.get('compiled_stations', [])]


def build_output(program):
    """Place each build beside the nearest ancestor's MPR project."""
    for directory in program.resolve().parents:
        projects = [path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == '.mpr']
        if len(projects) > 1:
            raise ValueError(f'ALARM: несколько .mpr в {directory}; выбор станции неоднозначен.')
        if projects:
            return directory / 'wplc-build' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    raise ValueError('Файл .mpr не найден в родительских папках Program; каталог сборки не определён.')


def compile_source(source, config):
    program = Path(source['program'])
    if source_directory(program) is None or not (program / 'Tables').is_dir():
        raise ValueError('Исходная станция больше не доступна; повторите поиск.')
    # Existing local reports supply known options without hardcoding project symbols.
    reports = [Path(package).with_name('build.json')
               for package, origin in config.get('sources', {}).items()
               if origin == str(program) and Path(package).with_name('build.json').is_file()]
    report = read_json(reports[-1]) if reports else {}
    entry = ask('Циклическая функция (entry)', report.get('entry', ''))
    if not entry:
        raise ValueError('Необходимо указать циклическую функцию.')
    init = ask('Функции инициализации через запятую, - для пустого списка',
               ','.join(report.get('init', [])) or '-')
    profile = ask('Профиль: strict или cyclic-sim', report.get('system_profile', 'strict'))
    if profile not in {'strict', 'cyclic-sim'}:
        raise ValueError('Неизвестный профиль.')
    fuel = integer('Бюджет Wasm на вызов', report.get('fuel_per_call', 10000000), 1, 100000000)
    runtime = runtime_path(config)
    output = build_output(program)
    command = [sys.executable, str(ROOT / 'tools/compile_program.py'), str(program),
               '--entry', entry, '--name', source['name'], '--system-profile', profile,
               '--fuel-per-call', str(fuel), '--output', str(output), '--runtime', str(runtime), '--s7-map',
               '--station', str(output.parent.parent)]
    if init != '-':
        for function in init.split(','):
            if function.strip():
                command.extend(['--init', function.strip()])
    print(f'Сборка в {output}', flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    config.setdefault('sources', {})[str(output / 'program.pkg')] = str(program)
    save_config(config)
    register_build(output / 'program.pkg', program)
    print(f'Готово: {output / "program.pkg"}')


def ask(label, default=''):
    value = input(f'{label}' + (f' [{default}]' if str(default) else '') + ': ').strip()
    return value or str(default)


def integer(label, default, low, high):
    value = int(ask(label, default))
    if not low <= value <= high:
        raise ValueError(f'Допустимый диапазон: {low}..{high}')
    return value


def save_config(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(CONFIG)


def runtime_path(config):
    suffix = '.exe' if os.name == 'nt' else ''
    default = ROOT / 'target' / 'release' / ('station-runtime' + suffix)
    path = Path(ask('Исполняемый файл runtime', config.get('runtime', str(default)))).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'Файл не найден: {path}')
    config['runtime'] = str(path)
    return path


def build_command(package, manifest, source, output, runtime):
    report = read_json(package.with_name('build.json'))
    # Reuse the exact compiler options, including initialization order and presets.
    command = [sys.executable, str(ROOT / 'tools' / 'compile_program.py'), str(source),
               '--entry', report['entry'], '--name', manifest['name'],
               '--system-profile', report['system_profile'],
               '--fuel-per-call', str(report['fuel_per_call']),
               '--output', str(output), '--runtime', str(runtime)]
    for key, flag in [('init', '--init'), ('presets', '--set'), ('watches', '--watch')]:
        for value in report[key]:
            command.extend([flag, value])
    if package.with_name('s7-map.json').is_file():
        command.append('--s7-map')
        if output.parent.name == 'wplc-build':
            command.extend(['--station', str(output.parent.parent)])
    return command


def rebuild(package, manifest, config):
    sources = config.setdefault('sources', {})
    source = Path(ask('Папка Program с Source и Tables', sources.get(str(package), ''))).expanduser().resolve()
    if source_directory(source) is None or not (source / 'Tables').is_dir():
        raise ValueError('Нужна папка проекта с подпапками Source и Tables.')
    runtime = runtime_path(config)
    output = build_output(source)
    command = build_command(package, manifest, source, output, runtime)
    sources[str(package)] = str(source)
    save_config(config)
    print(f'Сборка в {output}', flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    sources[str(output / 'program.pkg')] = str(source)
    save_config(config)
    register_build(output / 'program.pkg', source)
    print(f'Готово: {output / "program.pkg"}')


def launch(package, config, mode=None):
    runtime = runtime_path(config)
    mapping = package.with_name('s7-map.json')
    if not mapping.is_file():
        raise ValueError('Для запуска с циклом и S7 нужна карта s7-map.json. Пересоберите пакет с --s7-map.')
    mode = mode or ask('Режим: 1 — Snap7, 2 — офлайн монитор, 3 — Станция — OPC UA / Modbus / S7', '1')
    period = integer('Период цикла, мс', 100, 1, 60000) if mode != '3' else 100
    command = [str(runtime), 'monitor', str(package), str(mapping), str(period), '0']
    if mode == '1':
        default = ROOT / '.local-validation/vendor/snap7/build/bin/win64/snap7.dll'
        library = Path(ask('Библиотека Snap7', config.get('snap7', str(default) if os.name == 'nt' else ''))).expanduser().resolve()
        if not library.is_file():
            raise ValueError(f'Библиотека не найдена: {library}')
        port = integer('Порт S7 на 127.0.0.1', 102, 1, 65535)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', port))
        config['snap7'] = str(library)
        command = [str(runtime), 'serve', str(package), str(mapping), str(library), str(port), str(period), '0']
    elif mode == '3':
        default = package.with_name('protocols.json')
        if not default.is_file():
            default = Path(config.get('protocols', ROOT / '.local-validation' / 'protocols.json'))
        settings = Path(ask('JSON настроек протоколов', str(default))).expanduser().resolve()
        if not settings.exists():
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({
                'period_ms': 100,
                'opcua': {'port': 4841, 'pki_dir': str(ROOT / '.local-validation/pki'), 'tags': []},
                'snap7': None,
                'modbus': [],
            }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(f'Созданы настройки OPC UA: {settings}. Modbus-устройства и Snap7 добавляются в этот JSON.')
        config['protocols'] = str(settings)
        command = [str(runtime), 'gateway', str(package), str(mapping), str(settings), '0']
    elif mode != '2':
        raise ValueError('Неизвестный режим.')
    save_config(config)
    # Separate executable per launch: rebuilding runtime cannot overwrite a running image.
    destination = ROOT / 'build/live/menu' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    destination.mkdir(parents=True)
    executable = destination / ('wplc.exe' if os.name == 'nt' else 'wplc')
    shutil.copy2(runtime, executable)
    command[0] = str(executable)
    if os.name == 'nt':
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # SW_SHOWNORMAL: do not inherit a hidden menu window.
        process = subprocess.Popen(command, cwd=ROOT,
                                   creationflags=subprocess.CREATE_NEW_CONSOLE,
                                   startupinfo=startup)
    elif shutil.which('x-terminal-emulator'):
        process = subprocess.Popen(['x-terminal-emulator', '-e', *command], cwd=ROOT)
    elif shutil.which('gnome-terminal'):
        process = subprocess.Popen(['gnome-terminal', '--', *command], cwd=ROOT)
    elif shutil.which('xterm'):
        process = subprocess.Popen(['xterm', '-e', *command], cwd=ROOT)
    else:
        print('Терминал не найден; запуск в текущем окне. Ctrl+C — остановка.')
        subprocess.run(command, cwd=ROOT, check=True)
        return
    print(f'Запуск запрошен, PID процесса запуска: {process.pid}. '
          'Состояние и ошибки — в окне wplc; Ctrl+C — остановка.')


def managed_executable(path):
    path = Path(path).resolve()
    return (path.name.lower() in {'wplc.exe', 'wplc'}
            and path.is_relative_to((ROOT / 'build/live').resolve()))


def windows_process(pid, terminate=False):
    """Open a stable process handle and obtain its executable and creation time."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    access = 0x1000 | (0x0001 | 0x00100000 if terminate else 0)
    handle = kernel.OpenProcess(access, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        identity = (str(Path(buffer.value).resolve()),
                    (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        return kernel, handle, identity
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def linux_identity(pid):
    directory = Path('/proc') / str(pid)
    executable = str((directory / 'exe').resolve(strict=True))
    # comm may contain spaces and parentheses; starttime is field 22.
    start = (directory / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    return executable, start


def running_stations():
    result = []
    if os.name == 'nt':
        script = ('[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); '
                  "@(Get-CimInstance Win32_Process -Filter \"Name='wplc.exe'\" | "
                  'Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress')
        output = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, encoding='utf-8', check=True)
        records = json.loads(output.stdout.strip() or '[]')
        if isinstance(records, dict):
            records = [records]
        for record in records:
            try:
                kernel, handle, identity = windows_process(record['ProcessId'])
                kernel.CloseHandle(handle)
                if managed_executable(identity[0]):
                    result.append(dict(pid=record['ProcessId'], identity=identity,
                                       command=record.get('CommandLine') or identity[0]))
            except OSError:
                continue  # Process exited or is inaccessible.
    else:
        for directory in Path('/proc').iterdir():
            if not directory.name.isdecimal():
                continue
            try:
                identity = linux_identity(int(directory.name))
                if managed_executable(identity[0]):
                    command = (directory / 'cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
                    result.append(dict(pid=int(directory.name), identity=identity, command=command))
            except OSError:
                continue
    return sorted(result, key=lambda record: record['pid'])


def stop_station(record):
    """Revalidate identity and signal a stable handle, never a reused PID."""
    if not managed_executable(record['identity'][0]):
        raise ValueError('Процесс не является экземпляром wplc из build/live.')
    if os.name == 'nt':
        kernel, handle, identity = windows_process(record['pid'], terminate=True)
        try:
            if identity != record['identity']:
                raise ValueError('Процесс изменился; обновите список экземпляров.')
            # Windows has no targeted console Ctrl+C for an unrelated console.
            # Terminate only this process, using the handle checked above.
            if not kernel.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.WaitForSingleObject(handle, 5000) != 0:
                raise OSError('Завершение процесса не подтверждено за 5 секунд.')
        finally:
            kernel.CloseHandle(handle)
    else:
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise OSError('Для безопасной остановки нужны Linux с pidfd и Python 3.9+. Используйте Ctrl+C в окне wplc.')
        import select
        descriptor = os.pidfd_open(record['pid'])
        try:
            if linux_identity(record['pid']) != record['identity']:
                raise ValueError('Процесс изменился; обновите список экземпляров.')
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            if not select.select([descriptor], [], [], 5)[0]:
                raise OSError('Завершение процесса не подтверждено за 5 секунд.')
        finally:
            os.close(descriptor)


def stop_menu():
    stations = running_stations()
    if not stations:
        print('Работающих экземпляров wplc в build/live не найдено.')
        return
    for index, record in enumerate(stations, 1):
        print(f'{index}) PID {record["pid"]}\n   {record["command"]}')
    print('Остановка завершит процесс; состояние PLC в памяти будет потеряно.')
    selected = integer('Номер экземпляра для остановки (0 — отмена)', 0, 0, len(stations))
    if selected:
        record = stations[selected - 1]
        stop_station(record)
        print(f'Экземпляр PID {record["pid"]} остановлен.')


def build_files(package):
    package = Path(package).absolute()
    folder = package.parent
    if package.name != 'program.pkg' or not re.fullmatch(r'\d{8}-\d{6}-\d{6}', folder.name):
        raise ValueError('Удаление доступно только для сборок в отдельных папках с датой и временем.')
    # Reject symlinks and junctions before resolving or deleting anything.
    for path in (package, folder, *folder.parents):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError(f'Ссылка или junction в пути сборки: {path}')
        if path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Reparse point в пути сборки: {path}')
    folder = folder.resolve(strict=True)
    local = any(folder.is_relative_to((ROOT / base).resolve())
                for base in ('build/compiled', 'build/recompiled'))
    station = (folder.parent.name == 'wplc-build'
               and any(p.is_file() and p.suffix.lower() == '.mpr' for p in folder.parent.parent.iterdir()))
    if not local and not station:
        raise ValueError(f'Папка не является каталогом сборки: {folder}')
    allowed = {'program.pkg', 'program.wat', 'manifest.json', 'build.json', 's7-map.json', 's7-map.json.tmp', 'protocols.json'}
    files = list(folder.iterdir())
    if not files or any(p.name not in allowed or not p.is_file() or p.is_symlink()
                        or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400 for p in files):
        raise ValueError(f'В папке есть посторонние файлы или подпапки: {folder}')
    return folder, files


def batch_selection(text, count):
    text = text.strip().lower()
    if not text or text == '0':
        return []
    if text in {'all', 'все'}:
        return list(range(count))
    result = set()
    for part in text.split(','):
        match = re.fullmatch(r'\s*(\d+)(?:\s*-\s*(\d+))?\s*', part)
        if not match:
            raise ValueError('Введите номера через запятую или диапазоны, например 1,3-5.')
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= count:
            raise ValueError('Номер сборки или диапазон вне списка.')
        result.update(range(first - 1, last))
    return sorted(result)


def check_build_not_running(folder, processes):
    paths = [str(folder)]
    if folder.is_relative_to(ROOT):
        paths.append(str(folder.relative_to(ROOT)))
    for process in processes:
        command = process['command'].replace('\\', '/').casefold()
        if any(path.replace('\\', '/').casefold() + '/' in command for path in paths):
            raise ValueError(f'Сборка используется процессом PID {process["pid"]}. Сначала остановите его.')


def delete_builds(packages):
    # Validate the whole selection before the first destructive action.
    processes = running_stations()
    for package in packages:
        folder, _ = build_files(package)
        check_build_not_running(folder, processes)
    removed = set()
    try:
        for package in packages:
            folder, files = build_files(package)
            check_build_not_running(folder, running_stations())
            for path in files:
                path.unlink()
            folder.rmdir()
            removed.add(str(Path(package).resolve()))
            print(f'Удалено: {folder}')
    finally:
        if removed:
            settings = load_settings()
            settings['compiled_stations'] = [item for item in settings.get('compiled_stations', [])
                                             if str(Path(item['package']).resolve()) not in removed]
            update_source_links(settings)
            save_settings(settings)
            if CONFIG.exists():
                config = read_json(CONFIG)
                config['sources'] = {path: source for path, source in config.get('sources', {}).items()
                                     if str(Path(path).resolve()) not in removed}
                save_config(config)


def delete_menu():
    candidates = []
    for package, manifest in saved_packages(load_settings()):
        try:
            build_files(package)
        except (OSError, ValueError):
            continue
        if package not in [path for path, _ in candidates]:
            candidates.append((package, manifest))
    if not candidates:
        print('Нет сборок для удаления в отдельных папках с датой и временем.')
        return
    show(candidates)
    selection = batch_selection(input('Удалить номера (1,3-5), все или 0 — отмена: '), len(candidates))
    if not selection:
        return
    packages = [candidates[index][0] for index in selection]
    print('Будут безвозвратно удалены папки сборок со всеми артефактами:')
    for package in packages:
        print(package.parent)
    if input('Для подтверждения введите УДАЛИТЬ: ').strip().upper() != 'УДАЛИТЬ':
        print('Удаление отменено.')
        return
    delete_builds(packages)


def emulator_command(config, package=None):
    from station_protocols import station_files
    default_folder = ''
    if package:
        source = config.get('sources', {}).get(str(package))
        for parent in (Path(source).parents if source else package.parents):
            if any(p.is_file() and p.suffix.lower() == '.mpr' for p in parent.iterdir()):
                default_folder = str(parent)
                break
    folder = Path(ask('Папка станции с MPR и USO', default_folder or config.get('emulator_station', ''))).expanduser().resolve()
    station_files(folder)
    suffix = '.exe' if os.name == 'nt' else ''
    candidates = [ROOT.parent / 'WEmulator' / 'target' / profile / ('wemulator' + suffix)
                  for profile in ('release', 'debug')]
    default = next((p for p in candidates if p.is_file()), candidates[0])
    executable = Path(ask('Исполняемый файл WEmulator', config.get('emulator', str(default)))).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f'Файл не найден: {executable}')
    config['emulator'] = str(executable)
    config['emulator_station'] = str(folder)
    save_config(config)
    return [str(executable), str(folder)]


def start_emulator(command, config):
    settings = Path(config.get('protocols', ''))
    port = 4841
    if settings.is_file():
        ua = read_json(settings).get('opcua')
        if ua:
            port = ua['port']
    endpoint = ask('OPC UA сервера станции', f'opc.tcp://127.0.0.1:{port}/')
    process = subprocess.Popen(command + ['--opc', endpoint, '--ns', 'urn:plc-wasm:tags'],
                               cwd=Path(command[0]).parent)
    print(f'WEmulator запущен, PID {process.pid}. Остановка — закрытием окна эмулятора.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='override stations_folder from menu-settings.json for this run')
    parser.add_argument('--list', action='store_true', help='list packages and exit')
    args = parser.parse_args()
    try:
        root = search_folder(args.root)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    config = read_json(CONFIG) if CONFIG.exists() else {}
    if args.list:
        show(saved_packages(load_settings()))
        return
    while True:
        try:
            root = search_folder(args.root)
            settings = load_settings()
            print(f'\nПапка поиска: {root}'
                  f'\n\nСкомпилированные станции — сохранено: {len(settings.get("compiled_stations", []))}'
                  '\n1) Найти и сохранить скомпилированные станции'
                  '\n2) Рекомпилировать сохранённую сборку\n3) Выбрать станцию для запуска'
                  f'\n\nИсходные станции — сохранено: {len(settings.get("source_stations", []))}'
                  '\n6) Найти исходные / нескомпилированные станции'
                  '\n7) Выбрать станцию для компиляции'
                  '\n\nУправление\n4) Остановить экземпляр станции\n5) Изменить папку поиска'
                  '\n8) Удалить сборки пачкой\n9) Запустить эмулятор станции'
                  '\n10) Запустить станцию + эмулятор\n0) Выход')
            choice = input('> ').strip()
            if choice == '0':
                return
            settings = load_settings()
            config = read_json(CONFIG) if CONFIG.exists() else {}
            root = search_folder(args.root)
            if choice == '9':
                start_emulator(emulator_command(config), config)
                continue
            if choice == '6':
                settings = refresh_catalog(root, config, include_sources=True)
                show_sources(settings)
                print('Списки сохранены; настройки перечитаны.')
                continue
            if choice == '7':
                print('\nИсходные станции из настроек:')
                sources = show_sources(settings)
                if sources:
                    selected = integer('Номер исходной станции (0 — отмена)', 0, 0, len(sources))
                    if selected:
                        compile_source(sources[selected - 1], config)
                        settings = load_settings()
                        show_sources(settings)
                continue
            if choice == '4':
                stop_menu()
                continue
            if choice == '8':
                delete_menu()
                continue
            if choice == '5':
                candidate = Path(ask('Папка')).expanduser().resolve()
                if not candidate.is_dir():
                    raise ValueError('Папка не найдена.')
                save_search_folder(candidate)
                args.root = None
                root = candidate
                continue
            if choice not in {'1', '2', '3', '10'}:
                print('Выберите пункт меню.')
                continue
            if choice == '1':
                settings = refresh_catalog(root, config)
            stations = saved_packages(settings)
            print('\nСкомпилированные станции из настроек:')
            show(stations)
            if choice == '1' or not stations:
                continue
            selected = integer('Номер станции (0 — отмена)', 0, 0, len(stations))
            if selected:
                package, manifest = stations[selected - 1]
                manifest = package_manifest(package)
                if choice == '2':
                    rebuild(package, manifest, config)
                elif choice == '10':
                    command = emulator_command(config, package)
                    launch(package, config, mode='3')
                    start_emulator(command, config)
                else:
                    launch(package, config)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            print(f'Ошибка: {error}')
        except KeyboardInterrupt:
            print('\nДействие прервано.')
        except EOFError:
            return


if __name__ == '__main__':
    main()
