from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union
from asyncio import Queue
import asyncio
import json
import ssl
import time

from aiohttp import ClientSession, client_exceptions
from mautrix.types import EventID
from mautrix.util.logging import TraceLogger
from signedjson.key import decode_signing_key_base64
from signedjson.sign import sign_json
from yarl import URL

from federationbot.cache import LRUCache
from federationbot.delegation import (
    DelegationHandler,
    check_and_maybe_split_server_name,
)
from federationbot.events import (
    CreateRoomStateEvent,
    EventBase,
    EventError,
    RoomMemberStateEvent,
    determine_what_kind_of_event,
)
from federationbot.responses import (
    FederationBaseResponse,
    FederationErrorResponse,
    FederationServerKeyResponse,
    FederationVersionResponse,
)
from federationbot.server_result import (
    DiagnosticInfo,
    ResponseStatusType,
    ServerResult,
    ServerResultError,
)


class FederationHandler:
    def __init__(
        self,
        http_client: ClientSession,
        logger: TraceLogger,
        server_signing_keys: Dict[str, str],
    ):
        self.http_client = http_client
        self.logger = logger
        self.server_signing_keys = server_signing_keys
        self.json_decoder = json.JSONDecoder()
        self.delegation_handler = DelegationHandler(self.logger)
        # Map the key to (server_name, event_id) -> Event
        self._events_cache: LRUCache[Tuple[str, str], EventBase] = LRUCache()

    async def federation_request(
        self,
        destination_server: str,
        path: str,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        server_result: Optional[ServerResult] = None,
        delegation_check: bool = True,
        force_recheck: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
        auth_request_for: Optional[str] = None,
        content: Optional[Dict[str, Any]] = None,
    ) -> FederationBaseResponse:
        """
        Retrieve json response from over federation.

        Args:
            destination_server: the server name being sent to, delegation is handled within
            path: The path component of the outgoing url
            query_string: the query component to send
            query_args:
            method: The method to use for the request: GET, PUT, etc
            server_result: a ServerResult object to send against, bypassing delegation
            delegation_check: if delegation checking should be skipped
            force_recheck: pass this into delegation handling to force retesting
            diagnostics: Collect diagnostic data. Errors are always collected
            timeout_seconds: Float of how many seconds before timeout
            auth_request_for: The server to send the request as, signing keys will be
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

        # If the server is delegated in some way, this will take care of making sure we
        # get a usable host:port
        if delegation_check and not server_result:
            server_result = await self.delegation_handler.maybe_handle_delegation(
                destination_server,
                self.federation_request,
                force_reload=force_recheck,
                diag_info=diag_info,
            )

        try:
            if server_result:
                # These only get filled in when diagnostics is True
                # This will add the word "Checking: " to the front of "Connectivity"
                diag_info.mark_step_num("Connectivity")
                # Use get_host() to get the actual server host instead of the delegated
                diag_info.add(f"Making request to {server_result.get_host()}{path}")

                # Use the URL class to build the parts, as otherwise the query
                # parameters don't encode right and fail the JSON signing process verify
                # on the other end. I suspect it has to do with sigil encoding.
                url_object = URL.build(
                    scheme="https",
                    host=server_result.get_host(),
                    port=int(server_result.port),
                    path=path,
                    query=query_args,
                    encoded=False,
                )

                request_headers = {"Host": server_result.host_header}

                signed_content = None
                if auth_request_for:
                    (
                        request_headers["Authorization"],
                        signed_content,
                    ) = authorization_headers(
                        origin_name=auth_request_for,
                        origin_signing_key=self.server_signing_keys[auth_request_for],
                        destination_name=destination_server,
                        request_method=method,
                        uri=str(url_object.relative()),
                        content=content,
                    )
                response = await self.http_client.request(
                    method=method,
                    url=url_object,
                    headers=request_headers,
                    timeout=timeout_seconds,
                    server_hostname=server_result.sni_server_name
                    if server_result.use_sni
                    else None,
                    json=signed_content,
                )

            else:
                diag_info.add(f"Making request to {destination_server}{path}")

                # Just a basic web GET request, but allowing use of our infrastructure
                # Realistically, this will only be used by the well known request.
                response = await self.http_client.request(
                    method=method,
                    url=f"https://{destination_server}{path}",
                    timeout=timeout_seconds,
                )

        except ConnectionRefusedError as e:
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

        except client_exceptions.ServerDisconnectedError as e:
            diag_info.error(f"Server Disconnect Error")
            error_reason = "Server Disconnect Error"

        # except client_exceptions.ConnectionTimeoutError as e:

        # except client_exceptions.SocketTimeoutError as e:

        except client_exceptions.ServerTimeoutError as e:
            diag_info.error(f"Server Timeout Error")
            error_reason = "Server Timeout Error"

        except client_exceptions.ServerFingerprintMismatch as e:
            diag_info.error(f"Server Fingerprint Mismatch Error")
            error_reason = "Server Fingerprint Mismatch Error"

        except client_exceptions.ServerConnectionError as e:
            diag_info.error(f"Server Connection Error")
            error_reason = "ServerConnectionError"

        except client_exceptions.ClientSSLError as e:
            diag_info.error(f"ClientSSLError: {e.strerror}")
            error_reason = f"Client SSL Error: {e.strerror}"
            # This is one of the errors I found while probing for SNI TLS
            code = -1

        except client_exceptions.ClientProxyConnectionError as e:
            diag_info.error(f"Client Proxy Connection Error")
            error_reason = "Client Proxy Connection Error"

        except client_exceptions.ClientConnectorError as e:
            # code = 0
            diag_info.error(f"ClientConnectorError: {e.strerror}")
            error_reason = f"Client Connector Error: {e.strerror}"

        except client_exceptions.ClientHttpProxyError as e:
            diag_info.error(f"Client HTTP Proxy Error")
            error_reason = "Client HTTP Proxy Error"

        except client_exceptions.WSServerHandshakeError as e:
            # Not sure this one will ever be used...
            pass
        except client_exceptions.ContentTypeError as e:
            # Pretty sure will never hit this one either, as it's not enforced here
            diag_info.error(f"Content Type Error")
            error_reason = "Content Type Error"
        except client_exceptions.ClientResponseError as e:
            diag_info.error(f"Client Response Error")
            error_reason = "Client Response Error"

        except client_exceptions.ClientPayloadError as e:
            diag_info.error(f"Client Payload Error")
            error_reason = "Client Payload Error"

        except client_exceptions.InvalidURL as e:
            diag_info.error(f"InvalidURL Error")
            error_reason = "InvalidURL Error"

        except client_exceptions.ClientOSError as e:
            diag_info.error(f"Client OS Error")
            error_reason = "Client OS Error"

        except client_exceptions.ClientConnectionError as e:
            diag_info.error(f"Client Connection Error")
            error_reason = "Client Connection Error"

        except client_exceptions.ClientError as e:
            diag_info.error(f"Client Error")
            error_reason = "Client Error"

        except asyncio.TimeoutError:
            diag_info.error(
                "TimeoutError, this server probably doesn't exist(or is taking to long)"
            )
            error_reason = "Timed out. Is this server online?"
        except Exception as e:
            self.logger.info(
                f"federation_request: General Exception: for {destination_server}:\n {e}"
            )
            diag_info.error(f"General Exception: {e}")
            error_reason = "General Exception"

        # response = await self.http_client.get(f"https://{server}{query_string}")
        else:
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
            if not server_result:
                host, port = check_and_maybe_split_server_name(destination_server)
                server_result = ServerResult(
                    host=host, port=port if port else "", diag_info=diag_info
                )
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
        force_recheck: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
    ) -> FederationBaseResponse:
        response = await self.federation_request(
            destination_server=server_name,
            path="/_matrix/federation/v1/version",
            method="GET",
            force_recheck=force_recheck,
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

    async def get_server_keys(
        self, server_name: str, timeout: float = 10.0
    ) -> Union[FederationServerKeyResponse, FederationErrorResponse]:
        response = await self.federation_request(
            destination_server=server_name,
            path="/_matrix/key/v2/server",
            method="GET",
            timeout_seconds=timeout,
        )
        if response.status_code == 404:
            response.server_result.diag_info.connection_test_status = (
                ResponseStatusType.NONE
            )
        elif response.status_code != 200:
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
            return FederationServerKeyResponse.from_response(response)

    async def get_server_keys_from_notary(
        self, fetch_server_name: str, from_server_name: str, timeout: float = 10.0
    ) -> FederationBaseResponse:
        response = await self.federation_request(
            destination_server=from_server_name,
            path=f"/_matrix/key/v2/query/{fetch_server_name}",
            method="GET",
            timeout_seconds=timeout,
        )
        return response

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

        Returns: A tuple containing the FederationResponse received and the Event
            contained in a List

        """
        # The return, map of event_id -> EventBase
        event_id_to_event: Dict[str, EventBase] = {}

        new_event_base = self._events_cache.get((destination_server, event_id))
        if new_event_base:
            # Only successful events are cached
            return {event_id: new_event_base}

        response = await self.federation_request(
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/event/{event_id}",
            auth_request_for=origin_server,
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
        for i in range(min(len(events_list), 10)):
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
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/state_ids/{room_id}",
            query_args=[("event_id", event_id)],
            auth_request_for=origin_server,
            timeout_seconds=timeout,
        )

        pdu_list = response.response_dict.get("pdu_ids", [])
        auth_chain_list = response.response_dict.get("auth_chain_ids", [])

        return pdu_list, auth_chain_list

    async def get_event_auth_for_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        timeout: float = 10.0,
    ) -> FederationBaseResponse:
        response = await self.federation_request(
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/event_auth/{room_id}/{event_id}",
            auth_request_for=origin_server,
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
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/timestamp_to_event/{room_id}",
            query_args=[("dir", "b"), ("ts", utc_time_at_ms)],
            auth_request_for=origin_server,
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
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/backfill/{room_id}",
            query_args=[("v", event_id), ("limit", limit)],
            auth_request_for=origin_server,
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
            destination_server=destination_server,
            path=f"/_matrix/federation/v1/user/devices/{user_mxid}",
            auth_request_for=origin_server,
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
            destination_server=destination_server,
            path="/_matrix/federation/v1/query/directory",
            query_args=[("room_alias", room_alias)],
            auth_request_for=origin_server,
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
) -> Tuple[str, Optional[Dict[str, Any]]]:
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

    return authorization_header, signed_json.get("content", None)


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
