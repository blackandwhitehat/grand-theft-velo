import struct, unittest
from gtv import parsers as p


class Parsers(unittest.TestCase):
    def test_cps_cadence(self):
        c = p.CrankCadence()
        p.parse_cycling_power(struct.pack("<HhHH", 0x20, 180, 10, 1000), c)
        w, rpm = p.parse_cycling_power(struct.pack("<HhHH", 0x20, 200, 12, 2024), c)
        self.assertEqual((w, round(rpm)), (200, 120))

    def test_cps_rollover_and_wheel_offset(self):
        c = p.CrankCadence()
        p.parse_cycling_power(struct.pack("<HhHH", 0x20, 0, 65535, 65000), c)
        _, rpm = p.parse_cycling_power(struct.pack("<HhHH", 0x20, 150, 0, (65000 + 512) & 0xFFFF), c)
        self.assertEqual(round(rpm), 120)
        c = p.CrankCadence()
        p.parse_cycling_power(struct.pack("<HhIHHH", 0x30, 100, 5, 7, 20, 100), c)
        _, rpm = p.parse_cycling_power(struct.pack("<HhIHHH", 0x30, 110, 6, 9, 21, 783), c)
        self.assertEqual(round(rpm), 90)

    def test_cps_stops_and_negative_power(self):
        c = p.CrankCadence()
        for _ in range(2):
            p.parse_cycling_power(struct.pack("<HhHH", 0x20, 100, 5, 500), c)
        p.parse_cycling_power(struct.pack("<HhHH", 0x20, 100, 6, 1012), c)
        self.assertEqual(round(c.rpm), 120)
        for _ in range(5):
            w, rpm = p.parse_cycling_power(struct.pack("<HhHH", 0x20, -3, 6, 1012), c)
        self.assertEqual((w, rpm), (0, 0.0))
        self.assertEqual(p.parse_cycling_power(struct.pack("<Hh", 0, 250))[1], None)

    def test_indoor_bike(self):
        # speed present (bit0 clear), cadence (bit2), power (bit6)
        d = struct.pack("<HHHh", 0x44, 3250, 180, 210)
        self.assertEqual(p.parse_indoor_bike(d), {"speed_kmh": 32.5, "cadence": 90.0, "power": 210})
        # "more data" set (bit 0): no speed; cadence, distance (3 bytes) and resistance come before power
        d = struct.pack("<HH", 0x75, 170) + b"\x01\x02\x03" + struct.pack("<hh", 5, 300)
        self.assertEqual(p.parse_indoor_bike(d), {"cadence": 85.0, "power": 300})

    def test_csc(self):
        c = p.CrankCadence()
        p.parse_csc(bytes((0x03,)) + struct.pack("<IHHH", 1, 2, 10, 0), c)
        self.assertEqual(round(p.parse_csc(bytes((0x03,)) + struct.pack("<IHHH", 2, 3, 11, 768), c)), 80)

    def test_sterzo(self):
        self.assertEqual(p.sterzo_response(0), 0x9696)
        self.assertTrue(all(0 <= p.sterzo_response(c) < 65336 for c in (1, 255, 4660, 65535)))

    def test_control_frames(self):
        self.assertEqual(p.ftms_simulation(5.0).hex(" "), "11 00 00 f4 01 28 33")
        self.assertEqual(p.ftms_simulation(-3.0).hex(" "), "11 00 00 d4 fe 28 33")
        self.assertEqual(p.fec_track_resistance(0).hex(" "), "a4 09 4f 05 33 ff ff ff ff 20 4e 50 ea")
        self.assertEqual(p.fec_track_resistance(5).hex(" "), "a4 09 4f 05 33 ff ff ff ff 14 50 50 c0")

    def test_classify(self):
        self.assertEqual(p.classify("STERZO", []), "steering")
        self.assertEqual(p.classify("Tacx Neo 12345", [p.CPS_SERVICE]), "trainer")
        self.assertEqual(p.classify("KICKR CORE 1234", [p.FTMS_SERVICE, p.CPS_SERVICE]), "trainer")
        self.assertEqual(p.classify("Assioma UNO", [p.CPS_SERVICE]), "power")
        self.assertEqual(p.classify("Wahoo CADENCE 55", [p.CSC_SERVICE]), "cadence")
        self.assertEqual(p.classify("Govee_H6072", []), None)


if __name__ == "__main__":
    unittest.main()


class HeartAndRides(unittest.TestCase):
    def test_heart_rate(self):
        self.assertEqual(p.parse_heart_rate(bytes((0x00, 142))), 142)
        self.assertEqual(p.parse_heart_rate(bytes((0x01, 0x2C, 0x01))), 300)
        self.assertEqual(p.classify("Polar H10 1234", []), "heart")
        self.assertEqual(p.classify("x", [p.HR_SERVICE]), "heart")

    def test_flat_speed(self):
        from gtv import rides
        self.assertEqual(rides.flat_speed(0), 0)
        self.assertTrue(8.0 < rides.flat_speed(200) < 10.0)          # about 32 km/h at 200 W
        self.assertTrue(rides.flat_speed(300) > rides.flat_speed(200))

    def test_record_and_export(self):
        import os, tempfile, xml.dom.minidom
        from gtv import rides
        rides.DIR = tempfile.mkdtemp()
        r = rides.Recorder(lambda _: None)
        r.start(); rid = r.id
        for i in range(40):
            r.add(200, 90, 150 if i > 5 else 0)
        r.add(0, 0, 150)                                             # not pedaling: left out
        s = r.stop()
        self.assertEqual((s["moving_s"], s["avg_watts"], s["avg_hr"]), (40, 200, 150))
        doc = xml.dom.minidom.parseString(rides.tcx(rid))
        self.assertEqual(len(doc.getElementsByTagName("Trackpoint")), 40)
        self.assertEqual(rides.list_rides()[0]["id"], rid)
        with self.assertRaises(ValueError):
            rides.read("../../etc/passwd")
        r.start(); r.add(100, 80, 0); self.assertIsNone(r.stop())    # too short: thrown away
        self.assertEqual(len(os.listdir(rides.DIR)), 1)


class ControllerSteering(unittest.TestCase):
    def run_yaw(self, gravity, axis, bias=(0.4, -0.3, 0.2)):
        from gtv.gamepad import Yaw
        y, t = Yaw(), 0.0
        still = lambda: [b + 0.0 for b in bias]
        for _ in range(400):                                        # 1.6 s at rest: learns the offset
            t += 0.004; y.update(gravity, still(), t)
        self.assertIsNotNone(y.bias); self.assertAlmostEqual(y.angle, 0.0, places=1)
        for _ in range(250):                                        # 1 s turning at 20 degrees a second
            t += 0.004; y.update(gravity, [b + 20.0 * a for b, a in zip(bias, axis)], t)
        return y, t

    def test_turn_is_measured_whichever_way_up(self):
        for gravity, axis in (((0, 1, 0), (0, 1, 0)), ((0, 0, -1), (0, 0, -1)), ((0.6, 0.8, 0), (0.6, 0.8, 0))):
            y, _ = self.run_yaw(gravity, axis)
            self.assertTrue(18.5 < y.angle < 20.5, y.angle)

    def test_drift_free_at_rest_and_returns_to_straight(self):
        y, t = self.run_yaw((0, 1, 0), (0, 1, 0))
        a0 = y.angle
        for _ in range(250 * 60):                                   # a minute holding still
            t += 0.004; y.update((0, 1, 0), [0.4, -0.3, 0.2], t)
        self.assertTrue(0 < y.angle < a0 * 0.35, y.angle)

    def test_tilting_is_not_steering(self):
        y, _ = self.run_yaw((0, 1, 0), (1, 0, 0))                   # rotation about an axis across gravity
        self.assertAlmostEqual(y.angle, 0.0, places=1)
