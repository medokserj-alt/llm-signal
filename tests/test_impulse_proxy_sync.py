import unittest

import get_signal_json


class TestImpulseProxySync(unittest.TestCase):
    def test_warnings_impulse_no_exhale_forces_impulse_proxy_true(self) -> None:
        d = {"warnings": ["impulse_no_exhale"], "impulse_proxy": False}
        get_signal_json.sync_impulse_proxy(d)
        self.assertTrue(bool(d.get("impulse_proxy")))

    def test_no_impulse_markers_forces_impulse_proxy_false(self) -> None:
        d = {"warnings": [], "impulse_proxy": True}
        get_signal_json.sync_impulse_proxy(d)
        self.assertFalse(bool(d.get("impulse_proxy")))


if __name__ == "__main__":
    unittest.main()

