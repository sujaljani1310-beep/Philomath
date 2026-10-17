"""Loopback-only synthetic backend for check_browser_workflows.py, never staging."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import socket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Require the parent to launch a fresh, credential-free process.
    if any(value for name, value in os.environ.items() if any(part in name for part in
            ("SUPABASE", "STAGING", "API_KEY", "ENCRYPTION_KEY"))):
        raise SystemExit("Synthetic server refuses ambient service credentials")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["PHILOMATH_PERFORMANCE_LOGGING"] = "false"
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Outbound connections are forbidden in the synthetic backend")
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden
    from tests.workflow_fixture import WorkflowFixture

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    with WorkflowFixture() as fixture:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def dispatch(self):
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 21 * 1024 * 1024:
                    self.send_error(413)
                    return
                response = fixture.http.request(self.command, self.path,
                    headers={k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")},
                    content=self.rfile.read(length) if length else b"")
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in ("content-length", "transfer-encoding", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            do_GET = do_POST = do_DELETE = do_OPTIONS = dispatch

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        server.daemon_threads = True
        try:
            server.serve_forever(poll_interval=0.1)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    print(json.dumps({"synthetic_server_closed": True, "active_leases": fixture.controller.active,
                      "database_pool_closed": fixture.db.http.is_closed,
                      "async_pools_closed": all(c.is_closed for c in fixture.async_clients)}))


if __name__ == "__main__":
    main()
