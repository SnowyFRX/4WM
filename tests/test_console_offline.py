import json
import unittest

import console


class OfflineProxyTests(unittest.TestCase):
    def test_state_returns_error_json_not_html(self):
        code, mime, body = console._offline_reply("state")
        self.assertEqual(code, 503)
        self.assertIn("application/json", mime)
        self.assertIn("error", json.loads(body))

    def test_command_returns_error_json_not_html(self):
        code, mime, body = console._offline_reply("command")
        self.assertEqual(code, 503)
        self.assertIn("error", json.loads(body))

    def test_page_still_returns_readable_html(self):
        code, mime, body = console._offline_reply("")
        self.assertEqual(code, 200)
        self.assertIn("text/html", mime)
        self.assertIn("脚本没在运行", body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
