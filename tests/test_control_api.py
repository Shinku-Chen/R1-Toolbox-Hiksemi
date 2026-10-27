"""Privileged API boundary tests. No real controller, Docker or power commands."""

import http.client
import json
import threading
import types
import unittest
from unittest import mock

from test_ui_service import service


class ControlAPITests(unittest.TestCase):
    def setUp(self):
        self.token = "fixture-token-not-a-deployment-secret"
        self.controller = mock.Mock()
        self.controller.snapshot.return_value = {"capabilities": {}, "settings": {"theme": "dark"}}
        self.controller.action.return_value = {"ok": True, "state": {"power": {"pending": False}}}
        self.insights = mock.Mock()
        self.insights.snapshot.return_value = {"containers": [], "system": {}}
        self.insights.action.return_value = {"ok": True}
        self.web = mock.Mock()
        self.web.snapshot.return_value = {"enabled": True, "port": 18732, "addresses": []}
        self.web.pairing_info.return_value = {"code": "12345", "expires_in": 180, "port": 18732}
        self.policy = mock.Mock()
        self.policy.snapshot.return_value = {"active": False, "busy": False, "recovery_confirmed": True}
        self.policy.action.return_value = {"ok": True, "settings_saved": True, "pending": True}
        monitor = types.SimpleNamespace(snapshot=lambda: {"schema_version": 1, "available": True})
        self.server = service.make_http_server(monitor, 0, self.controller, self.insights, self.token,
                                              web_config=self.web, fan_policy=self.policy)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def request(self, method="GET", path="/api/action", body=None, headers=None):
        values = {"Origin": "null", **(headers or {})}
        if body is not None and not isinstance(body, str):
            body = json.dumps(body)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=values)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None
        finally:
            connection.close()

    def action(self, payload, headers=None):
        return self.request("POST", body=payload, headers={
            "Content-Type": "application/json", "X-R1-Token": self.token, **(headers or {}),
        })

    def test_read_routes_do_not_disclose_the_control_token(self):
        for path in ("/api/status", "/api/toolbox", "/api/insights", "/api/web-config", "/api/fan-policy"):
            code, headers, data = self.request(path=path)
            self.assertEqual(code, 200)
            self.assertEqual(headers["Access-Control-Allow-Origin"], "null")
            self.assertNotIn(self.token, json.dumps(data))
        self.controller.action.assert_not_called()
        self.insights.action.assert_not_called()
        self.web.pairing_info.assert_not_called()

    def test_pairing_code_is_only_created_by_authenticated_local_action(self):
        self.assertEqual(self.action({"action": "web.pairing", "rotate": True},
                                     {"X-R1-Token": "wrong"})[0], 403)
        self.web.pairing_info.assert_not_called()
        for value in ({"rotate": "true"}, {"rotate": 1}, {"command": "anything"}):
            self.assertEqual(self.action({"action": "web.pairing", **value})[0], 400)
        self.web.pairing_info.assert_not_called()
        code, _, data = self.action({"action": "web.pairing", "rotate": True})
        self.assertEqual(code, 200)
        self.assertEqual(data["web"]["code"], "12345")
        self.web.pairing_info.assert_called_once_with(rotate=True)
        self.controller.action.assert_not_called()
        self.assertNotIn("12345", json.dumps(self.request(path="/api/web-config")[2]))

    def test_custom_policy_dispatch_and_legacy_control_interlock(self):
        payload = {"config": {"fixture": True}, "apply": False}
        self.assertEqual(self.action({"action": "fan.policy.update", **payload})[0], 200)
        self.policy.action.assert_called_once_with("fan.policy.update", payload)
        for state in ({"active": True}, {"busy": True}, {"recovery_confirmed": False}):
            self.policy.snapshot.return_value = state
            for name, values in (("fan.official", {"mode": "low", "confirmed": True}),
                                  ("fan.set", {"channel": "anything"}), ("fan.restore", {})):
                self.assertEqual(self.action({"action": name, **values})[0], 409)
        self.controller.action.assert_not_called()

    def test_missing_or_wrong_token_cannot_control_the_host(self):
        for token in ("", "wrong-token", "0" * 64):
            with self.subTest(token=token):
                code, _, _ = self.action({"action": "screen.sleep"}, {"X-R1-Token": token})
                self.assertEqual(code, 403)
        self.controller.action.assert_not_called()

    def test_cross_origin_or_rebound_host_is_rejected_even_with_a_token(self):
        for headers in ({"Origin": "https://example.invalid"},
                        {"Host": f"example.invalid:{self.port}"},
                        {"Host": "127.0.0.1:1"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.action({"action": "screen.sleep"}, headers)[0], 403)
        self.controller.action.assert_not_called()

    def test_power_and_workload_actions_require_boolean_confirmation(self):
        for action in ("power.request", "container.action", "vm.action", "fan.official"):
            for confirmed in (None, False, "true", 1):
                with self.subTest(action=action, confirmed=confirmed):
                    self.assertEqual(self.action({"action": action, "confirmed": confirmed})[0], 400)
        self.controller.action.assert_not_called()
        self.insights.action.assert_not_called()
        self.assertEqual(self.action({"action": "power.request", "operation": "reboot", "confirmed": True})[0], 200)
        self.controller.action.assert_called_once_with("power.request", {"operation": "reboot", "confirmed": True})
        self.assertEqual(self.action({"action": "container.action", "id": "a" * 12,
                                     "operation": "stop", "confirmed": True})[0], 200)
        self.insights.action.assert_called_once()

    def test_allowlisted_actions_are_dispatched_without_shell_or_path_parameters(self):
        payload = {"action": "settings.update", "settings": {"theme": "jade"}}
        self.assertEqual(self.action(payload)[0], 200)
        self.controller.action.assert_called_once_with("settings.update", {"settings": {"theme": "jade"}})
        for name in ("shell", "exec", "systemctl", "container.remove", "../screen.sleep"):
            self.assertEqual(self.action({"action": name})[0], 400)
        self.assertEqual(self.controller.action.call_count, 1)

    def test_malformed_duplicate_or_oversized_json_is_rejected(self):
        for value in ("{", "[]", '{"action":"screen.sleep","action":"power.request"}'):
            self.assertEqual(self.action(value)[0], 400)
        self.assertEqual(self.action("x" * 16385)[0], 413)
        self.assertEqual(self.action({"action": "screen.sleep"}, {"Content-Type": "text/plain"})[0], 415)
        self.controller.action.assert_not_called()

    def test_control_preflight_grants_only_the_expected_route_and_headers(self):
        headers = {"Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type, x-r1-token",
                   "Access-Control-Request-Private-Network": "true"}
        code, response, _ = self.request("OPTIONS", headers=headers)
        self.assertEqual(code, 204)
        self.assertEqual(response["Access-Control-Allow-Methods"], "POST")
        self.assertEqual(response["Access-Control-Allow-Private-Network"], "true")
        self.assertEqual(self.request("OPTIONS", headers={**headers, "Access-Control-Request-Headers": "authorization"})[0], 403)
        self.assertEqual(self.request("OPTIONS", path="/other", headers=headers)[0], 403)
        self.controller.action.assert_not_called()

    def test_controller_failures_do_not_leak_privileged_command_output(self):
        self.controller.action.side_effect = RuntimeError("fixture private command output")
        code, _, data = self.action({"action": "screen.sleep"})
        self.assertEqual(code, 500)
        self.assertNotIn("fixture private", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
