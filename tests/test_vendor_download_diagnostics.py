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
        return download.collect_aria2(root="/fixture", ownership_check=self.owner, socket_factory=self.connect, **kwargs)


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
        for status in ["proc_unavailable", "proc_partial", "not_listening", "owner_unverified", "ambiguous"]:
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
                self.assert_unavailable(download.collect_aria2(ownership_check=lambda **_: OWNER), status)
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
            value = download.collect_aria2(ownership_check=lambda **_: OWNER, socket_factory=connect)
            self.assertTrue(value["available"])
            self.assertEqual(value["data"]["tasks"][0]["completed_bytes"], 25)
            self.assertEqual([path for path, _ in requests], ["/jsonrpc", "/jsonrpc"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def qb_task(**changes):
    row = {"hash": "a" * 40, "state": "downloading", "size": 100, "completed": 25,
           "progress": 0.25, "dlspeed": 12, "upspeed": 0}
    row.update(changes)
    return row


class QbHarness:
    def __init__(self, tasks=None, transfer=None, owners=None):
        self.sockets = [FakeSocket(frame(b"v5.1.4")),
                        FakeSocket(frame(transfer if transfer is not None else {
                            "dl_info_speed": 12, "up_info_speed": 0, "connection_status": "connected"})),
                        FakeSocket(frame(tasks if tasks is not None else [qb_task()]))]
        self.owners = owners or [OWNER] * 4
        self.owner_calls, self.calls = [], []
        self.auth_state = download.QbAuthState()

    def connect(self, address, timeout):
        self.calls.append((address, timeout))
        return self.sockets[len(self.calls) - 1]

    def owner(self, **kwargs):
        self.owner_calls.append(kwargs)
        return self.owners[min(len(self.owner_calls) - 1, len(self.owners) - 1)]

    def collect(self, **kwargs):
        kwargs.setdefault('auth_state', self.auth_state)
        kwargs.setdefault('credentials', ('fixture-user', 'fixture-password'))
        return download.collect_qbittorrent(root="/fixture", ownership_check=self.owner,
                                           socket_factory=self.connect, **kwargs)


class QbittorrentDiagnosticsTests(unittest.TestCase):
    def test_fixed_three_gets_return_projected_tasks_and_real_zero_without_login(self):
        row = qb_task(name="PRIVATE-FILE", save_path="/private", magnet_uri="magnet:private",
                      tracker="http://private", token="private-token")
        harness = QbHarness(tasks=[row])
        with patch("builtins.open", side_effect=AssertionError("No config/credential reads")), \
                patch.dict(os.environ, {"HTTP_PROXY": "http://forbidden", "ALL_PROXY": "http://forbidden"}):
            value = harness.collect()
        self.assertTrue(value["available"], value)
        self.assertEqual(value["data"]["engine"], "qbittorrent")
        self.assertEqual(value["data"]["version"], "v5.1.4")
        self.assertEqual(value["data"]["counts"], {"tasks": 1})
        self.assertEqual(value["data"]["tasks"][0]["progress_percent"], 25.0)
        self.assertEqual(value["data"]["tasks"][0]["upload_bytes_per_second"], 0)
        self.assertFalse(value["data"]["complete"])
        # Initial identity plus an actual pre-send and post-response check for
        # each connection, including the time spent inside connect().
        self.assertEqual(len(harness.owner_calls), 7)
        self.assertTrue(all(call == {"root": "/fixture", "port": 8181, "comm": "qbittorrent-nox"}
                            for call in harness.owner_calls))
        self.assertEqual([address for address, _ in harness.calls], [("127.0.0.1", 8181)] * 3)
        for sock, path in zip(harness.sockets, download.QB_PATHS):
            self.assertTrue(sock.sent.startswith(f"GET {path} HTTP/1.1\r\n".encode()))
            self.assertIn(b"Host: 127.0.0.1:8181\r\n", sock.sent)
            self.assertIn(b"Referer: http://127.0.0.1:8181/\r\n", sock.sent)
            self.assertNotIn(b"Cookie:", sock.sent)
            self.assertNotIn(b"Authorization:", sock.sent)
            self.assertTrue(sock.closed)
        rendered = json.dumps(value)
        for secret in ("PRIVATE-FILE", "/private", "magnet:", "http://private", "private-token", "a" * 40):
            self.assertNotIn(secret, rendered)

    def test_auth_rejection_attempts_once_without_reading_config_or_exposing_vendor_text(self):
        for index in range(3):
            for status in (401, 403):
                harness = QbHarness()
                harness.sockets[index].response = frame(b"PRIVATE-ERROR", status=status)
                harness.sockets.insert(index + 1, FakeSocket(frame(b'Fails.')))
                with patch('builtins.open', side_effect=AssertionError('No config or credential file')):
                    value = harness.collect()
                self.assertFalse(value["available"])
                self.assertEqual(value["status"], "auth_rejected")
                self.assertIsNone(value["data"])
                self.assertEqual(len(harness.calls), index + 2)
                self.assertTrue(harness.sockets[index + 1].sent.startswith(b'POST /api/v2/auth/login HTTP/1.1\r\n'))
                self.assertNotIn("PRIVATE-ERROR", json.dumps(value))

    def test_verified_ipv6_listener_uses_fixed_loopback_and_bracketed_same_origin(self):
        harness = QbHarness(owners=[dict(OWNER, connect_host='::1')] * 4)
        value = harness.collect()
        self.assertTrue(value['available'], value)
        self.assertEqual([address for address, _ in harness.calls], [('::1', 8181)] * 3)
        for sock in harness.sockets:
            self.assertIn(b'Host: [::1]:8181\r\n', sock.sent)
            self.assertIn(b'Referer: http://[::1]:8181/\r\n', sock.sent)
        harness = QbHarness(owners=[dict(OWNER, connect_host='192.0.2.1')])
        value = harness.collect()
        self.assertEqual(value['status'], 'owner_unverified')
        self.assertEqual(harness.calls, [])

    def test_listener_address_change_during_query_discards_data(self):
        harness = QbHarness(owners=[dict(OWNER, connect_host='::1'), OWNER])
        self.assertEqual(harness.collect()['status'], 'identity_changed')
        self.assertEqual(len(harness.calls), 1)

    def test_partial_or_missing_identity_cannot_authorize_qb_connection(self):
        for status in ("proc_unavailable", "proc_partial", "not_listening", "owner_unverified", "ambiguous"):
            harness = QbHarness(owners=[{"available": False, "status": status}])
            value = harness.collect()
            self.assertFalse(value["available"])
            self.assertEqual(value["status"], status)
            self.assertEqual(harness.calls, [])

    def test_owner_change_after_each_stage_discards_all_data(self):
        for index in range(1, 4):
            harness = QbHarness(owners=[OWNER] * (index * 2) + [dict(OWNER, socket_inode="99999")])
            value = harness.collect()
            self.assertEqual(value["status"], "identity_changed")
            self.assertIsNone(value["data"])
            self.assertEqual(len(harness.calls), index)

    def test_missing_measurements_unknown_state_and_zero_size_stay_unknown(self):
        row = qb_task(state="FUTURE-STATE", size=None, completed=None, progress=None, dlspeed=None)
        value = QbHarness(tasks=[row], transfer={}).collect()
        self.assertTrue(value["available"])
        task = value["data"]["tasks"][0]
        for key in ("status", "total_bytes", "completed_bytes", "progress_percent", "download_bytes_per_second"):
            self.assertIsNone(task[key])
        self.assertIsNone(value["data"]["download_bytes_per_second"])
        self.assertIsNone(value["data"]["connection_status"])
        zero = QbHarness(tasks=[qb_task(size=0, completed=0, progress=0)]).collect()["data"]["tasks"][0]
        self.assertEqual(zero["total_bytes"], 0)
        self.assertEqual(zero["progress_percent"], 0)

    def test_torrent_limit_duplicate_hash_and_invalid_measurements_fail_closed(self):
        rows = [qb_task(hash=f"{index:040x}") for index in range(129)]
        self.assertEqual(QbHarness(tasks=rows).collect()["status"], "limit_exceeded")
        self.assertEqual(QbHarness(tasks=[qb_task(), qb_task()]).collect()["status"], "invalid_response")
        for change in ({"size": True}, {"completed": 101}, {"progress": 1.01}, {"progress": True},
                       {"progress": float("nan")}, {"progress": 10 ** 400}, {"dlspeed": -1}, {"hash": "PRIVATE-ID"}):
            value = QbHarness(tasks=[qb_task(**change)]).collect()
            self.assertEqual(value["status"], "invalid_response", change)
            self.assertIsNone(value["data"])

    def test_genuine_empty_list_requires_all_three_valid_responses(self):
        harness = QbHarness(tasks=[])
        value = harness.collect()
        self.assertTrue(value["available"])
        self.assertEqual(value["data"]["tasks"], [])
        self.assertEqual(value["data"]["counts"], {"tasks": 0})
        self.assertEqual(len(harness.calls), 3)
        harness = QbHarness(tasks=[])
        harness.sockets[0].response = frame(b"<html>Login</html>")
        self.assertEqual(harness.collect()["status"], "invalid_response")
        self.assertEqual(len(harness.calls), 1)

    def test_qb_http_rejects_redirects_compression_unbounded_and_duplicate_json(self):
        for response in (frame(b"", status=302, headers=b"Location: http://private\r\n"),
                         frame(b"v5.1.4", headers=b"Content-Encoding: gzip\r\n"),
                         frame(b"v5.1.4", headers=b"Transfer-Encoding: chunked\r\n"),
                         b"HTTP/1.1 200 OK\r\nContent-Length: 4097\r\n\r\n"):
            harness = QbHarness()
            harness.sockets[0].response = response
            value = harness.collect()
            self.assertIn(value["status"], ("invalid_response", "limit_exceeded"))
            self.assertIsNone(value["data"])
        harness = QbHarness()
        harness.sockets[1].response = frame(b'{"dl_info_speed":0,"dl_info_speed":1}')
        self.assertEqual(harness.collect()["status"], "invalid_response")

    def test_no_arbitrary_qb_path_can_be_requested(self):
        with self.assertRaises(ValueError):
            download._qb_get("/api/v2/torrents/resume", 1, lambda: 0,
                             lambda *_a, **_kw: self.fail("must not connect"))

    def test_qb_absolute_network_deadline_applies_to_all_three_requests(self):
        harness = QbHarness()
        now = [0.0]
        original = harness.owner

        def owner(**kwargs):
            value = original(**kwargs)
            if len(harness.owner_calls) == 5:
                now[0] = 4.1
            return value

        value = download.collect_qbittorrent(ownership_check=owner, socket_factory=harness.connect,
                                             monotonic=lambda: now[0], auth_state=harness.auth_state,
                                             credentials=('fixture-user', 'fixture-password'))
        self.assertEqual(value["status"], "timeout")
        self.assertEqual(len(harness.calls), 2)

    def test_qb_real_loopback_http_fixture_uses_only_get_and_bounded_projection(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Referer'), self.headers.get('Cookie')))
                if self.path == download.QB_PATHS[0]:
                    body = b'v5.1.4'
                elif self.path == download.QB_PATHS[1]:
                    body = b'{"dl_info_speed":12,"up_info_speed":0}'
                else:
                    body = json.dumps([qb_task(name='PRIVATE-FILE')]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def connect(address, timeout):
                self.assertEqual(address, ('127.0.0.1', 8181))
                return socket.create_connection(server.server_address, timeout=timeout)
            value = download.collect_qbittorrent(ownership_check=lambda **_: OWNER, socket_factory=connect,
                                                 auth_state=download.QbAuthState(),
                                                 credentials=('fixture-user', 'fixture-password'))
            self.assertTrue(value['available'], value)
            self.assertEqual([path for path, _, _ in requests], list(download.QB_PATHS))
            self.assertTrue(all(referer == 'http://127.0.0.1:8181/' and cookie is None
                                for _, referer, cookie in requests))
            self.assertNotIn('PRIVATE-FILE', json.dumps(value))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class DownloadEngineAggregationTests(unittest.TestCase):
    def test_failure_isolated_and_never_invents_cross_engine_totals(self):
        failure = {"available": False, "status": "proc_partial", "source": download.SOURCE,
                   "reason": download.REASONS["proc_partial"], "data": None}
        success = QbHarness(tasks=[]).collect()
        value = download.collect(root="/fixture", aria2=lambda **_: failure, qbittorrent=lambda **_: success)
        self.assertTrue(value["available"])
        self.assertEqual([row["status"] for row in value["engines"]], ["proc_partial", "ok"])
        self.assertFalse(value["data"]["complete"])
        self.assertNotIn("counts", value["data"])
        self.assertIsNone(value["data"]["engines"][0]["data"])
        self.assertEqual(value["data"]["engines"][1]["data"]["counts"], {"tasks": 0})

    def test_all_failures_still_retain_per_engine_status_but_no_empty_success(self):
        failed = {"available": False, "status": "auth_required", "source": download.QB_SOURCE,
                  "reason": download.REASONS["auth_required"], "data": None}

        def broken(**_):
            raise OSError("PRIVATE-PASSWORD-OR-PATH")

        value = download.collect(aria2=broken, qbittorrent=lambda **_: failed)
        self.assertFalse(value["available"])
        self.assertEqual(value["status"], "no_readable_engine")
        self.assertIsNone(value["data"])
        self.assertEqual([row["status"] for row in value["engines"]], ["diagnostic_error", "auth_required"])
        self.assertNotIn("PRIVATE", json.dumps(value))

    def test_malformed_source_is_isolated_and_top_level_extras_never_escape(self):
        for malformed in (None, {}, {'available': True}, {'available': False, 'status': 'PRIVATE'}):
            success = QbHarness(tasks=[]).collect()
            success.update(password='PRIVATE', engine='FORGED', source='PRIVATE-SOURCE', reason='PRIVATE-REASON')
            value = download.collect(aria2=lambda **_: malformed, qbittorrent=lambda **_: success)
            self.assertTrue(value['available'])
            self.assertEqual(value['engines'][0]['status'], 'diagnostic_error')
            self.assertEqual(value['engines'][1]['engine'], 'qbittorrent')
            self.assertNotIn('PRIVATE', json.dumps(value))


if __name__ == "__main__":
    unittest.main()
