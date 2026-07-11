from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from novel_crawler.acquisition.security import UrlSafetyPolicy
from novel_crawler.browser.proxy import PinnedSocksProxy, ProxyError


@contextmanager
def upstream(handler: Callable[[socket.socket], None]) -> Iterator[int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    done = threading.Event()

    def run() -> None:
        try:
            connection, _ = server.accept()
            with connection:
                handler(connection)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.close()
        done.wait(2)
        thread.join(2)


def connect_proxy(proxy: PinnedSocksProxy, host: str, port: int, *, command: int = 1, atyp: int = 3) -> socket.socket:
    client = socket.create_connection(proxy.address, timeout=1)
    client.sendall(b"\x05\x01\x00")
    assert client.recv(2) == b"\x05\x00"
    if atyp == 3:
        encoded = host.encode("idna")
        address = bytes((len(encoded),)) + encoded
    else:
        address = socket.inet_aton(host)
    client.sendall(b"\x05" + bytes((command, 0, atyp)) + address + port.to_bytes(2, "big"))
    return client


def test_proxy_pins_approved_ip_and_relays_without_second_resolution() -> None:
    resolutions: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str]:
        resolutions.append((host, port))
        return ("127.0.0.1",)

    def echo(connection: socket.socket) -> None:
        connection.sendall(connection.recv(64).upper())

    with upstream(echo) as port:
        policy = UrlSafetyPolicy(resolver=resolver)
        # This test intentionally maps a public logical origin to a local fake upstream.
        policy._require_public = lambda address, safe_url: None  # type: ignore[method-assign]
        proxy = PinnedSocksProxy(f"http://example.test:{port}/private", policy=policy, max_network_bytes=128)
        proxy.start()
        try:
            client = connect_proxy(proxy, "example.test", port)
            assert client.recv(10)[1] == 0
            client.sendall(b"hello")
            assert client.recv(5) == b"HELLO"
            client.close()
        finally:
            proxy.close()
    assert resolutions == [("example.test", port)]


@pytest.mark.parametrize(
    ("host", "port", "command", "atyp"),
    [
        ("other.test", 443, 1, 3),
        ("example.test", 444, 1, 3),
        ("example.test", 443, 2, 3),
        ("93.184.216.34", 443, 1, 1),
    ],
)
def test_proxy_rejects_cross_origin_port_bind_and_ip_literals(host: str, port: int, command: int, atyp: int) -> None:
    policy = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))
    proxy = PinnedSocksProxy("https://example.test/", policy=policy, max_network_bytes=128)
    proxy.start()
    try:
        client = connect_proxy(proxy, host, port, command=command, atyp=atyp)
        try:
            reply = client.recv(10)
        except ConnectionResetError:
            reply = b""
        assert reply[:2] != b"\x05\x00"
        client.close()
    finally:
        proxy.close()


def test_proxy_global_bidirectional_cap_hard_closes_connection() -> None:
    def flood(connection: socket.socket) -> None:
        connection.recv(64)
        connection.sendall(b"x" * 64)

    with upstream(flood) as port:
        policy = UrlSafetyPolicy(resolver=lambda host, port: ("127.0.0.1",))
        policy._require_public = lambda address, safe_url: None  # type: ignore[method-assign]
        proxy = PinnedSocksProxy(f"http://example.test:{port}/", policy=policy, max_network_bytes=12)
        proxy.start()
        try:
            client = connect_proxy(proxy, "example.test", port)
            assert client.recv(10)[1] == 0
            client.sendall(b"hello")
            received = client.recv(64)
            assert len(received) <= 7
            assert client.recv(1) == b""
            client.close()
            assert proxy.network_bytes <= 12
        finally:
            proxy.close()


def test_proxy_rejects_private_resolution_and_invalid_limits_safely() -> None:
    with pytest.raises(ValueError, match="proxy limits"):
        PinnedSocksProxy("https://example.test/", max_network_bytes=0)
    proxy = PinnedSocksProxy(
        "https://example.test/private?secret=x",
        policy=UrlSafetyPolicy(resolver=lambda host, port: ("127.0.0.1",)),
        max_network_bytes=1,
    )
    with pytest.raises(ProxyError, match="proxy_target_rejected") as caught:
        proxy.start()
    assert "private" not in str(caught.value) and "secret" not in str(caught.value)


def test_proxy_enforces_max_connections() -> None:
    policy = UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))
    proxy = PinnedSocksProxy("https://example.test/", policy=policy, max_network_bytes=128, max_connections=1)
    proxy.start()
    try:
        first = socket.create_connection(proxy.address, timeout=1)
        second = socket.create_connection(proxy.address, timeout=1)
        first.sendall(b"\x05\x01\x00")
        assert first.recv(2) == b"\x05\x00"
        second.sendall(b"\x05\x01\x00")
        assert second.recv(2) != b"\x05\x00"
        first.close()
        second.close()
    finally:
        proxy.close()


def test_proxy_lifecycle_and_handshake_rejections_are_safe() -> None:
    with pytest.raises(ValueError, match="error code"):
        ProxyError("cookie=secret")
    for url in ("file:///private", "https://127.0.0.1/"):
        with pytest.raises(ProxyError, match="proxy_target_rejected"):
            PinnedSocksProxy(url)
    proxy = PinnedSocksProxy(
        "https://example.test/", policy=UrlSafetyPolicy(resolver=lambda host, port: ("93.184.216.34",))
    )
    with pytest.raises(ProxyError, match="proxy_not_started"):
        _ = proxy.address
    proxy.start()
    try:
        assert proxy.proxy_url.startswith("socks5://127.0.0.1:")
        with pytest.raises(ProxyError, match="proxy_already_started"):
            proxy.start()
        client = socket.create_connection(proxy.address, timeout=1)
        client.sendall(b"\x05\x01\x02")
        assert client.recv(2) != b"\x05\x00"
        client.close()
    finally:
        proxy.close()
        proxy.close()


def test_proxy_rejects_prevalidated_target_binding_mismatch() -> None:
    from novel_crawler.acquisition.security import ResolvedTarget

    proxy = PinnedSocksProxy(
        "https://example.test/",
        resolved_target=ResolvedTarget("other.test", 443, ("93.184.216.34",)),
    )
    with pytest.raises(ProxyError, match="proxy_target_rejected"):
        proxy.start()
