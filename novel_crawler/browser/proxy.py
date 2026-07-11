"""A loopback SOCKS5 proxy that pins one validated browser origin to approved IPs."""

from __future__ import annotations

import ipaddress
import re
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from novel_crawler.acquisition.security import ResolvedTarget, UrlSafetyError, UrlSafetyPolicy
from novel_crawler.core.domains import canonical_domain

_SOCKS_SUCCESS = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"


class ProxyError(RuntimeError):
    """Privacy-safe proxy lifecycle failure."""

    def __init__(self, code: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("proxy error code is invalid")
        self.code = code
        super().__init__(code)


class PinnedSocksProxy:
    """SOCKS5 CONNECT-only proxy for one exact, prevalidated origin.

    The browser must send a domain-name request. Resolution occurs exactly once
    in :meth:`start`; upstream sockets connect directly to the returned IPs.
    """

    def __init__(
        self,
        locked_url: str,
        *,
        policy: UrlSafetyPolicy | None = None,
        resolved_target: ResolvedTarget | None = None,
        max_network_bytes: int = 64 * 1024 * 1024,
        max_connections: int = 32,
        connection_timeout: float = 10.0,
        session_timeout: float = 600.0,
    ) -> None:
        if max_network_bytes <= 0 or max_connections <= 0 or connection_timeout <= 0 or session_timeout <= 0:
            raise ValueError("proxy limits must be positive")
        parts = urlsplit(locked_url)
        if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
            raise ProxyError("proxy_target_rejected")
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            raise ProxyError("proxy_target_rejected")
        try:
            self._host = canonical_domain(parts.hostname)
            self._port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except (TypeError, ValueError):
            raise ProxyError("proxy_target_rejected") from None
        self._locked_url = locked_url
        self._policy = policy or UrlSafetyPolicy()
        self._prevalidated_target = resolved_target
        self._max_network_bytes = max_network_bytes
        self._max_connections = max_connections
        self._connection_timeout = connection_timeout
        self._session_timeout = session_timeout
        self._network_bytes = 0
        self._budget_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._active = 0
        self._listener: socket.socket | None = None
        self._address: tuple[str, int] | None = None
        self._target: ResolvedTarget | None = None
        self._deadline = 0.0
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()
        self._sockets: set[socket.socket] = set()

    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise ProxyError("proxy_not_started")
        return self._address

    @property
    def proxy_url(self) -> str:
        host, port = self.address
        return f"socks5://{host}:{port}"

    @property
    def network_bytes(self) -> int:
        with self._budget_lock:
            return self._network_bytes

    def start(self) -> None:
        with self._state_lock:
            if self._listener is not None:
                raise ProxyError("proxy_already_started")
            if self._prevalidated_target is None:
                try:
                    target = self._policy.validate(self._locked_url, timeout=self._connection_timeout)
                except UrlSafetyError:
                    raise ProxyError("proxy_target_rejected") from None
            else:
                target = self._prevalidated_target
                if target.host != self._host or target.port != self._port:
                    raise ProxyError("proxy_target_rejected")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(self._max_connections)
                listener.settimeout(0.1)
            except OSError:  # pragma: no cover - environment-specific loopback failure
                listener.close()  # pragma: no cover
                raise ProxyError("proxy_start_failed") from None  # pragma: no cover
            self._target = target
            self._listener = listener
            bound = listener.getsockname()
            self._address = (str(bound[0]), int(bound[1]))
            self._deadline = time.monotonic() + self._session_timeout
            self._stop.clear()
            thread = threading.Thread(target=self._accept_loop, name="pinned-socks", daemon=True)
            self._accept_thread = thread
            thread.start()

    def close(self) -> None:
        self._stop.set()
        with self._state_lock:
            listener = self._listener
            self._listener = None
            sockets = tuple(self._sockets)
        if listener is not None:
            try:
                listener.close()
            except OSError:  # pragma: no cover - idempotent OS socket cleanup
                pass  # pragma: no cover
        for stream in sockets:
            self._close_socket(stream)
        if self._accept_thread is not None and self._accept_thread is not threading.current_thread():
            self._accept_thread.join(self._connection_timeout)
        for thread in tuple(self._threads):
            if thread is not threading.current_thread():
                thread.join(self._connection_timeout)

    def __enter__(self) -> PinnedSocksProxy:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set() and self._remaining() > 0:
            listener = self._listener
            if listener is None:
                return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._state_lock:
                at_capacity = self._active >= self._max_connections
                if not at_capacity:
                    self._active += 1
                    self._sockets.add(client)
            thread = threading.Thread(
                target=self._reject_capacity if at_capacity else self._serve,
                args=(client,),
                name="pinned-socks-connection",
                daemon=True,
            )
            with self._state_lock:
                self._threads.add(thread)
            thread.start()
        self.close()

    def _reject_capacity(self, client: socket.socket) -> None:
        try:
            client.settimeout(self._connection_timeout)
            _, count = self._recv_exact(client, 2)
            self._recv_exact(client, count)
            client.sendall(b"\x05\xff")
        except (OSError, ProxyError):  # pragma: no cover - racing capacity client disconnect
            pass  # pragma: no cover
        finally:
            self._close_socket(client)
            with self._state_lock:
                self._threads.discard(threading.current_thread())

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            client.settimeout(min(self._connection_timeout, self._remaining()))
            self._negotiate(client)
            upstream = self._connect_upstream()
            with self._state_lock:
                self._sockets.add(upstream)
            client.sendall(_SOCKS_SUCCESS)
            client.setblocking(False)
            upstream.setblocking(False)
            self._relay(client, upstream)
        except ProxyError as exc:
            if exc.code == "proxy_method_rejected":
                return
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:  # pragma: no cover - peer may close before rejection reply
                pass  # pragma: no cover
        except OSError:
            try:
                client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            except OSError:  # pragma: no cover
                pass  # pragma: no cover
        finally:
            self._close_socket(client)
            if upstream is not None:
                self._close_socket(upstream)
            with self._state_lock:
                self._active -= 1
                self._threads.discard(threading.current_thread())

    def _negotiate(self, client: socket.socket) -> None:
        version, count = self._recv_exact(client, 2)
        methods = self._recv_exact(client, count)
        if version != 5 or 0 not in methods:
            client.sendall(b"\x05\xff")
            raise ProxyError("proxy_method_rejected")
        client.sendall(b"\x05\x00")
        version, command, reserved, atyp = self._recv_exact(client, 4)
        if version != 5 or command != 1 or reserved != 0 or atyp != 3:
            raise ProxyError("proxy_request_rejected")
        size = self._recv_exact(client, 1)[0]
        if size == 0:
            raise ProxyError("proxy_request_rejected")
        try:
            host = canonical_domain(self._recv_exact(client, size).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise ProxyError("proxy_request_rejected") from None
        port = int.from_bytes(self._recv_exact(client, 2), "big")
        if host != self._host or port != self._port:
            raise ProxyError("proxy_request_rejected")

    def _connect_upstream(self) -> socket.socket:
        target = self._target
        if target is None:
            raise ProxyError("proxy_not_started")
        for address in target.addresses:
            try:
                return socket.create_connection(
                    (address, target.port),
                    timeout=min(self._connection_timeout, self._remaining()),
                )
            except OSError:
                continue
        raise ProxyError("proxy_connect_failed")

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        peers = {client: upstream, upstream: client}
        while not self._stop.is_set() and self._remaining() > 0:
            readable, _, _ = select.select(tuple(peers), (), (), min(0.1, self._remaining()))
            for source in readable:
                try:
                    data = source.recv(64 * 1024)
                except BlockingIOError:  # pragma: no cover - select readiness race
                    continue  # pragma: no cover
                if not data:
                    return
                forwarded, exhausted = self._take_budget(data)
                if forwarded:
                    peers[source].sendall(forwarded)
                if exhausted:
                    return

    def _take_budget(self, data: bytes) -> tuple[bytes, bool]:
        with self._budget_lock:
            remaining = self._max_network_bytes - self._network_bytes
            if remaining <= 0:
                return b"", True
            length = min(len(data), remaining)
            self._network_bytes += length
            return data[:length], length < len(data) or self._network_bytes >= self._max_network_bytes

    def _recv_exact(self, stream: socket.socket, length: int) -> bytes:
        value = bytearray()
        while len(value) < length:
            if self._remaining() <= 0:
                raise ProxyError("proxy_deadline")
            chunk = stream.recv(length - len(value))
            if not chunk:
                raise ProxyError("proxy_connection_closed")
            value.extend(chunk)
        return bytes(value)

    def _remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def _close_socket(self, stream: socket.socket) -> None:
        with self._state_lock:
            self._sockets.discard(stream)
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:  # pragma: no cover - idempotent OS socket cleanup
            pass  # pragma: no cover
        try:
            stream.close()
        except OSError:  # pragma: no cover - idempotent OS socket cleanup
            pass  # pragma: no cover
