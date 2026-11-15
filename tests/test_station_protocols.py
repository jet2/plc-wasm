import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from station_protocols import devices_from_uso, parse_uso, station_files


class StationProtocolsTests(unittest.TestCase):
    def setUp(self):
        self.ini = parse_uso('[Slaves]\n2=test\n[2]\n9=0,0,32,0\n1=0,1,2,0\n3=1,0,0,16\n'
                             '[2.9]\n0=Sensor\n17=Neighbor\n[2.1]\n1=Analog\n[2.3]\n4=Command\n')
        self.table = [dict(name=n, address=a, type=t) for n, a, t in
                      [('Sensor', 'I0.0', 'BOOL'), ('Neighbor', 'I0.1', 'BOOL'),
                       ('Analog', 'IW2', 'INT'), ('Command', 'Q0.0', 'BOOL')]]
        self.mapping = dict(signals=[dict(name='IO.' + s['name'].upper(), offset=i * 4, kind=s['type'])
                                    for i, s in enumerate(self.table)])

    def test_module_order_spaces_and_endpoints(self):
        device = devices_from_uso(self.ini, self.table, self.mapping)[0]
        self.assertEqual((device['address'], device['port'], device['unit']), ('127.0.0.1', 15002, 2))
        self.assertEqual(device['requests'][0]['fields'], ['IO.SENSOR'])
        self.assertEqual([(r['operation'], r['address']) for r in device['requests']],
                         [('read_discrete_inputs', 0), ('read_discrete_inputs', 17),
                          ('read_input_registers', 3), ('write_coils', 4)])
        for overrides, expected in [({'slaves_ip': {'2': '192.168.77.88'}}, ('192.168.77.88', 15002)),
                                    ({'slaves_port': {'2': '17002'}}, ('127.0.0.1', 17002)),
                                    ({'slaves_ip': {'2': '192.168.77.88'}, 'slaves_port': {'2': '17002'}},
                                     ('192.168.77.88', 17002))]:
            device = devices_from_uso(self.ini | overrides, self.table, self.mapping)[0]
            self.assertEqual((device['address'], device['port']), expected)

    def test_rejects_bad_addresses_missing_signals_and_wrong_direction(self):
        for extra in ({'slaves_port': {'2': '0'}}, {'slaves_ip': {'2': ''}}, {'slaves_port': {'77': '17077'}}):
            with self.assertRaises(ValueError):
                devices_from_uso(self.ini | extra, self.table, self.mapping)
        with self.assertRaises(ValueError):
            devices_from_uso(self.ini, self.table, {'signals': []})
        table = copy.deepcopy(self.table)
        table[0]['address'] = 'Q0.0'
        with self.assertRaises(ValueError):
            devices_from_uso(self.ini, table, self.mapping)

    def test_station_pair_and_multiple_mpr_alarm(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / 'Example.MPR').touch()
            (folder / 'example.uso').touch()
            self.assertEqual(station_files(folder)[1].name, 'example.uso')
            (folder / 'other.mpr').touch()
            with self.assertRaisesRegex(ValueError, 'ALARM'):
                station_files(folder)


if __name__ == '__main__':
    unittest.main()
