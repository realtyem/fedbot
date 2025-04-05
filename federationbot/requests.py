from typing import Any, Sequence, Union
import asyncio
import copy
import ipaddress
import json
import logging
import socket
import time

from aiohttp import ClientResponse, ClientSession, ClientTimeout, SocketTimeoutError, TCPConnector, client_exceptions
from signedjson.key import decode_signing_key_base64
from signedjson.sign import sign_json
from yarl import URL

from federationbot.errors import RedirectRetry, RequestClientError, RequestError, RequestServerError, RequestTimeout
from federationbot.resolver import (
    Diagnostics,
    IpAddressAndPort,
    ServerDiscoveryErrorResult,
    ServerDiscoveryResult,
    StatusEnum,
)
from federationbot.resolver.resolver import ServerDiscoveryResolver
from federationbot.responses import MatrixError, MatrixFederationResponse, MatrixResponse
from federationbot.tracing import make_fresh_trace_config

logger = logging.getLogger(__name__)


USER_AGENT_STRING = "AllYourServerBelongsToUs 0.1.1"

# Both are in seconds(float)
SOCKET_CONNECT_TIMEOUT = 10
SOCKET_READ_TIMEOUT = 10

CLIENT_TIMEOUT = ClientTimeout(
    # Don't limit the total connection time, as incremental reads are handled distinctly by sock_read
    total=None,
    # connect should be None, as sock_connect is behavior intended. This is for waiting for a connection from
    # the pool as well as establishing the connection itself.
    connect=None,
    # This is the most useful for detecting bad servers
    sock_connect=SOCKET_CONNECT_TIMEOUT,
    # This is the one that may have the longest time, as we wait for a server to send a response
    sock_read=SOCKET_READ_TIMEOUT,
    # defaults to 5, for roundups on timeouts
    # ceil_threshold=5.0,
)


async def raise_for_status_on_redirect(response: ClientResponse) -> None:
    if response.status in {301}:
        raise RedirectRetry(response.headers.get("Location"))


class FederationRequests:
    http_client: ClientSession
    server_discovery: ServerDiscoveryResolver

    def __init__(self, server_signing_keys: dict[str, str]) -> None:
        # resolver = ThreadedResolver()
        # resolver = CachingDNSResolver()
        # nameserver = Do53Nameserver("192.168.2.1")
        # self.dns_resolver = dns.asyncresolver.Resolver()
        # self.dns_resolver.nameservers = [nameserver]
        self.server_signing_keys = server_signing_keys
        connector = TCPConnector(
            use_dns_cache=True,
            ttl_dns_cache=300,
            family=socket.AddressFamily.AF_INET,  # type: ignore[no-member]
            limit=10000,
            limit_per_host=3,
            # resolver=resolver,  # type: ignore[arg-type]
            # happy_eyeballs_delay=None,
            # interleave=3,
            force_close=True,
        )
        self.http_client = ClientSession(
            connector=connector,
            trace_configs=[make_fresh_trace_config()],
            raise_for_status=raise_for_status_on_redirect,
        )
        self.json_decoder = json.JSONDecoder()
        self.server_discovery = ServerDiscoveryResolver(self._request)

    async def request(
        self,
        server_name: str,
        path: str,
        method: str = "GET",
        query_args: Sequence[tuple[str, Any]] | None = None,
        content: dict[str, Any] | None = None,
        origin_server: str | None = None,
        ip_address_and_port_tuple_override: list[IpAddressAndPort] | None = None,
        run_diagnostics: bool = False,
    ) -> MatrixResponse:
        """
        Place a federation request and retrieve the result, including errors
        Args:
            server_name: the server begin queried
            path: the URL path
            method: the HTTP method to use
            query_args: any args to append to the path, a list of tuples is best. Such as [(ver, "1"), (ver, "2")]
            content: any outgoing content, typically JSON in the form of a dict
            origin_server: usually the local server, decides what server signing keys to use to auth the outgoing request
            ip_address_and_port_tuple_override: only used during server discovery
            run_diagnostics: only used during server discovery

        Returns:

        """
        server_result = await self.server_discovery.discover_server(server_name, run_diagnostics=run_diagnostics)
        logger.debug("request: %s: server result retrieved: %r", server_name, server_result)
        if isinstance(server_result, ServerDiscoveryErrorResult):
            return MatrixError(
                reason=server_result.error,
                diagnostics=server_result.diagnostics,
                server_result=server_result,
                time_taken=0,
            )

        assert isinstance(server_result, ServerDiscoveryResult)
        diagnostics = None
        if run_diagnostics:
            diagnostics = Diagnostics()
            if server_result.diagnostics is not None:
                diagnostics.output_list = server_result.diagnostics.output_list.copy()
                diagnostics.status = copy.copy(server_result.diagnostics.status)
        if ip_address_and_port_tuple_override:
            list_of_ip_addresses_and_ports = ip_address_and_port_tuple_override
        else:
            list_of_ip_addresses_and_ports = server_result.list_of_resolved_addresses

        # TODO: Until IPv6 works on my server, curate the list of ip addresses to only have IPv4
        list_of_only_ipv4_address_tuples = []
        start_time = time.time()
        for ip_address_and_port in list_of_ip_addresses_and_ports:
            try:

                _ip_address = ipaddress.ip_address(ip_address_and_port.ip_address)
                if isinstance(_ip_address, ipaddress.IPv4Address):
                    list_of_only_ipv4_address_tuples.append(ip_address_and_port)
            except ValueError:
                pass
        try:
            list_of_coros: list[asyncio.Task] = []
            for ip_address in list_of_only_ipv4_address_tuples:
                coro = self._request(
                    ip_address,
                    server_name,
                    path,
                    method,
                    query_args=query_args,
                    content=content,
                    origin_server=origin_server,
                    host_header=server_result.host_header,
                    sni_host_name=server_result.sni,
                )
                list_of_coros.append(asyncio.create_task(coro))

            try:
                done, pending = await asyncio.wait(list_of_coros, return_when=asyncio.FIRST_COMPLETED)
            except ValueError as e:
                # TODO: can remove this after IPv6 is enabled on my server
                logger.exception("%s: %r", server_name, e)
                raise RequestError("This server can not make requests to IPv6") from e

            for task in pending:
                task.cancel()
            response: ClientResponse = done.pop().result()

        except RequestError as e:
            stop_time = time.time()
            if run_diagnostics and diagnostics:
                diagnostics.status.connection = StatusEnum.ERROR
                diagnostics.output_list.append(f"    Error: {e.reason}")
            error_return = MatrixError(
                reason=e.reason, diagnostics=diagnostics, server_result=server_result, time_taken=stop_time - start_time
            )
            return error_return
        except Exception as e:
            logger.debug("request: Error: %r: %s, %s", e, server_name, path)
            raise

        async with response:
            status_code = response.status
            headers = response.headers
            context_tracing = response._traces[0]._trace_config_ctx  # noqa: W0212  # pylint:disable=protected-access

            # There can be a range of status codes, but only 404 specifically is called out
            if 200 <= status_code < 600 and not status_code == 404:
                try:
                    text_content = await response.text()
                    response_content = self.json_decoder.decode(text_content)
                    stop_time = time.time()

                except json.decoder.JSONDecodeError as e:
                    if run_diagnostics and diagnostics:
                        diagnostics.status.connection = StatusEnum.ERROR
                        diagnostics.output_list.append(f"    Code: {status_code}, Error: {e.msg}")
                    return MatrixError(
                        http_code=status_code,
                        reason="JSONDecodeError: No usable data in response",
                        diagnostics=diagnostics,
                        server_result=server_result,
                        time_taken=time.time() - start_time,
                    )
                except client_exceptions.ServerTimeoutError as e:
                    if run_diagnostics and diagnostics:
                        diagnostics.status.connection = StatusEnum.ERROR
                        diagnostics.output_list.append(f"    Code: {status_code}, Error: {e.strerror}")
                    return MatrixError(
                        http_code=status_code,
                        reason=f"{e.__class__.__name__}: Timed out while reading response",
                        diagnostics=diagnostics,
                        server_result=server_result,
                        time_taken=time.time() - start_time,
                    )
            else:
                if run_diagnostics and diagnostics:
                    diagnostics.output_list.append(f"    Code: {status_code}")

                return MatrixError(
                    http_code=status_code,
                    diagnostics=diagnostics,
                    server_result=server_result,
                    time_taken=time.time() - start_time,
                )

        # TODO: parse the headers for the cache control stuff, sort out ttl options
        if run_diagnostics and diagnostics:
            diagnostics.status.connection = StatusEnum.OK

        return MatrixFederationResponse(
            http_code=status_code,
            json_response=response_content or {},
            tracing_context=context_tracing,
            headers=headers,
            diagnostics=diagnostics,
            server_result=server_result,
            time_taken=stop_time - start_time,
        )

    async def _request(
        self,
        ip_address_and_port: IpAddressAndPort,
        host_name: str,
        path: str,
        method: str = "GET",
        query_args: Sequence[tuple[str, Any]] | None = None,
        content: dict[str, Any] | None = None,
        origin_server: str | None = None,
        host_header: str | None = None,
        sni_host_name: str | None = None,
    ) -> ClientResponse:
        _ip_address = ip_address_and_port.ip_address
        _port = ip_address_and_port.port
        # TODO: this could be better
        url_object = URL.build(
            scheme="https",
            host=_ip_address,
            port=_port,
            path=path,
            query=query_args,
            encoded=False,
        )

        # url = f"https://{_ip_address if _ip_address else server_name}{path}"

        request_headers = {"User-Agent": USER_AGENT_STRING, "Host": host_header if host_header else host_name}
        if origin_server:
            # Implying an origin server means this request must have auth attached
            # assert request_headers is not None
            request_headers["Authorization"] = authorization_headers(
                origin_name=origin_server,
                origin_signing_key=self.server_signing_keys[origin_server],
                destination_name=host_name,
                request_method=method,
                uri=str(url_object.relative()),
                content=content,
            )

        try:
            logger.debug("Trying request: %s%s", host_name, path)

            # Because we have allow_redirects=False and with the ClientSession watching all responses for the
            # 301, this may raise a RedirectRetry exception. We trap that on the outside of this method, so it
            # can requery dns and log to diagnostics
            response = await self.http_client.request(
                method,
                url_object,
                headers=request_headers,
                server_hostname=sni_host_name,
                timeout=CLIENT_TIMEOUT,
                allow_redirects=False,
            )

        except RedirectRetry as e:
            raise e

        # Split the different exceptions up based on where the information is extracted from
        except client_exceptions.ClientConnectorCertificateError as e:
            # This is one of the errors I found while probing for SNI TLS
            raise RequestServerError(  # pylint: disable=bad-exception-cause
                reason=f"{e.__class__.__name__}, {str(e.certificate_error)}",
            ) from e

        except (
            client_exceptions.ClientConnectorSSLError,
            client_exceptions.ClientSSLError,
        ) as e:
            # This is one of the errors I found while probing for SNI TLS
            raise RequestServerError(reason=f"{e.__class__.__name__}, {e.strerror}") from e

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
                raise RequestClientError(reason=f"{e.__class__.__name__}, {e.os_error.strerror}") from e  # type: ignore[attr-defined]

            raise RequestClientError(reason=f"{e.__class__.__name__}, {e.strerror}") from e

        except (
            # For exceptions during http proxy handling(non-200 responses)
            client_exceptions.ClientHttpProxyError,  # e.message
            # For exceptions after getting a response, maybe unexpected termination?
            client_exceptions.ClientResponseError,  # e.message, base class, possibly e.status too
            # For exceptions where the server disconnected early
            client_exceptions.ServerDisconnectedError,  # e.message
        ) as e:
            raise RequestError(reason=f"{e.__class__.__name__}, {str(e.message)}") from e

        except (
            client_exceptions.ConnectionTimeoutError,
            SocketTimeoutError,
            client_exceptions.ServerTimeoutError,
        ) as e:
            # SocketTimeoutError is hit when aiohttp cancels a request that takes to long
            # ServerTimeoutError is asyncio.TimeoutError under it's hood
            raise RequestTimeout(reason=f"{e.__class__.__name__} after {SOCKET_CONNECT_TIMEOUT} seconds") from e

        #     socket.gaierror,
        #     ConnectionRefusedError,
        #     # Broader OS error
        #     OSError,
        #     client_exceptions.ServerFingerprintMismatch,  # e.expected, e.got
        #     client_exceptions.InvalidURL,  # e.url
        #     client_exceptions.ServerConnectionError,  # e
        #     client_exceptions.ClientConnectionError,  # e
        #     client_exceptions.ClientError,  # e
        except (
            client_exceptions.ClientPayloadError,  # e
            Exception,  # e
        ) as e:
            logger.error("Had a problem while placing REQUEST to %s%s", host_name, path, exc_info=True)
            raise RequestError(reason=f"{e.__class__.__name__}, {str(e)}") from e

        return response


def authorization_headers(
    origin_name: str,
    origin_signing_key: str,
    destination_name: str,
    request_method: str,
    uri: str,
    content: Union[str, dict[str, Any]] | None = None,
) -> str:
    # Extremely borrowed from Matrix spec docs, linked above. Spelunked a bit into
    # Synapse code to identify how the signing key is stored and decoded.
    request_json: dict[str, Any] = {
        "method": request_method,
        "uri": uri,
        "origin": origin_name,
        "destination": destination_name,
    }
    algorithm, version, key_base64 = origin_signing_key.split()

    key = decode_signing_key_base64(algorithm, version, key_base64)
    if content is not None:
        request_json["content"] = content

    # canon_request_json = canonical_json(request_json)
    signed_json = sign_json(request_json, origin_name, key)

    authorization_header = ""

    for key, sig in signed_json["signatures"][origin_name].items():
        # 'X-Matrix origin="%s",key="%s",sig="%s",destination="%s"'
        #
        # authorization_header = f'X-Matrix origin=\"{origin_name}\",key=\"{key}\",sig=\"{sig}\",destination=\"{destination_name}\"'
        authorization_header = (
            f'X-Matrix origin="{origin_name}",key="{key}",sig="{sig}",destination="{destination_name}"'
        )

    return authorization_header
