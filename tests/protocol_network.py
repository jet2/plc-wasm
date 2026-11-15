"""Independent asyncua client + local Modbus simulator; generic exported Rust fixture only.

Install asyncua in a local venv. Export the fixture with PLC_PROTOCOL_FIXTURE_DIR
while running the Rust protocols test, then pass --fixture and --runtime here.
"""
import argparse
import asyncio
import ctypes
import json
import os
from pathlib import Path
import socket
import struct

from asyncua import Client, ua


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


async def eventually(check, timeout=15):
    async def loop():
        while not await check():
            await asyncio.sleep(.05)
    await asyncio.wait_for(loop(), timeout)


class Simulator:
    def __init__(self):
        self.registers = {}
        self.failed = False
        self.connections = 0

    async def connection(self, reader, writer):
        self.connections += 1
        try:
            while True:
                header = await reader.readexactly(7)
                length = int.from_bytes(header[4:6], 'big') - 1
                pdu = await reader.readexactly(length)
                if self.failed:
                    break
                function = pdu[0]
                address, count = struct.unpack('>HH', pdu[1:5])
                if function == 3:
                    response = bytes([3, count * 2]) + struct.pack('>' + 'H' * count, *([42] * count))
                elif function == 1:
                    response = bytes([1, (count + 7) // 8]) + bytes([3] * ((count + 7) // 8))
                elif function == 16:
                    values = struct.unpack('>' + 'H' * count, pdu[6:])
                    self.registers.update(enumerate(values, address))
                    response = pdu[:5]
                else:
                    response = bytes([function | 0x80, 1])
                writer.write(header[:4] + struct.pack('>H', len(response) + 1) + header[6:] + response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()


class Changes:
    def __init__(self):
        self.values = []

    def datachange_notification(self, node, value, data):
        self.values.append(value)


def snap7_read(library, port):
    dll = (ctypes.WinDLL if os.name == 'nt' else ctypes.CDLL)(str(library))
    dll.Cli_Create.restype = ctypes.c_void_p
    dll.Cli_SetParam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    dll.Cli_ConnectTo.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    dll.Cli_DBRead.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    dll.Cli_Destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    handle = ctypes.c_void_p(dll.Cli_Create())
    try:
        selected_port = ctypes.c_uint16(port)
        assert dll.Cli_SetParam(handle, 2, ctypes.byref(selected_port)) == 0
        assert dll.Cli_ConnectTo(handle, b'127.0.0.1', 0, 2) == 0
        data = (ctypes.c_ubyte * 4)()
        assert dll.Cli_DBRead(handle, 1, 0, 4, data) == 0
        return bytes(data)
    finally:
        dll.Cli_Destroy(ctypes.byref(handle))


async def main(args):
    directory = args.fixture.resolve()
    simulator = Simulator()
    mb_server = await asyncio.start_server(simulator.connection, '127.0.0.1', 0)
    mb_port = mb_server.sockets[0].getsockname()[1]
    port, s7_port = free_port(), free_port()
    settings = {
        'period_ms': 50,
        'opcua': {'port': port, 'pki_dir': str(directory / 'pki'), 'tags': ['Input', 'Command', 'Real', 'Flag', 'Text', 'ProcessFlag']},
        'snap7': {'library': str(args.snap7.resolve()), 'port': s7_port} if args.snap7 else None,
        'modbus': [{'name': 'sim', 'address': '127.0.0.1', 'port': mb_port, 'unit': 1, 'poll_ms': 50, 'timeout_ms': 250,
                    'requests': [
                        {'operation': 'read_holding_registers', 'address': 0, 'fields': ['Input']},
                        {'operation': 'read_coils', 'address': 0, 'fields': ['Flag', 'ProcessFlag']},
                        {'operation': 'write_registers', 'address': 10, 'fields': ['Command', 'Real']},
                    ]}],
    }
    config = directory / 'network-settings.json'
    config.write_text(json.dumps(settings), encoding='utf-8')
    process = None
    try:
        with (directory / 'gateway.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(str(args.runtime.resolve()), 'gateway',
                str(directory / 'program.pkg'), str(directory / 's7-map.json'), str(config), '800',
                stdout=log, stderr=log)

            async def listening():
                if process.returncode is not None:
                    raise AssertionError('Gateway exited; inspect gateway.log')
                try:
                    _, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close(); await writer.wait_closed()
                    return True
                except OSError:
                    return False
            await eventually(listening, 60)
            async with Client(f'opc.tcp://127.0.0.1:{port}/') as client:
                ns = await client.get_namespace_index('urn:plc-wasm:tags')
                nodes = {name: client.get_node(ua.NodeId(f'tag/{name}', ns)) for name in settings['opcua']['tags']}
                async def input_ready():
                    value = await nodes['Input'].read_data_value(raise_on_bad_status=False)
                    return value.StatusCode.is_good() and value.Value.Value == 42
                await eventually(input_ready)
                assert await nodes['Flag'].read_value() is True
                assert await nodes['ProcessFlag'].read_value() is True
                assert await client.nodes.objects.get_children()
                changes = Changes()
                subscription = await client.create_subscription(100, changes)
                await subscription.subscribe_data_change(nodes['Command'])
                await nodes['Command'].write_value(1234, ua.VariantType.UInt16)
                await nodes['Real'].write_value(-1.25, ua.VariantType.Float)
                await nodes['Text'].write_value('Привет', ua.VariantType.String)
                async def output_ready():
                    return simulator.registers.get(10) == 1234 and simulator.registers.get(11) == 0xbfa0 and 1234 in changes.values
                await eventually(output_ready)
                assert await nodes['Text'].read_value() == 'Привет'
                changes.values.clear()
                await nodes['Command'].write_value(2345, ua.VariantType.UInt16)
                async def changed():
                    return 2345 in changes.values and simulator.registers.get(10) == 2345
                await eventually(changed)
                assert 7 not in changes.values, 'Write admission emitted initial cached value'
                for name, value, kind in [('Input', 5, ua.VariantType.UInt16), ('ProcessFlag', False, ua.VariantType.Boolean), ('Real', 2.0, ua.VariantType.Double),
                                          ('Real', float('nan'), ua.VariantType.Float), ('Text', '123456789', ua.VariantType.String)]:
                    try:
                        await nodes[name].write_value(value, kind)
                    except ua.UaStatusCodeError:
                        pass
                    else:
                        raise AssertionError(f'Invalid write accepted: {name}')
                if args.snap7:
                    assert await asyncio.to_thread(snap7_read, args.snap7, s7_port) == struct.pack('>HH', 42, 2345)
                simulator.failed = True
                async def offline():
                    value = await nodes['Input'].read_data_value(raise_on_bad_status=False)
                    return value.StatusCode.value == ua.StatusCodes.BadNoCommunication
                await eventually(offline)
                simulator.failed = False
                await eventually(input_ready)
                assert simulator.connections >= 2
                await subscription.delete()
            await asyncio.wait_for(process.wait(), 65)
            assert process.returncode == 0
        print('PASS: UA browsing, reads, writes, subscriptions and validation; Modbus input/output/reconnect/quality; concurrent Snap7; finite shutdown.')
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        mb_server.close()
        await mb_server.wait_closed()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--snap7', type=Path)
    asyncio.run(main(parser.parse_args()))
