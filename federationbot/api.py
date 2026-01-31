from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple
from asyncio import sleep
import json
import logging
import time

from aiohttp import ClientResponse, ClientSession, ClientTimeout, TCPConnector, client_exceptions
from yarl import URL
import backoff

from federationbot.cache import LRUCache
from federationbot.controllers import ReactionTaskController
from federationbot.delegation import DelegationHandler
from federationbot.errors import (
    FedBotException,
    PluginTimeout,
    ServerDiscoveryDNSError,
    ServerSSLException,
    ServerUnreachable,
)
from federationbot.requests import FederationRequests
from federationbot.requests.backoff import (
    backoff_logging_backoff_handler,
    backoff_logging_giveup_handler,
    backoff_update_retries_handler,
)
from federationbot.requests.requests import authorization_headers
from federationbot.resolver import StatusEnum
from federationbot.responses import MatrixError, MatrixFederationResponse, MatrixResponse
from federationbot.server_result import DiagnosticInfo, ResponseStatusType, ServerResult
from federationbot.tracing import make_fresh_trace_config
from federationbot.types import RoomAlias

fedapi_logger = logging.getLogger("federation_api")

SOCKET_TIMEOUT_SECONDS = 5.0
USER_AGENT_STRING = "AllYourServerBelongsToUs 0.1.1"
# Some fools have their anti-indexer system on their reverse proxy that filters out things from inside
# the /_matrix urlspace. 'bot' and 'Python' trigger it, so use a different name
# "Maubot/Fedbot 0.1.1"


class FederationApi:
    def __init__(
        self,
        server_signing_keys: Dict[str, str],
        task_controller: ReactionTaskController,
    ):
        self.server_signing_keys = server_signing_keys
        self.task_controller = task_controller
        self.json_decoder = json.JSONDecoder()
        # Map this cache to server_name -> ServerResult
        self.server_discovery_cache: LRUCache[str, ServerResult] = LRUCache(expire_after_seconds=60 * 30)

        connector = TCPConnector(ttl_dns_cache=60 * 60 * 5, limit=10000, limit_per_host=1, force_close=True)
        # TODO: Make a custom Resolver to handle server discovery
        self.http_client = ClientSession(
            connector=connector,
            trace_configs=[make_fresh_trace_config()],
        )
        self.delegation_handler = DelegationHandler(self._federation_request)
        self.federation_transport = FederationRequests(self.server_signing_keys)
        # self.server_discovery_resolver = ServerDiscoveryResolver(self.http_client)

    async def shutdown(self) -> None:
        await self.http_client.close()
        await self.federation_transport.http_client.close()
        await self.server_discovery_cache.stop()

    @backoff.on_predicate(
        backoff.runtime,
        predicate=lambda r: r.status == 429,
        value=lambda r: int(r.headers.get("Retry-After")),
    )
    async def _federation_request(
        self,
        destination_server_name: str,
        path: str,
        *,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        origin_server: Optional[str] = None,
        server_result: Optional[ServerResult] = None,
        force_ip: Optional[str] = None,
        force_port: Optional[int] = None,
        content: Optional[Dict[str, Any]] = None,
        timeout: float = 10.0,
        stagger_requests_by: float = 0.0,
    ) -> ClientResponse:
        """
        Retrieve json response from over federation. This inner function handles
            applying auth to the request

        Args:
            destination_server_name: The server_name which acts as a hostname(or IP).
                Extract actual server connection details from server_result or assume
                that the server_name is the actual hostname.
            path: The path to query
            query_args: Optional query arguments as a sequence of Tuples
            method: GET, POST, etc.
            origin_server: if authing this request, the server originating the request
            server_result: Allows access to server discovery data, like port, host
                header, and sni data
            content: if not a GET request, the content to send
            timeout: float of seconds before socket read timeout occurs

        Returns: A ClientResponse aiohttp context manager thingy

        """
        # Use the URL class to build the parts, as otherwise the query
        # parameters don't encode right and fail the JSON signing process verify
        # on the other end. I suspect it has to do with sigil encoding.
        #
        # From what I understand, having a port as 'None' will cause the URL to select
        # the most likely port based on the scheme. Sounds handy, use that.
        destination_port: Optional[int]
        request_headers = {"User-Agent": USER_AGENT_STRING}
        if server_result:
            if server_result.unhealthy:
                raise ServerUnreachable(
                    f"{server_result.unhealthy}",
                    "Server was previously unreachable",
                )
            ip_port_tuple = server_result.get_ip_port_or_hostname()

            try:
                host, port = ip_port_tuple
            except TypeError:
                raise ServerDiscoveryDNSError("No IP Address found")

            resolved_destination_server = force_ip or host
            destination_port = int(port)

            server_hostname_sni = server_result.sni_server_name if server_result.use_sni else None
            request_headers.update({"Host": server_result.host_header})

        else:
            # To allow for the simple request machinery in the delegation handler, allow overriding the ip/port when
            # there is no ServerResult. Default to None, so default behavior should fall through.
            destination_port = force_port
            resolved_destination_server = force_ip or destination_server_name
            server_hostname_sni = None

        url_object = URL.build(
            scheme="https",
            host=resolved_destination_server,
            port=destination_port,
            path=path,
            query=query_args,
            encoded=False,
        )

        if origin_server:
            # Implying an origin server means this request must have auth attached
            assert request_headers is not None
            request_headers["Authorization"] = authorization_headers(
                origin_name=origin_server,
                origin_signing_key=self.server_signing_keys[origin_server],
                destination_name=destination_server_name,
                request_method=method,
                uri=str(url_object.relative()),
                content=content,
            )

        client_timeouts = ClientTimeout(
            # Don't limit the total connection time, as incremental reads are handled distinctly by sock_read
            total=None,
            # connect should be None, as sock_connect is behavior intended. This is for waiting for a connection from
            # the pool as well as establishing the connection itself.
            connect=None,
            # This is the most useful for detecting bad servers
            sock_connect=timeout,
            # This is the one that may have the longest time, as we wait for a server to send a response
            sock_read=timeout,
            # defaults to 5, for roundups on timeouts
            # ceil_threshold=5.0,
        )

        if stagger_requests_by > 0:
            await sleep(stagger_requests_by)
        try:
            response = await self.http_client.request(
                method=method,
                url=url_object,
                headers=request_headers,
                timeout=client_timeouts,
                server_hostname=server_hostname_sni,
                json=content,
                trace_request_ctx={"url": url_object},
            )

        # Split the different exceptions up based on where the information is extracted from
        except client_exceptions.ClientConnectorCertificateError as e:
            # This is one of the errors I found while probing for SNI TLS
            raise ServerSSLException(  # pylint: disable=bad-exception-cause
                e.__class__.__name__, str(e.certificate_error)
            ) from e

        except (
            client_exceptions.ClientConnectorSSLError,
            client_exceptions.ClientSSLError,
        ) as e:
            # This is one of the errors I found while probing for SNI TLS
            raise ServerSSLException(e.__class__.__name__, e.strerror) from e

        except (
            # e is an OSError, may have e.strerror, possibly e.os_error.strerror
            client_exceptions.ClientProxyConnectionError,
            # e is an OSError, may have e.strerror, possibly e.os_error.strerror
            client_exceptions.ClientConnectorError,
            # e is an OSError, may have e.strerror
            client_exceptions.ClientOSError,
        ) as e:
            if hasattr(e, "os_error"):
                # This gets type ignored, as it is defined but for some reason mypy can't figure that out
                raise FedBotException(e.__class__.__name__, e.os_error.strerror) from e  # type: ignore[attr-defined]

            raise FedBotException(e.__class__.__name__, e.strerror) from e

        except (
            client_exceptions.ClientHttpProxyError,  # e.message
            client_exceptions.ClientResponseError,  # e.message, base class, possibly e.status too
            client_exceptions.ServerDisconnectedError,  # e.message
        ) as e:
            raise FedBotException(e.__class__.__name__, str(e.message)) from e

        except client_exceptions.ServerTimeoutError as e:
            # ServerTimeoutError is asyncio.TimeoutError under its hood
            raise PluginTimeout(
                e.__class__.__name__,
                f"{e.__class__.__name__} after {SOCKET_TIMEOUT_SECONDS} seconds",
            ) from e

        except (
            ServerUnreachable,
            ConnectionRefusedError,
            client_exceptions.ServerFingerprintMismatch,  # e.expected, e.got
            client_exceptions.InvalidURL,  # e.url
            client_exceptions.ClientPayloadError,  # e
            client_exceptions.ServerConnectionError,  # e
            client_exceptions.ClientConnectionError,  # e
            client_exceptions.ClientError,  # e
            Exception,  # e
        ) as e:
            fedapi_logger.info(
                "federation_request: General Exception: for %s:\n %r",
                destination_server_name,
                e,
            )
            raise FedBotException(e.__class__.__name__, str(e)) from e

        return response

    @backoff.on_exception(
        backoff.expo,
        PluginTimeout,
        max_tries=3,
        logger=None,
        on_backoff=[backoff_logging_backoff_handler, backoff_update_retries_handler],
        on_giveup=[backoff_logging_giveup_handler, backoff_update_retries_handler],
        max_value=4.0,
        base=1.5,
    )
    async def federation_request(
        self,
        destination_server_name: str,
        path: str,
        *,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        force_rediscover: bool = False,
        diagnostics: bool = False,
        origin_server: Optional[str] = None,
        content: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> MatrixResponse:
        """
        Retrieve json response from over federation. This outer-level function handles
        caching of server discovery processes and catching errors. Calls
        _federation_request() inside to handle authing and places the actual request.

        Args:
            destination_server_name: the server name being sent to, delegation is
                handled within
            path: The path component of the outgoing url
            query_args: the query component to send
            method: The method to use for the request: GET, PUT, etc.
            force_rediscover: in case we need to bypass the cache to redo server
                discovery
            diagnostics: Collect diagnostic data. Errors are always collected
            origin_server: The server to send the request as, signing keys will be
                required to be setup in the config files for authed requests
            content: for non-GET requests, the Dict that will be transformed into json
                to send
        Returns:
            MatrixResponse
        """
        diag_info = DiagnosticInfo(diagnostics)
        server_result = self.server_discovery_cache.get(destination_server_name)
        now = time.time()

        # Either no ServerResult, or
        # forcing rediscovery, OR
        # had a ServerResult, but it was unhealthy and requested retry time has passed
        # then try and reload the ServerResult
        if not server_result or force_rediscover or (server_result.unhealthy and server_result.retry_time_s < now):
            # If this server was attempted at some point and failed, there is no
            # point trying again until the cache entry is replaced.

            server_result = await self.delegation_handler.discover_server(
                destination_server_name,
                diag_info=diag_info,
                **kwargs,
            )

        try:
            response = await self._federation_request(
                destination_server_name,
                path,
                query_args=query_args,
                method=method,
                origin_server=origin_server,
                server_result=server_result,
                content=content,
                **kwargs,
            )

        except (FedBotException, ServerDiscoveryDNSError) as e:
            fedapi_logger.warning("Problem on %s: %r", destination_server_name, e)
            # All the inner exceptions that can be raised are given a code of 0, representing an outside error
            if isinstance(e, FedBotException):
                diag_info.error(str(e.long_exception))
                server_result.unhealthy = str(e.summary_exception)
            else:
                diag_info.error("DNS error")
                server_result.unhealthy = "DNS error"
            # Since there was an exception, cache the result unless it was a timeout error, as that shouldn't count

            # Errors will be cached for 10 minutes
            server_result.retry_time_s = now + 10 * 60

            self.server_discovery_cache.set(server_result.host, server_result)

            return MatrixError(
                http_code=0,
                errcode=str(0),
                reason=str(e),
                diag_info=diag_info if diagnostics else None,
                json_response={},
            )

        # The server was responsive, but may not have returned something useful
        errcode: Optional[str] = None
        error: Optional[str] = None
        async with response:
            code = response.status
            reason = response.reason or "No Reason/status returned"
            headers = response.headers
            self.server_discovery_cache.set(server_result.host, server_result)
            for ctx in response._traces:  # noqa: W0212  # pylint:disable=protected-access
                # The tracing context saves it's time in float of seconds
                diag_info.trace_ctx = ctx._trace_config_ctx  # noqa: W0212  # pylint:disable=protected-access
                # self.logger.info(f"Found context info in _traces: {context}")

            diag_info.add(f"Request status: code:{code}, reason: {reason}")

            if 200 <= code < 599:
                try:
                    result_dict = self.json_decoder.decode(await response.text())
                except json.decoder.JSONDecodeError:
                    diag_info.error("JSONDecodeError")
                    diag_info.add("No usable data in response")
                    result_dict = None
                except client_exceptions.ServerTimeoutError:
                    diag_info.error("Server Timed out while reading response")
                    result_dict = None
                    fedapi_logger.debug(
                        "fedreq: Weird server timeout while reading response: %s %s", destination_server_name, path
                    )

                # if there was a matrix related error, pick the bits out of the json
                if result_dict:
                    error = result_dict.get("error", error)
                    errcode = result_dict.get("errcode", errcode)

            else:
                result_dict = None

        diag_info.tls_handled_by = headers.get("server", None)

        caddy_hit = bool(code == 200 and result_dict is None)
        # The request is complete.
        # Caddy is notorious for returning a 200 as a default for non-existent endpoints. This is a problem, and
        # I believe it is against the Spec. When this happens, there should be no JSON to decode so result_dict
        # should still be None. Lump that in with other errors to return
        if code != 200 or caddy_hit:
            diag_info.error(f"Request to {path} failed")
            if code == 200:
                result_dict = {}

                # Going to log this for now, see how prevalent it is
                fedapi_logger.debug("fedreq: HIT possible Caddy condition: %s", destination_server_name)

            return MatrixError(
                http_code=code,
                reason=reason,
                diag_info=diag_info if diagnostics else None,
                json_response=result_dict,
                errcode=errcode,
                error=error,
                tracing_context=diag_info.trace_ctx,
            )

        # Don't need a success diagnostic message here, the one above works fine
        return MatrixFederationResponse(
            http_code=code,
            reason=reason,
            diag_info=diag_info if diagnostics else None,
            json_response=result_dict,
            errcode=errcode,
            error=error,
            tracing_context=diag_info.trace_ctx,
            headers=headers,
        )

    async def get_server_version(
        self,
        server_name: str,
        force_rediscover: bool = False,
        diagnostics: bool = False,
        **kwargs,
    ) -> MatrixResponse:
        # response = await self.federation_transport.request(
        #     server_name, "/_matrix/federation/v1/version", run_diagnostics=diagnostics
        # )
        response = await self.federation_request(
            server_name,
            "/_matrix/federation/v1/version",
            force_rediscover=force_rediscover,
            diagnostics=diagnostics,
            **kwargs,
        )

        if diagnostics and response.diag_info is not None:
            # Update the diagnostics info, this is the only request can do this on and is only for the delegation test
            if response.http_code != 200:
                response.diag_info.connection_test_status = ResponseStatusType.ERROR
            else:
                response.diag_info.connection_test_status = ResponseStatusType.OK

        return response

    async def get_server_version_new(
        self,
        server_name: str,
        diagnostics: bool = False,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            server_name, "/_matrix/federation/v1/version", run_diagnostics=diagnostics
        )

        if diagnostics:
            # Update the diagnostics info, this is the only request can do this on and is only for the delegation test
            if response.http_code != 200:
                response.diagnostics.status.connection = StatusEnum.ERROR
            else:
                response.diagnostics.status.connection = StatusEnum.OK

        return response

    async def get_server_keys(self, server_name: str) -> MatrixResponse:
        # response = await self.federation_request(
        #     server_name,
        #     "/_matrix/key/v2/server",
        #     **kwargs,
        # )
        fedapi_logger.debug("Making server keys request to %s", server_name)
        response = await self.federation_transport.request(server_name, "/_matrix/key/v2/server")
        if response.http_code != 200:
            fedapi_logger.debug(
                "get_server_keys: %s: got %d: %s %s",
                server_name,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_server_notary_keys(
        self,
        fetch_server_name: str,
        from_server_name: str,
        minimum_valid_until_ts: int,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            from_server_name,
            f"/_matrix/key/v2/query/{fetch_server_name}",
            query_args=[("minimum_valid_until_ts", minimum_valid_until_ts)],
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_server_notary_keys: %s: got %d: %s %s",
                from_server_name,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_event(
        self,
        destination_server: str,
        origin_server: str,
        event_id: str,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/event/{event_id}",
            origin_server=origin_server,
        )

        return response

    async def get_state_ids(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/state_ids/{room_id}",
            query_args=[("event_id", event_id)],
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_state_ids: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_state(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/state/{room_id}",
            query_args=[("event_id", event_id)],
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_state: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_event_auth(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/event_auth/{room_id}/{event_id}",
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_event_auth: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_timestamp_to_event(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        utc_time_at_ms: int,
    ) -> MatrixResponse:
        # With no errors, will produce a json like:
        # {
        #    "event_id": "$somehash",
        #    "origin_server_ts": 123455676543whatever_int
        # }
        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/timestamp_to_event/{room_id}",
            query_args=[("dir", "b"), ("ts", utc_time_at_ms)],
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_timestamp_to_event: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_backfill(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        *,
        limit: str = "10",
    ) -> MatrixResponse:

        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/backfill/{room_id}",
            query_args=[("v", event_id), ("limit", limit)],
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_backfill: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_user_devices(
        self,
        origin_server: str,
        destination_server: str,
        user_mxid: str,
    ) -> MatrixResponse:
        # url = URL(
        #     f"https://{destination_server}/_matrix/federation/v1/user/devices/{mxid}"
        # )

        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/user/devices/{user_mxid}",
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_user_devices: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_room_alias_from_directory(
        self,
        origin_server: str,
        destination_server: str,
        room_alias: str | RoomAlias,
    ) -> MatrixResponse:
        response = await self.federation_transport.request(
            destination_server,
            "/_matrix/federation/v1/query/directory",
            query_args=[("room_alias", f"{room_alias}")],
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_room_alias_from_directory: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def put_pdu_transaction(
        self,
        origin_server: str,
        destination_server: str,
        pdus_to_send: Sequence[Dict[str, Any]],
    ) -> MatrixResponse:
        formatted_data: Dict[str, Any] = {}
        now = int(time.time() * 1000)
        formatted_data["origin"] = origin_server
        formatted_data["origin_server_ts"] = now
        formatted_data["pdus"] = []
        for pdu in pdus_to_send:
            formatted_data["pdus"].append(pdu)

        response = await self.federation_request(
            destination_server,
            f"/_matrix/federation/v1/send/{now}",
            method="PUT",
            content=formatted_data,
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "put_pdu_transaction: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def get_public_rooms(
        self,
        origin_server: str,
        destination_server: str,
        *,
        include_all_networks: bool = False,
        limit: int = 10,
        since: Optional[str] = None,
        third_party_instance_id: Optional[str] = None,
    ) -> MatrixResponse:
        query_args = [
            ("include_all_networks", str(include_all_networks).lower()),
            ("limit", limit),
        ]

        if since:
            query_args.append(("since", since))
        if third_party_instance_id:
            query_args.append(("third_party_instance_id", third_party_instance_id))

        response = await self.federation_transport.request(
            destination_server,
            "/_matrix/federation/v1/publicRooms",
            query_args=query_args,
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "get_public_rooms: %s: got %d: %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason or response.json_response,
            )

        return response

    async def make_join(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        user_id: str,
    ) -> MatrixResponse:
        # In an ideal world, this would expand correctly in aiohttp. In reality, it does not.
        # query_list={
        #     "ver": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]
        # },

        query_list = []
        for i in range(0, 12):
            query_list.extend([("ver", f"{i + 1}")])

        response = await self.federation_transport.request(
            destination_server,
            f"/_matrix/federation/v1/make_join/{room_id}/{user_id}",
            query_args=query_list,
            origin_server=origin_server,
        )

        if response.http_code != 200:
            fedapi_logger.debug(
                "make_join: %s: got %d: %s %s %s",
                destination_server,
                response.http_code,
                response.errcode,
                response.error or response.reason,
                response.json_response,
            )

        return response
