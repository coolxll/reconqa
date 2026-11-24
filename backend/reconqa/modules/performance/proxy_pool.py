"""Lightweight async HTTP & HTTPS CONNECT round-robin local proxy pool forwarder.

Usage:
    async with RoundRobinProxyPool(["http://127.0.0.1:9001", "http://127.0.0.1:9002"]) as url:
        # Pass `HTTP_PROXY=url` / `HTTPS_PROXY=url` to k6 or httpx
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class UpstreamProxy:
    url: str
    host: str
    port: int
    auth_header: str | None = None


def _parse_upstream(raw_url: str) -> UpstreamProxy:
    url = raw_url.strip()
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError(f"Invalid upstream proxy URL: {raw_url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 8080)
    auth_header: str | None = None
    if parsed.username is not None:
        creds = f"{parsed.username}:{parsed.password or ''}".encode()
        token = base64.b64encode(creds).decode("ascii")
        auth_header = f"Basic {token}"
    return UpstreamProxy(url=raw_url.strip(), host=parsed.hostname, port=port, auth_header=auth_header)


class RoundRobinProxyPool:
    """Async local HTTP/CONNECT proxy gateway rotating across multiple upstream proxies."""

    def __init__(
        self,
        upstreams: list[str],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        connect_timeout: float = 10.0,
    ) -> None:
        if not upstreams:
            raise ValueError("RoundRobinProxyPool requires at least one upstream proxy URL")
        self._upstreams: list[UpstreamProxy] = [_parse_upstream(u) for u in upstreams]
        self._host = host
        self._requested_port = port
        self._connect_timeout = connect_timeout
        self._index = 0
        self._server: asyncio.AbstractServer | None = None
        self._actual_port: int | None = None
        self._requests_total = 0
        self._errors = 0
        self._per_upstream: dict[str, int] = {u.url: 0 for u in self._upstreams}

    @property
    def upstreams(self) -> list[str]:
        return [u.url for u in self._upstreams]

    @property
    def proxy_url(self) -> str:
        if self._actual_port is None:
            raise RuntimeError("RoundRobinProxyPool has not been started")
        return f"http://{self._host}:{self._actual_port}"

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "requests_total": self._requests_total,
            "errors": self._errors,
            "per_upstream": dict(self._per_upstream),
        }

    def next_upstream(self) -> UpstreamProxy:
        upstream = self._upstreams[self._index % len(self._upstreams)]
        self._index += 1
        return upstream

    async def start(self) -> str:
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._requested_port,
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Failed to bind local proxy pool socket")
        self._actual_port = int(sockets[0].getsockname()[1])
        return self.proxy_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._actual_port = None

    async def __aenter__(self) -> str:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        up_writer: asyncio.StreamWriter | None = None
        try:
            head_bytes = await asyncio.wait_for(
                client_reader.readuntil(b"\r\n\r\n"),
                timeout=self._connect_timeout,
            )
            upstream = self.next_upstream()
            self._requests_total += 1
            self._per_upstream[upstream.url] = self._per_upstream.get(upstream.url, 0) + 1

            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(upstream.host, upstream.port),
                timeout=self._connect_timeout,
            )

            first_line = head_bytes.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            is_connect = first_line.upper().startswith("CONNECT ")
            rewritten_head = self._prepare_upstream_head(
                head_bytes,
                auth_header=upstream.auth_header,
                force_close=not is_connect,
            )

            up_writer.write(rewritten_head)
            await up_writer.drain()

            if is_connect:
                await self._relay_bidirectional(client_reader, client_writer, up_reader, up_writer)
            else:
                content_length = self._extract_content_length(head_bytes)
                if content_length > 0:
                    body = await asyncio.wait_for(
                        client_reader.readexactly(content_length),
                        timeout=self._connect_timeout,
                    )
                    up_writer.write(body)
                    await up_writer.drain()
                await self._pipe(up_reader, client_writer)
        except Exception:
            self._errors += 1
            try:
                if not client_writer.is_closing():
                    client_writer.write(
                        b"HTTP/1.1 502 Bad Gateway\r\n"
                        b"Content-Length: 11\r\n"
                        b"Connection: close\r\n\r\n"
                        b"Bad Gateway"
                    )
                    await client_writer.drain()
            except Exception:
                pass
        finally:
            for writer in (up_writer, client_writer):
                if writer is not None and not writer.is_closing():
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

    @staticmethod
    def _extract_content_length(head_bytes: bytes) -> int:
        lines = head_bytes.decode("latin-1", errors="replace").split("\r\n")[1:]
        for line in lines:
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.strip().lower() == "content-length":
                try:
                    return max(0, int(v.strip()))
                except ValueError:
                    return 0
        return 0

    @staticmethod
    def _prepare_upstream_head(
        head_bytes: bytes,
        *,
        auth_header: str | None,
        force_close: bool,
    ) -> bytes:
        text = head_bytes[:-4].decode("latin-1", errors="replace")
        lines = text.split("\r\n")
        request_line = lines[0]
        filtered: list[str] = []
        for line in lines[1:]:
            low = line.lower()
            if auth_header and low.startswith("proxy-authorization:"):
                continue
            if force_close and (low.startswith("connection:") or low.startswith("proxy-connection:")):
                continue
            filtered.append(line)
        if auth_header:
            filtered.append(f"Proxy-Authorization: {auth_header}")
        if force_close:
            filtered.append("Connection: close")
            filtered.append("Proxy-Connection: close")
        return ("\r\n".join([request_line, *filtered]) + "\r\n\r\n").encode("latin-1")

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass

    async def _relay_bidirectional(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        t1 = asyncio.create_task(self._pipe(client_reader, up_writer))
        t2 = asyncio.create_task(self._pipe(up_reader, client_writer))
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
