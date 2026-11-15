"""Interactive local station discovery, rebuild and visible runtime launcher."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import struct
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


def discover(root):
    result = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.suffix.lower() != '.pkg':
            continue
        try:
            result.append((path.resolve(), package_manifest(path)))
        except (OSError, ValueError):
            continue
    return result


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


def source_directory(program):
    for name in ('Source', 'Soure'):
        directory = program / name
        if directory.is_dir() and any(directory.iterdir()):
            return directory
    return None


def discover_sources(root):
    result = []
    for directory, children, _ in os.walk(root):
        program = Path(directory)
        if program.name.lower() != 'program':
            continue
        tables = program / 'Tables'
        if source_directory(program) is not None and tables.is_dir() and any(tables.iterdir()):
            result.append(dict(name=program.parent.name, program=str(program.resolve())))
            children[:] = []
    return sorted(result, key=lambda station: station['program'])


def refresh_catalog(root, config, include_sources=False):
    if not root.is_dir():
        raise ValueError(f'Папка поиска не найдена: {root}')
    settings = load_settings()
    # Keep valid packages found earlier in other folders, plus local build outputs.
    folders = {root.resolve(), (ROOT / 'build').resolve()}
    folders.update(Path(item['package']).parent for item in settings.get('compiled_stations', []))
    packages = {}
    for folder in sorted(folders):
        if folder.is_dir():
            packages.update(discover(folder))
    associations = {item['package']: item.get('program')
                    for item in settings.get('compiled_stations', [])}
    associations.update(config.get('sources', {}))
    settings['compiled_stations'] = [dict(package=str(path), name=manifest.get('name', path.stem),
                                         program=associations.get(str(path)),
                                         s7_map=path.with_name('s7-map.json').is_file())
                                    for path, manifest in sorted(packages.items())]
    if include_sources:
        settings['source_stations'] = discover_sources(root)
        settings['source_search_folder'] = str(root)
    for source in settings.get('source_stations', []):
        source['packages'] = [item['package'] for item in settings['compiled_stations']
                              if item['program'] == source['program']]
        source['compiled'] = bool(source['packages'])
    save_settings(settings)
    # Subsequent choices must use the persisted catalog, not the search snapshot.
    return load_settings()


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
    output = ROOT / 'build/compiled' / program.parent.name / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    command = [sys.executable, str(ROOT / 'tools/compile_program.py'), str(program),
               '--entry', entry, '--name', source['name'], '--system-profile', profile,
               '--fuel-per-call', str(fuel), '--output', str(output), '--runtime', str(runtime), '--s7-map']
    if init != '-':
        for function in init.split(','):
            if function.strip():
                command.extend(['--init', function.strip()])
    print(f'Сборка в {output}', flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    config.setdefault('sources', {})[str(output / 'program.pkg')] = str(program)
    save_config(config)
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
    return command


def rebuild(package, manifest, config):
    sources = config.setdefault('sources', {})
    source = Path(ask('Папка Program с Source и Tables', sources.get(str(package), ''))).expanduser().resolve()
    if source_directory(source) is None or not (source / 'Tables').is_dir():
        raise ValueError('Нужна папка проекта с подпапками Source и Tables.')
    runtime = runtime_path(config)
    output = ROOT / 'build/recompiled' / package.parent.name / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    command = build_command(package, manifest, source, output, runtime)
    sources[str(package)] = str(source)
    save_config(config)
    print(f'Сборка в {output}', flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    sources[str(output / 'program.pkg')] = str(source)
    save_config(config)
    print(f'Готово: {output / "program.pkg"}')


def launch(package, config):
    runtime = runtime_path(config)
    mapping = package.with_name('s7-map.json')
    if not mapping.is_file():
        raise ValueError('Для запуска с циклом и S7 нужна карта s7-map.json. Пересоберите пакет с --s7-map.')
    mode = ask('Режим: 1 — Snap7, 2 — офлайн монитор', '1')
    period = integer('Период цикла, мс', 100, 1, 2147483647)
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
        process = subprocess.Popen(command, cwd=ROOT, creationflags=subprocess.CREATE_NEW_CONSOLE)
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
            settings = load_settings()
            print(f'\nПапка поиска: {root}'
                  f'\n\nСкомпилированные станции — сохранено: {len(settings.get("compiled_stations", []))}'
                  '\n1) Найти и сохранить скомпилированные станции'
                  '\n2) Рекомпилировать сохранённую сборку\n3) Выбрать станцию для запуска'
                  f'\n\nИсходные станции — сохранено: {len(settings.get("source_stations", []))}'
                  '\n6) Найти исходные / нескомпилированные станции'
                  '\n7) Выбрать станцию для компиляции'
                  '\n\nУправление\n4) Остановить экземпляр станции\n5) Изменить папку поиска\n0) Выход')
            choice = input('> ').strip()
            if choice == '0':
                return
            settings = load_settings()
            config = read_json(CONFIG) if CONFIG.exists() else {}
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
                        settings = refresh_catalog(root, config)
                        show_sources(settings)
                continue
            if choice == '4':
                stop_menu()
                continue
            if choice == '5':
                candidate = Path(ask('Папка')).expanduser().resolve()
                if not candidate.is_dir():
                    raise ValueError('Папка не найдена.')
                save_search_folder(candidate)
                root = candidate
                continue
            if choice not in {'1', '2', '3'}:
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
                    refresh_catalog(root, config)
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
