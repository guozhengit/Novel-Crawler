import random
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProxyEntry:
    url: str
    scheme: str = "http"
    fail_count: int = 0
    max_fails: int = 3
    alive: bool = True

    def to_dict(self) -> dict[str, str]:
        return {self.scheme: self.url}

    def record_fail(self) -> None:
        self.fail_count += 1
        if self.fail_count >= self.max_fails:
            self.alive = False

    def reset(self) -> None:
        self.fail_count = 0
        self.alive = True


class ProxyPool:
    """代理池：轮换、失败统计、自动剔除。"""

    def __init__(self, proxies: list[str] | None = None, rotate_strategy: str = "round_robin"):
        self.entries: list[ProxyEntry] = []
        self.rotate_strategy = rotate_strategy
        self._lock = threading.Lock()
        self._index = 0
        if proxies:
            for p in proxies:
                self.add(p)

    @classmethod
    def from_file(cls, path: Path) -> "ProxyPool":
        pool = cls()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    pool.add(line)
        return pool

    def add(self, proxy_url: str, scheme: str = "http") -> None:
        if "://" in proxy_url:
            scheme = proxy_url.split("://")[0]
        self.entries.append(ProxyEntry(url=proxy_url, scheme=scheme))

    def next(self) -> dict[str, str] | None:
        with self._lock:
            alive = [e for e in self.entries if e.alive]
            if not alive:
                return None
            if self.rotate_strategy == "random":
                entry = random.choice(alive)
            else:
                entry = alive[self._index % len(alive)]
                self._index += 1
            return entry.to_dict()

    def record_fail(self, proxy_dict: dict[str, str] | None = None) -> None:
        with self._lock:
            if proxy_dict is None:
                return
            for entry in self.entries:
                if proxy_dict.get(entry.scheme) == entry.url:
                    entry.record_fail()
                    break

    def alive_count(self) -> int:
        return sum(1 for e in self.entries if e.alive)

    def reset_all(self) -> None:
        with self._lock:
            for entry in self.entries:
                entry.reset()

    def to_text(self) -> str:
        lines = [f"ProxyPool: {self.alive_count()}/{len(self.entries)} alive"]
        for entry in self.entries:
            status = "alive" if entry.alive else "dead"
            lines.append(f"  [{status}] {entry.scheme}://{entry.url} (fails={entry.fail_count})")
        return "\n".join(lines)
