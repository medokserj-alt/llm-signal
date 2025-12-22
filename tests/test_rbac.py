import os
import unittest
from unittest import mock

import rbac


class TestRbac(unittest.TestCase):
    def test_analysis_menu_layout_gated(self):
        viewer_rows = rbac.analysis_menu_layout(is_admin_user=False)
        viewer_flat = [x for row in viewer_rows for x in row]
        self.assertIn("📈 Current", viewer_flat)
        self.assertNotIn("🗓 DAY", viewer_flat)
        self.assertNotIn("📰 MID", viewer_flat)

        admin_rows = rbac.analysis_menu_layout(is_admin_user=True)
        admin_flat = [x for row in admin_rows for x in row]
        self.assertIn("🗓 DAY", admin_flat)
        self.assertIn("📰 MID", admin_flat)

    def test_is_admin_parses_env(self):
        with mock.patch.dict(os.environ, {"TG_ADMIN_IDS": "123, 456;789"}):
            self.assertTrue(rbac.is_admin(123))
            self.assertTrue(rbac.is_admin(456))
            self.assertTrue(rbac.is_admin(789))
            self.assertFalse(rbac.is_admin(1))

    def test_is_admin_falls_back_to_constant(self):
        with mock.patch.dict(os.environ, {"TG_ADMIN_IDS": ""}):
            with mock.patch.object(rbac, "ADMIN_USER_IDS", {999}):
                self.assertTrue(rbac.is_admin(999))
                self.assertFalse(rbac.is_admin(1000))


if __name__ == "__main__":
    unittest.main()

