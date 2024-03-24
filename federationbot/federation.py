from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from asyncio import Queue
import asyncio
import json
import ssl
import time

from aiohttp import ClientResponse, ClientSession, client_exceptions
from mautrix.types import EventID
from mautrix.util.logging import TraceLogger
from signedjson.key import decode_signing_key_base64, decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, sign_json, verify_signed_json
from yarl import URL

from federationbot.cache import LRUCache
from federationbot.delegation import (
    DelegationHandler,
    check_and_maybe_split_server_name,
)
from federationbot.errors import ServerUnavailable
from federationbot.events import (
    CreateRoomStateEvent,
    Event,
    EventBase,
    EventError,
    RoomMemberStateEvent,
    determine_what_kind_of_event,
    redact_event,
)
from federationbot.responses import (
    FederationBaseResponse,
    FederationErrorResponse,
    FederationServerKeyResponse,
    FederationVersionResponse,
    ServerVerifyKeys,
)
from federationbot.server_result import (
    DiagnosticInfo,
    ResponseStatusType,
    ServerResult,
    ServerResultError,
)
from federationbot.types import KeyContainer, KeyID, SignatureVerifyResult
from federationbot.utils import full_dict_copy, get_domain_from_id


class FederationHandler:
    def __init__(
        self,
        http_client: ClientSession,
        logger: TraceLogger,
        bot_mxid: str,
        server_signing_keys: Dict[str, str],
    ):
        self.http_client = http_client
        self.logger = logger
        self.hosting_server = get_domain_from_id(bot_mxid)
        self.bot_mxid = bot_mxid
        self.server_signing_keys = server_signing_keys
        self.json_decoder = json.JSONDecoder()
        self.delegation_handler = DelegationHandler(self.logger)
        # Map the key to (server_name, event_id) -> Event
        self._events_cache: LRUCache[Tuple[str, str], EventBase] = LRUCache()
        # Map this cache to server_name -> ServerResult
        self._server_discovery_cache: LRUCache[str, ServerResult] = LRUCache()
        self._server_keys_cache: LRUCache[str, ServerVerifyKeys] = LRUCache()

    async def stop(self) -> None:
        # For stopping the cleanup task on these caches
        await self._events_cache.stop()
        await self._server_discovery_cache.stop()
        await self._server_keys_cache.stop()

    async def _federation_request(
        self,
        destination_server_name: str,
        path: str,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        origin_server: Optional[str] = None,
        server_result: Optional[ServerResult] = None,
        content: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 10.0,
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
            method: GET, POST, etc
            origin_server: if authing this request, the server originating the request
            server_result: Allows access to server discovery data, like port, host
                header, and sni data
            content: if not a GET request, the content to send
            timeout_seconds: float

        Returns: A ClientResponse aiohttp context manager thingy

        """
        # Use the URL class to build the parts, as otherwise the query
        # parameters don't encode right and fail the JSON signing process verify
        # on the other end. I suspect it has to do with sigil encoding.
        #
        # From what I understand, having a port as 'None' will cause the URL to select
        # the most likely port based on the scheme. Sounds handy, use that.
        if server_result:
            if server_result.unhealthy:
                # If this server was attempted at some point and errored, there is no
                # point trying again until the cache entry is replaced.
                raise ServerUnavailable(f"{server_result.unhealthy}")

            destination_port = int(server_result.port)
            resolved_destination_server = server_result.get_host()
            server_hostname_sni = (
                server_result.sni_server_name if server_result.use_sni else None
            )
            request_headers = {"Host": server_result.host_header}
            # self.logger.info(f"{destination_server_name}, host: {server_result.host}, host_header: {server_result.host_header}, sni_server_name: {server_result.sni_server_name}")

        else:
            destination_port = None
            resolved_destination_server = destination_server_name
            server_hostname_sni = None
            request_headers = None

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

        try:
            response = await self.http_client.request(
                method=method,
                url=url_object,
                headers=request_headers,
                timeout=timeout_seconds,
                server_hostname=server_hostname_sni,
                json=content,
            )
        except Exception as e:
            raise e
        else:
            return response

    async def federation_request(
        self,
        destination_server_name: str,
        path: str,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        skip_discovery: bool = False,
        force_rediscover: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
        origin_server: Optional[str] = None,
        content: Optional[Dict[str, Any]] = None,
    ) -> FederationBaseResponse:
        """
        Retrieve json response from over federation. This outer-level function handles
        caching of server discovery processes and catching errors. Calls
        _federation_request() inside to handle authing and places the actual request.

        Args:
            destination_server_name: the server name being sent to, delegation is
                handled within
            path: The path component of the outgoing url
            query_args: the query component to send
            method: The method to use for the request: GET, PUT, etc
            skip_discovery: if delegation checking should be skipped
            force_rediscover: in case we need to bypass the cache to redo server
                discovery
            diagnostics: Collect diagnostic data. Errors are always collected
            timeout_seconds: Float of how many seconds before timeout
            origin_server: The server to send the request as, signing keys will be
                required to be setup in the config files for authed requests
            content: for non-GET requests, the Dict that will be transformed into json
                to send
        Returns:
            FederationBaseResponse: Either a Base or Error variant
        """
        result_dict: Dict[str, Any] = {}
        error_reason: Optional[str] = None
        code = 0
        headers = None
        diag_info = DiagnosticInfo(diagnostics)
        request_info = None
        server_result = None

        # If the server is delegated in some way, this will take care of making sure we
        # get a usable host:port
        if not skip_discovery:
            server_result = self._server_discovery_cache.get(destination_server_name)
            if not server_result or force_rediscover:
                # self.logger.warning(
                #     f"cache entry not found for {destination_server_name}"
                # )
                server_result = await self.delegation_handler.maybe_handle_delegation(
                    destination_server_name,
                    self.federation_request,
                    diag_info=diag_info,
                )

        try:
            if server_result:
                # self.logger.info(
                #     f"Making real federation request: {destination_server_name}"
                # )
                # These only get filled in when diagnostics is True
                # This will add the word "Checking: " to the front of "Connectivity"
                diag_info.mark_step_num("Connectivity")
                diag_info.add(f"Making request to {server_result.get_host()}{path}")

            response = await self._federation_request(
                destination_server_name=destination_server_name,
                path=path,
                query_args=query_args,
                method=method,
                origin_server=origin_server,
                server_result=server_result,
                content=content,
                timeout_seconds=timeout_seconds,
            )

        except ConnectionRefusedError:
            diag_info.error("ConnectionRefusedError")
            error_reason = "ConnectionRefusedError"

        except client_exceptions.ClientConnectorCertificateError as e:
            assert isinstance(e._certificate_error, ssl.SSLError)
            diag_info.error(f"SSL Certificate Error, {e._certificate_error.reason}")
            error_reason = f"SSL Certificate Error, {e._certificate_error.reason}"
            # This is one of the two errors I found while probing for SNI TLS
            code = -1

        except client_exceptions.ClientConnectorSSLError as e:
            diag_info.error(f"Client Connector SSL Error, {e}")
            error_reason = f"Client Connector SSL Error, {e}"
            code = -1

        except client_exceptions.ServerDisconnectedError:
            diag_info.error("Server Disconnect Error")
            error_reason = "Server Disconnect Error"

        # except client_exceptions.ConnectionTimeoutError as e:

        # except client_exceptions.SocketTimeoutError as e:

        except client_exceptions.ServerTimeoutError:
            diag_info.error("Server Timeout Error")
            error_reason = "Server Timeout Error"

        except client_exceptions.ServerFingerprintMismatch:
            diag_info.error("Server Fingerprint Mismatch Error")
            error_reason = "Server Fingerprint Mismatch Error"

        except client_exceptions.ServerConnectionError:
            diag_info.error("Server Connection Error")
            error_reason = "ServerConnectionError"

        except client_exceptions.ClientSSLError as e:
            diag_info.error(f"ClientSSLError: {e.strerror}")
            error_reason = f"Client SSL Error: {e.strerror}"
            # This is one of the errors I found while probing for SNI TLS
            code = -1

        except client_exceptions.ClientProxyConnectionError:
            diag_info.error("Client Proxy Connection Error")
            error_reason = "Client Proxy Connection Error"

        except client_exceptions.ClientConnectorError as e:
            # code = 0
            diag_info.error(f"ClientConnectorError: {e.strerror}")
            error_reason = f"Client Connector Error: {e.strerror}"

        except client_exceptions.ClientHttpProxyError:
            diag_info.error("Client HTTP Proxy Error")
            error_reason = "Client HTTP Proxy Error"

        except client_exceptions.WSServerHandshakeError:
            # Not sure this one will ever be used...
            pass
        except client_exceptions.ContentTypeError:
            # Pretty sure will never hit this one either, as it's not enforced here
            diag_info.error("Content Type Error")
            error_reason = "Content Type Error"
        except client_exceptions.ClientResponseError:
            diag_info.error("Client Response Error")
            error_reason = "Client Response Error"

        except client_exceptions.ClientPayloadError:
            diag_info.error("Client Payload Error")
            error_reason = "Client Payload Error"

        except client_exceptions.InvalidURL:
            diag_info.error("InvalidURL Error")
            error_reason = "InvalidURL Error"

        except client_exceptions.ClientOSError:
            diag_info.error("Client OS Error")
            error_reason = "Client OS Error"

        except client_exceptions.ClientConnectionError:
            diag_info.error("Client Connection Error")
            error_reason = "Client Connection Error"

        except client_exceptions.ClientError:
            diag_info.error("Client Error")
            error_reason = "Client Error"

        except ServerUnavailable as e:
            diag_info.error(f"{e}")
            error_reason = f"{e}"

        except asyncio.TimeoutError:
            diag_info.error(
                "TimeoutError, this server probably doesn't exist(or is taking to long)"
            )
            error_reason = "Timed out. Is this server online?"
        except Exception as e:
            self.logger.info(
                f"federation_request: General Exception: for {destination_server_name}"
                f":\n {e}"
            )
            diag_info.error(f"General Exception: {e}")
            error_reason = "General Exception"

        else:
            # The server was responsive, but may not have returned something useful
            async with response:
                code = response.status
                reason = response.reason
                # for errors that are not JSON decoding related(which is overridden
                # below)
                error_reason = response.reason
                headers = response.headers
                request_info = response.request_info

                diag_info.add(f"Request status: code:{code}, reason: {reason}")

                try:
                    result_dict = await response.json()

                except client_exceptions.ContentTypeError:
                    diag_info.error(
                        "Response had Content-Type: "
                        f"{response.headers.get('Content-Type', 'None Found')}"
                    )
                    diag_info.add(
                        "Expected Content-Type of 'application/json', will try "
                        "work-around"
                    )

                # Sometimes servers don't have their well-known(or other things)
                # set to return a content-type of `application/json`, try and
                # handle it.
                if not result_dict:
                    try:
                        result = await response.text()
                        result_dict = self.json_decoder.decode(result)
                    except json.decoder.JSONDecodeError:
                        # if debug:
                        #     self.logger.info(f"original result: {result}")
                        diag_info.error("JSONDecodeError, work-around failed")
                        error_reason = "No/bad JSON returned"

                if not result_dict:
                    diag_info.add(f"No usable data in response")

        finally:
            # The request is complete. If the server wasn't actually there, the code
            # will be <= 0. Anything higher means the server result can be cached, as it
            # means a successful contact.
            if not server_result:
                # This will be hit when checking for well-known, it gives us an initial
                # ServerResult to base further queries on. Don't save it in the cache
                host, port = check_and_maybe_split_server_name(destination_server_name)

                server_result = ServerResult(
                    host=host, port=port if port else "", diag_info=diag_info
                )

            else:
                server_result.unhealthy = error_reason if code <= 0 else None
                # self.logger.warning(f"saving {server_result.host} to cache")
                self._server_discovery_cache.set(server_result.host, server_result)

            if code != 200:
                diag_info.error(f"Request to {path} failed")
                return FederationErrorResponse(
                    code,
                    error_reason,
                    response_dict=result_dict,
                    server_result=server_result,
                    list_of_errors=diag_info.list_of_results,
                    headers=headers,
                    request_info=request_info,
                )
            else:
                # Don't need a success diagnostic message here, the one above works fine
                return FederationBaseResponse(
                    code,
                    reason,
                    response_dict=result_dict,
                    server_result=server_result,
                    list_of_errors=diag_info.list_of_results,
                    headers=headers,
                    request_info=request_info,
                )

    async def get_server_version(
        self,
        server_name: str,
        force_rediscover: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
    ) -> FederationBaseResponse:
        response = await self.federation_request(
            destination_server_name=server_name,
            path="/_matrix/federation/v1/version",
            method="GET",
            force_rediscover=force_rediscover,
            diagnostics=diagnostics,
            timeout_seconds=timeout_seconds,
        )
        # TODO: This was behaving oddly, disect it later
        # if response.status_code == 404:
        #     response.server_result.diag_info.connection_test_status = (
        #         ResponseStatusType.NONE
        #     )
        if response.status_code != 200:
            response.server_result.diag_info.connection_test_status = (
                ResponseStatusType.ERROR
            )
        else:
            response.server_result.diag_info.connection_test_status = (
                ResponseStatusType.OK
            )

        if isinstance(response, FederationErrorResponse):
            return response
        else:
            return FederationVersionResponse.from_response(response)

    async def _get_server_keys(
        self, server_name: str, timeout: float = 10.0
    ) -> Union[FederationServerKeyResponse, FederationErrorResponse]:
        response = await self.federation_request(
            destination_server_name=server_name,
            path="/_matrix/key/v2/server",
            method="GET",
            timeout_seconds=timeout,
        )
        if isinstance(response, FederationErrorResponse):
            return response
        else:
            return FederationServerKeyResponse.from_response(response)

    async def get_server_keys(
        self, server_name: str, timeout: float = 10.0
    ) -> Union[ServerVerifyKeys, FederationErrorResponse]:
        response = await self._get_server_keys(server_name=server_name, timeout=timeout)

        if isinstance(response, FederationErrorResponse):
            return response
        else:
            return response.server_verify_keys

    async def get_server_keys_from_notary(
        self, fetch_server_name: str, from_server_name: str, timeout: float = 10.0
    ) -> FederationBaseResponse:
        minimum_valid_until_ts = int(time.time() * 1000) + (
            30 * 60 * 1000
        )  # Add 30 minutes
        response = await self.federation_request(
            destination_server_name=from_server_name,
            path=f"/_matrix/key/v2/query/{fetch_server_name}",
            query_args=[("minimum_valid_until_ts", minimum_valid_until_ts)],
            method="GET",
            timeout_seconds=timeout,
        )
        return response

    async def get_server_key(
        self, for_server_name: str, key_id_needed: str, timeout: float = 10.0
    ) -> Dict[KeyID, KeyContainer]:
        # TODO: I feel like this is incomplete in error handling
        key_id_formatted = KeyID(key_id_needed)
        cached_server_keys = self._server_keys_cache.get(for_server_name)
        if (
            cached_server_keys is not None
            and key_id_formatted in cached_server_keys.verify_keys
        ):
            return cached_server_keys.verify_keys
        server_verify_keys = await self.get_server_keys(for_server_name, timeout)
        if isinstance(server_verify_keys, FederationErrorResponse):
            raise ServerUnavailable(server_verify_keys.reason)
        else:
            self._server_keys_cache.set(for_server_name, server_verify_keys)
            verify_keys = server_verify_keys.verify_keys
            if key_id_formatted in verify_keys:
                return server_verify_keys.verify_keys
            else:
                notary_server_verify_keys = await self.get_server_keys_from_notary(
                    fetch_server_name=for_server_name,
                    from_server_name=self.hosting_server,
                )
                if isinstance(notary_server_verify_keys, FederationErrorResponse):
                    raise ServerUnavailable(notary_server_verify_keys.reason)
                else:
                    cached_server_keys = self._server_keys_cache.get(for_server_name)
                    if cached_server_keys is None:
                        cached_server_keys = ServerVerifyKeys({})
                    cached_server_keys.update_key_data_from_list(
                        notary_server_verify_keys.response_dict
                    )
                    self._server_keys_cache.set(for_server_name, cached_server_keys)

                    if key_id_formatted in cached_server_keys.verify_keys:
                        return cached_server_keys.verify_keys
        # TODO: I don't think this is right
        return {}

    async def verify_signatures_and_annotate_event(
        self,
        event: Event,
        room_version: int,
    ) -> None:
        # There are two places that discuss verifying the signatures:

        # 1. S2S section 27.2
        #  First the signature is checked. The event is redacted following the
        #  redaction algorithm, and the resultant object is checked for a signature
        #  from the originating server, following the algorithm described in Checking
        #  for a signature. Note that this step should succeed whether we have been
        #  sent the full event or a redacted copy.
        #
        #  The signatures expected on an event are:
        #
        #  The sender’s server, unless the invite was created as a result of 3rd party
        #  invite. The sender must already match the 3rd party invite, and the server
        #  which actually sends the event may be a different server.
        #
        #  For room versions 1 and 2, the server which created the event_id. Other
        #  room versions do not track the event_id over federation and therefore do
        #  not need a signature from those servers.

        # 2. Appendices 3.3

        # To check if an entity has signed a JSON object an implementation does the
        # following:
        #
        #  1. Checks if the signatures member of the object contains an entry with the
        #     name of the entity. If the entry is missing then the check fails.
        #  2. Removes any signing key identifiers from the entry with algorithms it
        #     doesn’t understand. If there are no signing key identifiers left then the
        #     check fails.
        #  3. Looks up verification keys for the remaining signing key identifiers
        #     either from a local cache or by consulting a trusted key server. If it
        #     cannot find a verification key then the check fails.
        #  4. Decodes the base64 encoded signature bytes. If base64 decoding fails then
        #     the check fails.
        #  5. Removes the signatures and unsigned members of the object.
        #  6. Encodes the remainder of the JSON object using the Canonical JSON
        #     encoding.
        #  7. Checks the signature bytes against the encoded object using the
        #     verification key. If this fails then the check fails. Otherwise the check
        #     succeeds.

        # So it looks like in summary:
        # 1. Redact the event to strip off keys that aren't needed
        # 2. Make sure we have the various servers that have signed the event's public
        #    keys. Should already have the key decoded from base64 in the KeyContainer
        # 3. Create the VerifyKey from the decoded server key
        # 4. Run each servers key's through verify_signed_json(), which will:
        #    * Strip off all signatures and the 'unsigned' section of an event
        #    * Does the actual verifying
        base_event = full_dict_copy(event.raw_data)
        signatures = base_event.get("signatures", {})
        redacted_base_event = redact_event(room_version, base_event)
        for server_name, server_key_set in signatures.items():
            for server_key_id in server_key_set.keys():
                remote_server_keys = await self.get_server_key(
                    server_name, server_key_id
                )

                remote_server_key = remote_server_keys.get(KeyID(server_key_id), None)
                if remote_server_key is not None:
                    verify_key = decode_verify_key_bytes(
                        server_key_id, remote_server_key.key.decoded_key
                    )
                    try:
                        verify_signed_json(redacted_base_event, server_name, verify_key)
                    except SignatureVerifyException:
                        event.signatures_verified[
                            server_name
                        ] = SignatureVerifyResult.FAIL
                    else:
                        event.signatures_verified[
                            server_name
                        ] = SignatureVerifyResult.SUCCESS

    async def get_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        event_id: str,
        timeout: float = 10.0,
    ) -> Dict[str, EventBase]:
        """
        Retrieves a single Event from a server. Since the event id will be known, it can
         be included in the retrieved Event.
        Args:
            origin_server: The server placing the request
            destination_server: The server receiving the request
            event_id: The opaque string of the id given to the Event
            timeout:

        Returns: A tuple containing the FederationResponse received and the Event
            contained in a List

        """

        new_event_base = self._events_cache.get((destination_server, event_id))
        if new_event_base:
            # Only successful events are cached
            return {event_id: new_event_base}

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/event/{event_id}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.status_code != 200:
            # self.logger.warning(
            #     f"get_event_from_server had an error\n{event_id}\n{response.status_code}:{response.reason}"
            # )
            new_event_base = EventError(
                EventID(event_id),
                {"error": f"{response.reason}", "errcode": f"{response.status_code}"},
            )

            return {event_id: new_event_base}

        pdu_list = response.response_dict.get("pdus", [])
        for data in pdu_list:
            # This path is only taken on success, errors are sorted above
            new_event_base = determine_what_kind_of_event(EventID(event_id), data)
            self._events_cache.set((destination_server, event_id), new_event_base)

        assert new_event_base is not None
        return {event_id: new_event_base}

    async def get_events_from_server(
        self,
        origin_server: str,
        destination_server: str,
        events_list: Union[Sequence[str], Set[str]],
        timeout: float = 10.0,
    ) -> Dict[str, EventBase]:
        """
        Retrieve multiple Events from a given server. Uses Async Tasks and a Queue to
        be efficient. Creates number of event_ids up to 10 Tasks for concurrency.

        Args:
            origin_server: The server to auth the request with
            destination_server: The server to ask about the Event
            events_list: Either a Sequence or a Set of Event ID strings
            timeout:

        Returns: A mapping of the Event ID to the Event(or EventError)

        """
        # Keep both the response and the actual event, if there was an error it will be
        # in the response and the event won't exist here
        event_to_event_base: Dict[str, EventBase] = {}

        async def _get_event_worker(queue: Queue) -> None:
            while True:
                worker_event_id: str = await queue.get()
                try:
                    event_base_dict = await asyncio.wait_for(
                        self.get_event_from_server(
                            origin_server=origin_server,
                            destination_server=destination_server,
                            event_id=worker_event_id,
                        ),
                        timeout=timeout,
                    )

                except asyncio.TimeoutError:
                    error_event = EventError(
                        EventID(worker_event_id),
                        {"error": "Request Timed Out", "errcode": "Timeout err"},
                    )
                    event_to_event_base[worker_event_id] = error_event
                except Exception as e:
                    error_event = EventError(
                        EventID(worker_event_id),
                        {"error": f"{e}", "errcode": "Plugin error"},
                    )
                    event_to_event_base[worker_event_id] = error_event

                else:
                    for r_event_id, event_base in event_base_dict.items():
                        # Add this newly retrieved Event data to the outside dict that
                        # is being returned.
                        event_to_event_base[r_event_id] = event_base

                finally:
                    queue.task_done()

        event_queue: Queue[str] = asyncio.Queue()
        for event_id in events_list:
            await event_queue.put(event_id)

        tasks = []
        for i in range(min(len(events_list), 3)):
            task = asyncio.create_task(_get_event_worker(event_queue))
            tasks.append(task)

        await event_queue.join()

        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        return event_to_event_base

    async def get_state_ids_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        timeout: float = 10.0,
    ) -> Tuple[List[str], List[str],]:
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/state_ids/{room_id}",
            query_args=[("event_id", event_id)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        pdu_list = response.response_dict.get("pdu_ids", [])
        auth_chain_list = response.response_dict.get("auth_chain_ids", [])

        return pdu_list, auth_chain_list

    async def get_state_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        timeout: float = 60.0,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/state/{room_id}",
            query_args=[("event_id", event_id)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        pdus_list = response.response_dict.get("pdus", [])
        auth_chain_list = response.response_dict.get("auth_chain", [])

        return pdus_list, auth_chain_list

    async def get_event_auth_for_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        timeout: float = 10.0,
    ) -> FederationBaseResponse:
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/event_auth/{room_id}/{event_id}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        return response

    async def get_timestamp_to_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        utc_time_at_ms: int,
        timeout: float = 10.0,
    ) -> FederationBaseResponse:
        # With no errors, will produce a json like:
        # {
        #    "event_id": "$somehash",
        #    "origin_server_ts": 123455676543whatever_int
        # }
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/timestamp_to_event/{room_id}",
            query_args=[("dir", "b"), ("ts", utc_time_at_ms)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        return response

    async def get_backfill_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        limit: str = "10",
        timeout: float = 10.0,
    ) -> FederationBaseResponse:

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/backfill/{room_id}",
            query_args=[("v", event_id), ("limit", limit)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        return response

    async def get_user_devices_from_server(
        self,
        origin_server: str,
        destination_server: str,
        user_mxid: str,
        timeout: float = 10.0,
    ) -> FederationBaseResponse:
        # url = URL(
        #     f"https://{destination_server}/_matrix/federation/v1/user/devices/{mxid}"
        # )

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/user/devices/{user_mxid}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        return response

    async def get_room_alias_from_server(
        self,
        origin_server: str,
        # destination_server: Optional[str],
        room_alias: str,
        timeout: float = 10.0,
    ) -> FederationBaseResponse:
        try:
            _, destination_server = room_alias.split(":", maxsplit=1)
        except ValueError:
            return FederationErrorResponse(
                status_code=0,
                status_reason="Malformed Room Alias: missing a domain",
                response_dict={},
                server_result=ServerResultError(),
            )
        response = await self.federation_request(
            destination_server_name=destination_server,
            path="/_matrix/federation/v1/query/directory",
            query_args=[("room_alias", room_alias)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        return response

    async def discover_room_version(
        self, origin_server: str, destination_server: str, room_id: str
    ) -> str:
        creation_event_list = await self.filter_state_for_type(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            state_type_str="m.room.create",
        )
        # In this case, there will ever be one creation event, so slice the list
        creation_event = creation_event_list[0]
        # Really need to figure out a better way of doing this. Some kind of Type dance
        assert isinstance(creation_event, CreateRoomStateEvent)
        room_version = creation_event.room_version
        return str(room_version)

    async def filter_state_for_type(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        state_type_str: str,
    ) -> List[EventBase]:
        now = int(time.time() * 1000)
        event_id = None
        ts_response = await self.get_timestamp_to_event_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            utc_time_at_ms=now,
        )
        if not isinstance(ts_response, FederationErrorResponse):
            event_id = ts_response.response_dict.get("event_id")

        assert event_id is not None
        state_ids, _ = await self.get_state_ids_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            event_id=event_id,
            # timeout=,
        )
        state_events = await self.get_events_from_server(
            origin_server=origin_server,
            destination_server=destination_server,
            events_list=state_ids,
        )
        event_base_list = []
        for event in state_events.values():
            if event.event_type == state_type_str:
                event_base_list.append(event)

        return event_base_list


# https://spec.matrix.org/v1.9/server-server-api/#request-authentication
# {
#     "method": "GET",
#     "uri": "/target",
#     "origin": "origin.hs.example.com",
#     "destination": "destination.hs.example.com",
#     "content": <JSON-parsed request body>,
#     "signatures": {
#         "origin.hs.example.com": {
#             "ed25519:key1": "ABCDEF..."
#         }
#     }
# }
def authorization_headers(
    origin_name: str,
    origin_signing_key: str,
    destination_name: str,
    request_method: str,
    uri: str,
    content: Optional[Union[str, Dict[str, Any]]] = None,
) -> str:
    # Extremely borrowed from Matrix spec docs, linked above. Spelunked a bit into
    # Synapse code to identify how the signing key is stored and decoded.
    request_json: Dict[str, Any] = {
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
            'X-Matrix origin="%s",key="%s",sig="%s",destination="%s"'
            % (
                origin_name,
                key,
                sig,
                destination_name,
            )
        )

    return authorization_header


def filter_events_based_on_type(
    events: List[EventBase], filter: str
) -> List[EventBase]:
    events_to_return = []
    for event in events:
        if event.event_type == filter:
            events_to_return.append(event)
    return events_to_return


def filter_state_events_based_on_membership(
    events: List[RoomMemberStateEvent], filter: str
) -> List[RoomMemberStateEvent]:
    events_to_return = []
    for event in events:
        if event.membership == filter:
            events_to_return.append(event)
    return events_to_return


def parse_list_response_into_list_of_event_bases(
    list_from_response: List[Dict[str, Any]]
) -> List[EventBase]:
    """
    Parse a list returned from a federation request into a list of EventBase type
    objects. Best used when we don't have any event id's to add to the new EventBase.

    Returns: list of processed Event type objects, in the order they were received

    """
    list_of_event_bases = []
    for event_dict in list_from_response:
        list_of_event_bases.append(
            determine_what_kind_of_event(event_id=None, data_to_use=event_dict)
        )

    return list_of_event_bases
