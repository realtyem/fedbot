"""
Timing hooks for Matrix federation request tracing.

Provides AIOHTTP trace hooks used by FederationApi to monitor and debug Matrix
federation requests. Records timestamps for request lifecycle stages to help
diagnose federation connectivity and performance issues.

The hooks track request timing, connection handling, and DNS resolution to help
identify bottlenecks in federation communication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from asyncio import get_event_loop
from logging import getLogger as get_logger

from aiohttp.tracing import (
    TraceConfig,
    TraceConnectionCreateEndParams,
    TraceConnectionCreateStartParams,
    TraceConnectionQueuedEndParams,
    TraceConnectionQueuedStartParams,
    TraceConnectionReuseconnParams,
    TraceDnsCacheHitParams,
    TraceDnsCacheMissParams,
    TraceDnsResolveHostEndParams,
    TraceDnsResolveHostStartParams,
    TraceRequestChunkSentParams,
    TraceRequestEndParams,
    TraceRequestExceptionParams,
    TraceRequestHeadersSentParams,
    TraceRequestRedirectParams,
    TraceRequestStartParams,
    TraceResponseChunkReceivedParams,
)

if TYPE_CHECKING:
    from types import SimpleNamespace

    from aiohttp import ClientSession

logger = get_logger("aiohttp_tracing")


def make_fresh_trace_config() -> TraceConfig:
    trace_config = TraceConfig()
    trace_config.on_request_start.append(on_request_start)
    trace_config.on_request_end.append(on_request_end)
    trace_config.on_request_chunk_sent.append(on_request_chunk_sent)
    trace_config.on_request_redirect.append(on_request_redirect)
    trace_config.on_request_exception.append(on_request_exception)
    trace_config.on_request_headers_sent.append(on_request_headers_sent)
    trace_config.on_response_chunk_received.append(on_response_chunk_received)
    trace_config.on_connection_create_end.append(on_connection_create_end)
    trace_config.on_connection_create_start.append(on_connection_create_start)
    trace_config.on_connection_reuseconn.append(on_connection_reuseconn)
    trace_config.on_connection_queued_end.append(on_connection_queued_end)
    trace_config.on_connection_queued_start.append(on_connection_queued_start)
    trace_config.on_dns_cache_hit.append(on_dns_cache_hit)
    trace_config.on_dns_cache_miss.append(on_dns_cache_miss)
    trace_config.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    trace_config.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    return trace_config


async def on_request_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestStartParams,
) -> None:
    """Record timestamp when request starts."""
    trace_config_ctx.request_start = get_event_loop().time()


async def on_request_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestEndParams,
) -> None:
    """Record timestamp when request completes."""
    trace_config_ctx.request_end = get_event_loop().time()


async def on_request_chunk_sent(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestChunkSentParams,
) -> None:
    """Record timestamp when request chunk is sent."""
    trace_config_ctx.request_chunk_sent = get_event_loop().time()


async def on_request_redirect(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestRedirectParams,
) -> None:
    """Record timestamp when request is redirected."""
    trace_config_ctx.request_redirect = get_event_loop().time()


async def on_request_exception(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestExceptionParams,
) -> None:
    """Record timestamp when request encounters an exception."""
    trace_config_ctx.request_exception = get_event_loop().time()


async def on_request_headers_sent(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceRequestHeadersSentParams,
) -> None:
    """Record timestamp when request headers are sent."""
    trace_config_ctx.request_headers_sent = get_event_loop().time()


async def on_response_chunk_received(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceResponseChunkReceivedParams,
) -> None:
    """Record timestamp when response chunk is received."""
    trace_config_ctx.response_chunk_received = get_event_loop().time()


async def on_connection_create_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceConnectionCreateEndParams,
) -> None:
    """Record timestamp when connection creation completes."""
    trace_config_ctx.connection_create_end = get_event_loop().time()


async def on_connection_create_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceConnectionCreateStartParams,
) -> None:
    """Record timestamp when connection creation starts."""
    trace_config_ctx.connection_create_start = get_event_loop().time()


async def on_connection_reuseconn(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceConnectionReuseconnParams,
) -> None:
    """Record timestamp when existing connection is reused."""
    trace_config_ctx.connection_reuseconn = get_event_loop().time()


async def on_connection_queued_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceConnectionQueuedEndParams,
) -> None:
    """Record timestamp when connection queuing ends."""
    trace_config_ctx.connection_queued_end = get_event_loop().time()


async def on_connection_queued_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceConnectionQueuedStartParams,
) -> None:
    """Record timestamp when connection queuing starts."""
    trace_config_ctx.connection_queued_start = get_event_loop().time()


async def on_dns_cache_hit(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceDnsCacheHitParams,
) -> None:
    """Record timestamp when DNS cache hit occurs."""
    trace_config_ctx.dns_cache_hit = get_event_loop().time()


async def on_dns_cache_miss(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceDnsCacheMissParams,
) -> None:
    """Record timestamp when DNS cache miss occurs."""
    trace_config_ctx.dns_cache_miss = get_event_loop().time()


async def on_dns_resolvehost_end(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceDnsResolveHostEndParams,
) -> None:
    """Record timestamp when DNS host resolution completes."""
    trace_config_ctx.dns_resolvehost_end = get_event_loop().time()


async def on_dns_resolvehost_start(  # noqa: RUF029
    _session: ClientSession,
    trace_config_ctx: SimpleNamespace,
    _params: TraceDnsResolveHostStartParams,
) -> None:
    """Record timestamp when DNS host resolution starts."""
    trace_config_ctx.dns_resolvehost_start = get_event_loop().time()
