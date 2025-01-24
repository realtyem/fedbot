"""
Timing hooks for Matrix federation request tracing.

This module provides AIOHTTP trace hooks used by FederationApi to monitor and debug
Matrix federation requests. Each hook records timestamps for different stages of
request lifecycle:
- Request lifecycle (start/end/chunks/headers)
- Connection handling (creation/reuse/queuing)
- DNS resolution (cache hits/misses, hostname resolution)

These hooks are attached to the AIOHTTP client session in FederationApi to:
- Track timing of federation requests to remote Matrix servers
- Debug connection and DNS issues with federation requests
- Monitor request performance and connection reuse
- Help diagnose federation connectivity problems

The timestamps are stored in the trace context and can be analyzed to understand
request timing and identify bottlenecks in federation communication.
"""

from __future__ import annotations

from asyncio import get_event_loop
from logging import getLogger as get_logger
from typing import TYPE_CHECKING

from aiohttp.tracing import TraceRequestEndParams, TraceRequestStartParams

if TYPE_CHECKING:
    from types import SimpleNamespace

    from aiohttp import ClientSession

logger = get_logger("aiohttp_tracing")

# Type alias for all trace callback args since they share same structure
TraceRequestParams = TraceRequestStartParams | TraceRequestEndParams


async def on_request_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request starts."""
    trace_config_ctx.request_start = get_event_loop().time()


async def on_request_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request completes."""
    trace_config_ctx.request_end = get_event_loop().time()


async def on_request_chunk_sent(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request chunk is sent."""
    trace_config_ctx.request_chunk_sent = get_event_loop().time()


async def on_request_redirect(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request is redirected."""
    trace_config_ctx.request_redirect = get_event_loop().time()


async def on_request_exception(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request encounters an exception."""
    trace_config_ctx.request_exception = get_event_loop().time()


async def on_request_headers_sent(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when request headers are sent."""
    trace_config_ctx.request_headers_sent = get_event_loop().time()


async def on_response_chunk_received(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when response chunk is received."""
    trace_config_ctx.response_chunk_received = get_event_loop().time()


async def on_connection_create_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when connection creation completes."""
    trace_config_ctx.connection_create_end = get_event_loop().time()


async def on_connection_create_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when connection creation starts."""
    trace_config_ctx.connection_create_start = get_event_loop().time()


async def on_connection_reuseconn(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when existing connection is reused."""
    trace_config_ctx.connection_reuseconn = get_event_loop().time()


async def on_connection_queued_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when connection queuing ends."""
    trace_config_ctx.connection_queued_end = get_event_loop().time()


async def on_connection_queued_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when connection queuing starts."""
    trace_config_ctx.connection_queued_start = get_event_loop().time()


async def on_dns_cache_hit(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when DNS cache hit occurs."""
    trace_config_ctx.dns_cache_hit = get_event_loop().time()


async def on_dns_cache_miss(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when DNS cache miss occurs."""
    trace_config_ctx.dns_cache_miss = get_event_loop().time()


async def on_dns_resolvehost_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when DNS host resolution completes."""
    trace_config_ctx.dns_resolvehost_end = get_event_loop().time()


async def on_dns_resolvehost_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestParams,
) -> None:
    """Record timestamp when DNS host resolution starts."""
    trace_config_ctx.dns_resolvehost_start = get_event_loop().time()
