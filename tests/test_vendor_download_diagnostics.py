import copy
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import vendor_download_diagnostics as download


OWNER = {"available": True, "status": "verified", "pid": 42, "start_ticks": 100, "socket_inode": "12345"}


def version_result():
    return {"jsonrpc": "2.0", "id": "r1-download-version", "result": {"version": "1.37.0", "enabledFeatures": ["BitTorrent"]}}


def task(gid="1" * 16, status="active", total="100", completed="25"):
    return {"gid": gid, "status": status, "totalLength": total, "completedLength": completed,
            "downloadSpeed": "12", "uploadSpeed": "3"}


def queues(active=None, waiting=None, stopped=None):
    return {"jsonrpc": "2.0", "id": "r1-download-queues", "result": [[active or []], [waiting or []], [stopped or []]]}


def frame(value, status=200, headers=b""):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n".encode() + headers + b"\r\n" + body


class FakeSocket:
    def __init__(self, response):
        self.response, self.sent, self.closed, self.timeouts = response, b"", False, []

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, value):
        self.sent += value

    def recv(self, count):
        block, self.response = self.response[:count], self.response[count:]
        return block

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, second=None, first=None, owners=None):
        self.sockets = [FakeSocket(frame(first or version_result())), FakeSocket(frame(second or queues()))]
        self.calls, self.owner_calls = [], []
        self.owners = owners or [copy.deepcopy(OWNER)] * 3

    def connect(self, address, timeout):
        self.calls.append((address, timeout))
        return self.sockets[len(self.calls) - 1]

    def owner(self, **kwargs):
        self.owner_calls.append(kwargs)
        return self.owners[min(len(self.owner_calls) - 1, len(self.owners) - 1)]

    def collect(self, **kwargs):
        return download.collect(root="/fixture", ownership_check=self.owner, socket_factory=self.connect, **kwargs)


class DownloadDiagnosticsTests(unittest.TestCase):
    def assert_unavailable(self, value, status):
        self.assertFalse(value["available"])
        self.assertEqual(value["status"], status)
        self.assertIsNone(value["data"])

    def test_owned_service_returns_only_task_summary_and_real_zero(self):
        row = task()
        row.update(files=[{"path": "/private/secret"}], dir="/private", url="https://secret", token="credential")
        h = Harness(queues(active=[row], waiting=[task("2" * 16, "paused", "0", "0")], stopped=[task("3" * 16, "complete", "10", "10")]))
        with patch("builtins.open", side_effect=AssertionError("No cache/config reads")):
            value = h.collect()
        self.assertTrue(value["available"])
        data = value["data"]
        self.assertEqual(data["counts"], {"active": 1, "waiting": 1, "stopped": 1})
        self.assertEqual(data["tasks"][0]["progress_percent"], 25.0)
        self.assertEqual(data["tasks"][1]["total_bytes"], 0)
        self.assertIsNone(data["tasks"][1]["progress_percent"])
        self.assertFalse(data["complete"])
        rendered = json.dumps(value)
        for secret in ["/private", "https://secret", "credential", '"files"', '"dir"', '"token"']:
            self.assertNotIn(secret, rendered)
        self.assertEqual(len(h.owner_calls), 3)
        self.assertTrue(all(s.closed for s in h.sockets))

    def test_wire_requests_are_fixed_and_never_read_secrets_or_proxy(self):
        h = Harness()
        with patch.dict(os.environ, {"HTTP_PROXY": "http://forbidden", "ALL_PROXY": "http://forbidden"}):
            self.assertTrue(h.collect()["available"])
        self.assertTrue(all(a == ("127.0.0.1", 6800) for a, _ in h.calls))
        requests = [json.loads(s.sent.split(b"\r\n\r\n", 1)[1]) for s in h.sockets]
        self.assertEqual(requests[0], {"jsonrpc": "2.0", "id": "r1-download-version", "method": "aria2.getVersion", "params": []})
        methods = requests[1]["params"][0]
        self.assertEqual([m["methodName"] for m in methods], ["aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"])
        self.assertEqual(methods[1]["params"], [0, 128, list(download.KEYS)])
        self.assertEqual(methods[2]["params"], [0, 128, list(download.KEYS)])
        for s in h.sockets:
            self.assertTrue(s.sent.startswith(b"POST /jsonrpc HTTP/1.1\r\nHost: 127.0.0.1:6800\r\n"))
            self.assertNotIn(b"Authorization", s.sent)
            self.assertNotIn(b"token:", s.sent)
        self.assertTrue(all(c == {"root": "/fixture", "port": 6800, "comm": "aria2c-nox"} for c in h.owner_calls))

    def test_missing_listener_never_connects_and_does_not_fake_empty(self):
        for status in ["proc_unavailable", "not_listening", "owner_unverified", "ambiguous"]:
            h = Harness(owners=[{"available": False, "status": status, "reason": "/secret/path"}])
            self.assert_unavailable(h.collect(), status)
            self.assertEqual(h.calls, [])

    def test_unverified_identity_cannot_authorize_a_connection(self):
        for change in [{"pid": True}, {"start_ticks": None}, {"socket_inode": "socket:[1]"}, {"status": "ok"}, {"available": 1}]:
            owner = dict(OWNER, **change)
            h = Harness(owners=[owner])
            self.assert_unavailable(h.collect(), "owner_unverified")
            self.assertEqual(h.calls, [])

    def test_identity_changes_after_version_prevent_task_query(self):
        for key, value in [("pid", 43), ("start_ticks", 101), ("socket_inode", "12346")]:
            h = Harness(owners=[OWNER, dict(OWNER, **{key: value})])
            self.assert_unavailable(h.collect(), "identity_changed")
            self.assertEqual(len(h.calls), 1)

    def test_identity_changes_after_tasks_discard_snapshot(self):
        h = Harness(queues(active=[task()]), owners=[OWNER, OWNER, dict(OWNER, pid=43)])
        self.assert_unavailable(h.collect(), "identity_changed")

    def test_authorization_errors_do_not_login_retry_or_expose_message(self):
        h = Harness(first={"jsonrpc": "2.0", "id": "r1-download-version", "error": {"code": 1, "message": "Unauthorized"}})
        self.assert_unavailable(h.collect(), "auth_required")
        self.assertEqual(len(h.calls), 1)
        h = Harness()
        h.sockets[0].response = frame(b"secret body", status=401)
        self.assert_unavailable(h.collect(), "auth_required")
        self.assertEqual(len(h.calls), 1)

    def test_non_auth_rpc_error_is_not_misreported_as_auth_required(self):
        h = Harness(first={"jsonrpc": "2.0", "id": "r1-download-version", "error": {"code": 1, "message": "private URL http://secret"}})
        value = h.collect()
        self.assert_unavailable(value, "rpc_error")
        self.assertNotIn("secret", json.dumps(value))

    def test_multicall_auth_and_other_faults_are_fail_closed(self):
        for message, expected in [("Unauthorized", "auth_required"), ("No such method", "rpc_error")]:
            value = queues()
            value["result"][1] = {"code": 1, "message": message}
            self.assert_unavailable(Harness(value).collect(), expected)

    def test_redirect_compression_transfer_encoding_and_duplicate_headers_rejected(self):
        variants = [frame(b"", status=302, headers=b"Location: http://secret\r\n"),
                    frame(version_result(), headers=b"Content-Encoding: gzip\r\n"),
                    frame(version_result(), headers=b"Transfer-Encoding: chunked\r\n"),
                    frame(version_result(), headers=b"Content-Length: 1\r\n")]
        for response in variants:
            h = Harness()
            h.sockets[0].response = response
            self.assert_unavailable(h.collect(), "invalid_response")
            self.assertEqual(len(h.calls), 1)
            self.assertTrue(h.sockets[0].closed)

    def test_header_and_body_limits_and_truncated_frame(self):
        variants = [(b"HTTP/1.1 200 OK\r\nX: " + b"a" * 9000, "limit_exceeded"),
                    (b"HTTP/1.1 200 OK\r\nContent-Length: 131073\r\n\r\n", "limit_exceeded"),
                    (b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n{}", "invalid_response")]
        for response, status in variants:
            h = Harness()
            h.sockets[0].response = response
            self.assert_unavailable(h.collect(), status)

    def test_json_duplicates_nan_and_wrong_id_fail(self):
        for raw in [b'{"jsonrpc":"2.0","id":"r1-download-version","result":{},"result":{}}',
                    b'{"jsonrpc":"2.0","id":"r1-download-version","result":NaN}', b'[]', b'{"id":"wrong"}']:
            h = Harness()
            h.sockets[0].response = frame(raw)
            self.assert_unavailable(h.collect(), "invalid_response")

    def test_false_version_and_invalid_feature_schema_stop_before_tasks(self):
        for value in [{"version": "not aria2", "enabledFeatures": []}, {"version": "1.37.0", "enabledFeatures": "secret"}]:
            response = version_result()
            response["result"] = value
            h = Harness(first=response)
            self.assert_unavailable(h.collect(), "invalid_response")
            self.assertEqual(len(h.calls), 1)

    def test_real_empty_queues_require_both_successful_rpcs(self):
        h = Harness()
        value = h.collect()
        self.assertTrue(value["available"])
        self.assertEqual(value["data"]["tasks"], [])
        self.assertEqual(value["data"]["counts"], {"active": 0, "waiting": 0, "stopped": 0})
        self.assertEqual(len(h.calls), 2)

    def test_active_queue_over_limit_is_not_silently_truncated(self):
        rows = [task(f"{i:016x}") for i in range(download.MAX_QUEUE + 1)]
        self.assert_unavailable(Harness(queues(active=rows)).collect(), "limit_exceeded")

    def test_invalid_measurements_status_gid_and_missing_fields_reject_snapshot(self):
        for change in [{"totalLength": "-1"}, {"completedLength": "101"}, {"downloadSpeed": True},
                       {"uploadSpeed": "9" * 20}, {"status": "waiting"}, {"gid": "http://secret"}, {"totalLength": None}]:
            row = task()
            row.update(change)
            self.assert_unavailable(Harness(queues(active=[row])).collect(), "invalid_response")
        self.assert_unavailable(Harness(queues(active=[task(), task()])).collect(), "invalid_response")

    def test_network_failure_classification_and_absolute_deadline(self):
        for error, status in [(ConnectionRefusedError(), "service_unavailable"), (OSError(), "transport_error"), (TimeoutError(), "timeout")]:
            with patch.object(socket, "create_connection", side_effect=error):
                self.assert_unavailable(download.collect(ownership_check=lambda **_: OWNER), status)
        h = Harness()
        clock = iter([0.0, 0.0, 0.0, 0.0, 2.1])
        self.assert_unavailable(h.collect(monotonic=lambda: next(clock)), "timeout")
        self.assertTrue(h.sockets[0].closed)

    def test_real_loopback_http_fixture_uses_bounded_projection(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, request))
                value = version_result() if request["method"] == "aria2.getVersion" else queues(active=[task()])
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def connect(address, timeout):
                self.assertEqual(address, ("127.0.0.1", 6800))
                return socket.create_connection(server.server_address, timeout=timeout)
            value = download.collect(ownership_check=lambda **_: OWNER, socket_factory=connect)
            self.assertTrue(value["available"])
            self.assertEqual(value["data"]["tasks"][0]["completed_bytes"], 25)
            self.assertEqual([path for path, _ in requests], ["/jsonrpc", "/jsonrpc"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
