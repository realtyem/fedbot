from types import SimpleNamespace
import asyncio
import logging

from aiohttp import ClientSession

logger = logging.getLogger("aiohttp_tracing")


async def on_request_start(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_start: {_params}")
    # logger.info(f"on_request_start: {trace_config_ctx}")
    trace_config_ctx.request_start = asyncio.get_event_loop().time()


async def on_request_end(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_end: {_params}")
    # logger.info(f"on_request_end: {trace_config_ctx}")
    trace_config_ctx.request_end = asyncio.get_event_loop().time()


async def on_request_chunk_sent(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_chunk_sent: {_params}")
    # logger.info(f"on_request_chunk_sent: {trace_config_ctx}")
    trace_config_ctx.request_chunk_sent = asyncio.get_event_loop().time()


async def on_request_redirect(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_redirect: {_params}")
    # logger.info(f"on_request_redirect: {trace_config_ctx}")
    trace_config_ctx.request_redirect = asyncio.get_event_loop().time()


async def on_request_exception(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_exception: {_params}")
    # logger.info(f"on_request_exception: {trace_config_ctx}")
    trace_config_ctx.request_exception = asyncio.get_event_loop().time()


async def on_request_headers_sent(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_request_headers_sent: {_params}")
    # logger.info(f"on_request_headers_sent: {trace_config_ctx}")
    trace_config_ctx.request_headers_sent = asyncio.get_event_loop().time()


async def on_response_chunk_received(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_response_chunk_received:{_params}")
    # logger.info(f"on_response_chunk_received: {trace_config_ctx}")
    trace_config_ctx.response_chunk_received = asyncio.get_event_loop().time()


async def on_connection_create_end(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_connection_create_end: {_params}")
    # logger.info(f"on_connection_create_end: {trace_config_ctx}")
    trace_config_ctx.connection_create_end = asyncio.get_event_loop().time()


async def on_connection_create_start(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_connection_create_start: {_params}")
    # logger.info(f"on_connection_create_start: {trace_config_ctx}")
    trace_config_ctx.connection_create_start = asyncio.get_event_loop().time()


async def on_connection_reuseconn(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_connection_reuseconn: {_params}")
    # logger.info(f"on_connection_reuseconn: {trace_config_ctx}")
    trace_config_ctx.connection_reuseconn = asyncio.get_event_loop().time()


async def on_connection_queued_end(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_connection_queued_end: {_params}")
    # logger.info(f"on_connection_queued_end: {trace_config_ctx}")
    trace_config_ctx.connection_queued_end = asyncio.get_event_loop().time()


async def on_connection_queued_start(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_connection_queued_start: {_params}")
    # logger.info(f"on_connection_queued_start: {trace_config_ctx}")
    trace_config_ctx.connection_queued_start = asyncio.get_event_loop().time()


async def on_dns_cache_hit(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_dns_cache_hit: {_params}")
    # logger.info(f"on_dns_cache_hit: {trace_config_ctx}")
    trace_config_ctx.dns_cache_hit = asyncio.get_event_loop().time()


async def on_dns_cache_miss(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_dns_cache_miss: {_params}")
    # logger.info(f"on_dns_cache_miss: {trace_config_ctx}")
    trace_config_ctx.dns_cache_miss = asyncio.get_event_loop().time()


async def on_dns_resolvehost_end(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_dns_resolvehost_end: {_params}")
    # logger.info(f"on_dns_resolvehost_end: {trace_config_ctx}")
    trace_config_ctx.dns_resolvehost_end = asyncio.get_event_loop().time()


async def on_dns_resolvehost_start(_session: ClientSession, trace_config_ctx: SimpleNamespace, _params) -> None:
    # logger.info(f"on_dns_resolvehost_start: {_params}")
    # logger.info(f"on_dns_resolvehost_start: {trace_config_ctx}")
    trace_config_ctx.dns_resolvehost_start = asyncio.get_event_loop().time()
