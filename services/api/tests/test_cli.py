"""Starting tp-api when something already listens on its port."""

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tp_api.cli import already_serving


class Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/api/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def tp_api_running() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_another_tp_api_is_named_with_how_to_stop_it(tp_api_running: int) -> None:
    message = already_serving("127.0.0.1", tp_api_running)
    assert message is not None
    assert f"already running at http://127.0.0.1:{tp_api_running}" in message
    assert "make mac-agent-remove" in message


def test_another_program_on_the_port_and_a_free_port() -> None:
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen()
    port = other.getsockname()[1]
    try:
        message = already_serving("127.0.0.1", port)
        assert message is not None
        assert "taken by another program" in message
    finally:
        other.close()
    assert already_serving("127.0.0.1", port) is None
