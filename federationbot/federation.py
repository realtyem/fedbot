from typing import Any, Collection, Dict, List, Optional, Sequence, Set, Tuple, Union
from asyncio import Queue
import asyncio
import json
import logging
import time

from aiohttp import (
    ClientResponse,
    ClientSession,
    ClientTimeout,
    TCPConnector,
    client_exceptions,
)
from backoff._typing import Details
from mautrix.types import EventID
from mautrix.util.logging import TraceLogger
from signedjson.key import decode_signing_key_base64, decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, sign_json, verify_signed_json
from yarl import URL
import backoff

from federationbot import ReactionTaskController
from federationbot.cache import LRUCache
from federationbot.delegation import DelegationHandler
from federationbot.errors import (
    FedBotException,
    PluginTimeout,
    ServerSSLException,
    ServerUnreachable,
)
from federationbot.events import (
    Event,
    EventBase,
    EventError,
    RoomMemberStateEvent,
    determine_what_kind_of_event,
    redact_event,
)
from federationbot.responses import (
    MakeJoinResponse,
    MatrixError,
    MatrixFederationResponse,
    MatrixResponse,
)
from federationbot.server_result import DiagnosticInfo, ResponseStatusType, ServerResult
from federationbot.types import (
    KeyContainer,
    KeyID,
    ServerVerifyKeys,
    SignatureVerifyResult,
)
from federationbot.utils import full_dict_copy, get_domain_from_id

backoff_logger = logging.getLogger("backoff")
SOCKET_TIMEOUT_SECONDS = 2.0


def backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    host = details.get("kwargs", {}).get("destination_server_name", "not found")
    backoff_logger.info(
        "Backing off {wait:0.2f} seconds after {tries} tries on "
        "host {host}".format(wait=wait, tries=tries, host=host)
    )


class FederationHandler:
    def __init__(
        self,
        logger: TraceLogger,
        bot_mxid: str,
        server_signing_keys: Dict[str, str],
        task_controller: ReactionTaskController,
    ):
        # TODO: Make a custom Resolver to handle server discovery
        self.http_client = ClientSession(
            connector=TCPConnector(keepalive_timeout=60, limit=1000, limit_per_host=3)
        )
        self.logger = logger
        self.hosting_server = get_domain_from_id(bot_mxid)
        self.bot_mxid = bot_mxid
        self.server_signing_keys = server_signing_keys
        self.json_decoder = json.JSONDecoder()
        self.delegation_handler = DelegationHandler(self.logger)
        self.task_controller = task_controller
        # Map the key to (server_name, event_id) -> Event
        self._events_cache: LRUCache[Tuple[str, str], EventBase] = LRUCache()
        # Map this cache to server_name -> ServerResult
        self._server_discovery_cache: LRUCache[str, ServerResult] = LRUCache(
            expire_after_seconds=60 * 30
        )
        self._server_keys_cache: LRUCache[str, ServerVerifyKeys] = LRUCache()
        self.room_version_cache: LRUCache[str, int] = LRUCache(
            expire_after_seconds=float(60 * 60 * 6),
            cleanup_task_sleep_time_seconds=float(60 * 60),
        )

    async def stop(self) -> None:
        # For stopping the cleanup task on these caches
        await self._events_cache.stop()
        await self._server_discovery_cache.stop()
        await self._server_keys_cache.stop()
        await self.room_version_cache.stop()
        await self.http_client.close()

    @backoff.on_predicate(
        backoff.runtime,
        predicate=lambda r: r.status == 429,
        value=lambda r: int(r.headers.get("Retry-After")),
    )
    @backoff.on_exception(
        backoff.expo,
        PluginTimeout,
        max_tries=3,
        backoff_log_level=logging.INFO,
        giveup_log_level=logging.INFO,
        on_backoff=backoff_logging_handler,
        logger=None,
        max_value=1.0,
        base=1.25,
    )
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
                raise ServerUnreachable(
                    f"{server_result.unhealthy}",
                    "Server was previously unreachable",
                )
            destination_port = int(server_result.port)
            resolved_destination_server = server_result.get_host()
            server_hostname_sni = (
                server_result.sni_server_name if server_result.use_sni else None
            )
            request_headers = {"Host": server_result.host_header}

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

        client_timeouts = ClientTimeout(
            # Don't limit the total connection time, as incremental reads are handled distinctly by sock_read
            total=None,
            # connect should be None, as sock_connect is behavior intended
            connect=None,
            # This is the most useful for detecting bad servers
            sock_connect=SOCKET_TIMEOUT_SECONDS,
            # This is the one that may have the longest time, as we wait for a server to send a response
            sock_read=timeout_seconds,
            # defaults to 5, for roundups on timeouts
            # ceil_threshold=5.0,
        )
        try:
            response = await self.http_client.request(
                method=method,
                url=url_object,
                headers=request_headers,
                timeout=client_timeouts,
                server_hostname=server_hostname_sni,
                json=content,
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
            if getattr(e, "os_error"):
                # This gets type ignored, as it is defined but for some reason mypy can't figure that out
                raise FedBotException(
                    e.__class__.__name__, e.os_error.strerror  # type: ignore[attr-defined]
                ) from e

            raise FedBotException(e.__class__.__name__, e.strerror) from e

        except (
            client_exceptions.ClientHttpProxyError,  # e.message
            client_exceptions.ClientResponseError,  # e.message, base class, possibly e.status too
            client_exceptions.ServerDisconnectedError,  # e.message
        ) as e:
            raise FedBotException(e.__class__.__name__, str(e.message)) from e

        # Save these other exceptions, as they might get revisited
        # except client_exceptions.ConnectionTimeoutError as e:
        # except client_exceptions.SocketTimeoutError as e:
        # except client_exceptions.WSServerHandshakeError:

        # Pretty sure will never hit this one either, as it's not enforced here
        # except client_exceptions.ContentTypeError:

        except client_exceptions.ServerTimeoutError as e:
            raise PluginTimeout(
                e.__class__.__name__,
                f"{e.__class__.__name__} after {SOCKET_TIMEOUT_SECONDS} seconds",
            ) from e
        except asyncio.TimeoutError as e:
            self.logger.warning(
                f"raising {e.__class__.__name__}: {destination_server_name}: {e}"
            )
            raise PluginTimeout(
                e.__class__.__name__,
                f"{e.__class__.__name__} after {timeout_seconds} seconds",
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
            self.logger.info(
                f"federation_request: General Exception: for {destination_server_name}"
                f":\n {e}"
            )
            raise FedBotException(e.__class__.__name__, str(e)) from e

        return response

    async def federation_request(
        self,
        destination_server_name: str,
        path: str,
        query_args: Optional[Sequence[Tuple[str, Any]]] = None,
        method: str = "GET",
        force_rediscover: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
        origin_server: Optional[str] = None,
        content: Optional[Dict[str, Any]] = None,
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
            method: The method to use for the request: GET, PUT, etc
            force_rediscover: in case we need to bypass the cache to redo server
                discovery
            diagnostics: Collect diagnostic data. Errors are always collected
            timeout_seconds: Float of how many seconds before timeout
            origin_server: The server to send the request as, signing keys will be
                required to be setup in the config files for authed requests
            content: for non-GET requests, the Dict that will be transformed into json
                to send
        Returns:
            MatrixResponse
        """
        result_dict: Optional[Dict[str, Any]] = None
        diag_info = DiagnosticInfo(diagnostics)
        server_result = self._server_discovery_cache.get(destination_server_name)
        errcode: Optional[str] = None
        error: Optional[str] = None
        now = time.time()

        # Either no ServerResult, or
        # forcing rediscovery, OR
        # had a ServerResult, but it was unhealthy and requested retry time has passed
        # then try and reload the ServerResult
        if (
            not server_result
            or force_rediscover
            or (server_result.unhealthy and server_result.retry_time_s < now)
        ):
            # If this server was attempted at some point and failed, there is no
            # point trying again until the cache entry is replaced.

            server_result = await self.delegation_handler.handle_delegation(
                destination_server_name,
                self._federation_request,
                diag_info=diag_info,
            )

        if server_result:
            # These only get filled in when diagnostics is True
            # This will add the word "Checking: " to the front of "Connectivity"
            diag_info.mark_step_num("Connectivity")
            diag_info.add(f"Making request to {server_result.get_host()}{path}")

        try:
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
        except FedBotException as e:
            # All the inner exceptions that can be raised are given a code of 0, representing an outside error
            code = 0
            error_reason = str(e.summary_exception)
            diag_info.error(str(e.long_exception))
            # Since there was an exception, cache the result unless it was a timeout error, as that shouldn't count
            server_result.unhealthy = error_reason

            if not isinstance(e, PluginTimeout):
                # Most all errors will be cached for 5 minutes
                server_result.retry_time_s = now + 5 * 60
            else:
                # Timeout errors get cached for 30 seconds
                server_result.retry_time_s = now + 30

            self._server_discovery_cache.set(server_result.host, server_result)

            return MatrixError(
                http_code=code,
                errcode=str(code),
                reason=error_reason,
                diag_info=diag_info if diagnostics else None,
                json_response={},
            )

        # The server was responsive, but may not have returned something useful
        async with response:
            code = response.status
            reason = response.reason or "No Reason/status returned"
            headers = response.headers
            self._server_discovery_cache.set(server_result.host, server_result)

            if 200 <= code < 599:
                result = await response.text()
            else:
                result = None

            diag_info.add(f"Request status: code:{code}, reason: {reason}")

        if result:
            try:
                result_dict = self.json_decoder.decode(result)

            except json.decoder.JSONDecodeError:
                diag_info.error("JSONDecodeError, work-around failed")
                diag_info.add("No usable data in response")
                # error_reason = "No/bad JSON returned"

            else:
                # if there was a matrix related error, pick the bits out of the json
                if result_dict:
                    error = result_dict.get("error", error)
                    errcode = result_dict.get("errcode", errcode)

        if diagnostics:
            tls_handled_by = headers.get("server", None)
            diag_info.tls_handled_by = tls_handled_by

        # The request is complete.
        # Caddy is notorious for returning a 200 as a default for non-existent endpoints. This is a problem, and
        # I believe it is against the Spec. When this happens, there should be no JSON to decode so result_dict
        # should still be None. Lump that in with other errors to return
        if code != 200 or (code == 200 and result_dict is None):
            diag_info.error(f"Request to {path} failed")
            if code == 200:
                # Going to log this for now, see how prevalent it is
                self.logger.warning(
                    f"fedreq: HIT possible Caddy condition: {destination_server_name}"
                )

            return MatrixError(
                http_code=code,
                reason=reason,
                diag_info=diag_info if diagnostics else None,
                json_response=result_dict or {},
                errcode=errcode,
                error=error,
            )

        # Don't need a success diagnostic message here, the one above works fine
        return MatrixFederationResponse(
            http_code=code,
            reason=reason,
            diag_info=diag_info if diagnostics else None,
            json_response=result_dict or {},
            errcode=errcode,
            error=error,
        )

    async def get_server_version(
        self,
        server_name: str,
        force_rediscover: bool = False,
        diagnostics: bool = False,
        timeout_seconds: float = 10.0,
    ) -> MatrixResponse:
        response = await self.federation_request(
            destination_server_name=server_name,
            path="/_matrix/federation/v1/version",
            method="GET",
            force_rediscover=force_rediscover,
            diagnostics=diagnostics,
            timeout_seconds=timeout_seconds,
        )

        if diagnostics and response.diag_info is not None:
            # Update the diagnostics info, this is the only request can do this on and is only for the delegation test
            if response.http_code != 200:
                response.diag_info.connection_test_status = ResponseStatusType.ERROR
            else:
                response.diag_info.connection_test_status = ResponseStatusType.OK

        return response

    async def _get_server_keys(
        self, server_name: str, timeout: float = 10.0
    ) -> MatrixResponse:
        response = await self.federation_request(
            destination_server_name=server_name,
            path="/_matrix/key/v2/server",
            method="GET",
            timeout_seconds=timeout,
        )

        return response

    async def get_server_keys(
        self, server_name: str, timeout: float = 10.0
    ) -> ServerVerifyKeys:
        response = await self._get_server_keys(server_name=server_name, timeout=timeout)

        if response.http_code != 200:
            self.logger.warning(f"get_server_keys: {server_name}: got {response}")

        json_response = response.json_response
        return ServerVerifyKeys(json_response)

    async def _notary_keys_request(
        self,
        fetch_server_name: str,
        from_server_name: str,
        minimum_valid_until_ts: int,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        response = await self.federation_request(
            destination_server_name=from_server_name,
            path=f"/_matrix/key/v2/query/{fetch_server_name}",
            query_args=[("minimum_valid_until_ts", minimum_valid_until_ts)],
            method="GET",
            timeout_seconds=timeout,
        )

        return response

    async def get_server_keys_from_notary(
        self, fetch_server_name: str, from_server_name: str, timeout: float = 10.0
    ) -> ServerVerifyKeys:
        minimum_valid_until_ts = int(time.time() * 1000) + (
            30 * 60 * 1000
        )  # Add 30 minutes

        response = await self._notary_keys_request(
            fetch_server_name=fetch_server_name,
            from_server_name=from_server_name,
            minimum_valid_until_ts=minimum_valid_until_ts,
            timeout=timeout,
        )
        if response.http_code != 200:
            self.logger.warning(
                f"get_server_keys_from_notary: {fetch_server_name}: got {response}"
            )

        server_verify_keys = ServerVerifyKeys({})

        server_verify_keys.update_key_data_from_list(response.json_response)

        return server_verify_keys

    async def get_server_key(
        self, for_server_name: str, key_id_needed: str, timeout: float = 10.0
    ) -> Dict[KeyID, KeyContainer]:

        key_id_formatted = KeyID(key_id_needed)
        cached_server_keys = self._server_keys_cache.get(for_server_name)
        if cached_server_keys is not None:
            if key_id_formatted in cached_server_keys.verify_keys:
                return cached_server_keys.verify_keys

        server_verify_keys = await self.get_server_keys(for_server_name, timeout)

        if key_id_formatted not in server_verify_keys.verify_keys:
            server_verify_keys = await self.get_server_keys_from_notary(
                for_server_name, self.hosting_server, timeout
            )

        # TODO: verify can remove this, as it can never be None but can be empty
        # if server_verify_keys is None:
        #     return {}

        # At this point we know
        # 1. Wasn't in the cache before(or at least this one key id wasn't)
        # 2. We have some kind of result
        verify_keys = server_verify_keys.verify_keys

        self._server_keys_cache.set(for_server_name, server_verify_keys)

        return verify_keys

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
        inject_new_data: Optional[Dict[str, Any]] = None,
        keys_to_pop: Optional[str] = None,
    ) -> Dict[str, EventBase]:
        """
        Retrieves a single Event from a server. Since the event id will be known, it can
         be included in the retrieved Event.
        Args:
            origin_server: The server placing the request
            destination_server: The server receiving the request
            event_id: The opaque string of the id given to the Event
            timeout:
            inject_new_data: Allow for injecting data into the structure for testing verification later
            keys_to_pop: Allow for removing data by key(s) from the structure for testing verification

        Returns: A tuple containing the FederationResponse received and the Event
            contained in a List

        """

        new_event_base = self._events_cache.get((destination_server, event_id))
        if new_event_base and not inject_new_data and not keys_to_pop:
            # Only successful events are cached
            return {event_id: new_event_base}

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/event/{event_id}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"get_event_from_server: {destination_server}, {event_id}: {response}"
            )
            new_event_base = EventError(
                EventID(event_id),
                {"error": f"{response.reason}", "errcode": f"{response.http_code}"},
            )

            return {event_id: new_event_base}

        pdu_list: List[Dict[str, Any]] = response.json_response.get("pdus", [])
        split_keys: List[str] = []
        if keys_to_pop:
            if "," in keys_to_pop:
                split_keys = keys_to_pop.split(",")
            else:
                split_keys = [keys_to_pop]
        # self.logger.info(f"split_keys: {split_keys}")
        for data in pdu_list:
            if inject_new_data:
                data.update(inject_new_data)
            for key_to_lose in split_keys:
                key_to_lose = key_to_lose.strip()
                self.logger.info(f"keys being popped: {key_to_lose}")

                data.pop(key_to_lose, None)
            # This path is only taken on success, errors are sorted above
            new_event_base = determine_what_kind_of_event(EventID(event_id), None, data)
            if inject_new_data or keys_to_pop:
                self.logger.info(
                    f"Dump of new data:\n{json.dumps(new_event_base.raw_data, indent=4)}"
                )
            else:
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
                    event_base_dict = await self.get_event_from_server(
                        origin_server=origin_server,
                        destination_server=destination_server,
                        event_id=worker_event_id,
                        timeout=timeout,
                    )

                # TODO: may not need these any more
                except asyncio.TimeoutError:
                    error_event = EventError(
                        EventID(worker_event_id),
                        {"error": "Request Timed Out", "errcode": "Timeout err"},
                    )
                    event_to_event_base[worker_event_id] = error_event
                except FedBotException as e:
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
        for _ in range(min(len(events_list), 3)):
            task = asyncio.create_task(_get_event_worker(event_queue))
            tasks.append(task)

        await event_queue.join()

        # Cancel our worker tasks.
        for task in tasks:
            task.cancel()
        # Wait until all worker tasks are cancelled.
        await asyncio.gather(*tasks, return_exceptions=True)

        return event_to_event_base

    async def find_event_on_servers(
        self, origin_server: str, event_id: str, servers_to_check: Collection[str]
    ) -> Dict[str, EventBase]:
        host_to_event_status_map: Dict[str, EventBase] = {}

        host_queue: Queue[str] = Queue()
        for host in servers_to_check:
            host_queue.put_nowait(host)

        async def _event_finding_worker(queue: Queue[str]) -> Tuple[str, EventBase]:
            worker_host = await queue.get()
            returned_events = await self.get_event_from_server(
                origin_server=origin_server,
                destination_server=worker_host,
                event_id=event_id,
            )
            inner_returned_event = returned_events.get(event_id)
            assert inner_returned_event is not None
            queue.task_done()
            return worker_host, inner_returned_event

        # Create a collection of Task's, to run the coroutine in
        reference_task_key = self.task_controller.setup_task_set()

        # These are one-off tasks, not workers. Create as many as we have servers to check
        self.task_controller.add_tasks(
            reference_task_key,
            _event_finding_worker,
            host_queue,
            limit=len(servers_to_check),
        )

        results = await self.task_controller.get_task_results(reference_task_key)

        for host, response in results:
            host_to_event_status_map[host] = response

        # Make sure to cancel all tasks
        await self.task_controller.cancel(reference_task_key)

        return host_to_event_status_map

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

        if response.http_code != 200:
            self.logger.warning(
                f"get_state_ids_from_server: {destination_server}: got {response}"
            )

        pdu_list = response.json_response.get("pdu_ids", [])
        auth_chain_list = response.json_response.get("auth_chain_ids", [])

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

        if response.http_code != 200:
            self.logger.warning(
                f"get_state_from_server: {destination_server}: got {response}"
            )

        pdus_list = response.json_response.get("pdus", [])
        auth_chain_list = response.json_response.get("auth_chain", [])

        return pdus_list, auth_chain_list

    async def get_event_auth_for_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        event_id: str,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/event_auth/{room_id}/{event_id}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"get_event_auth_for_event_from_server: {destination_server}: got {response}"
            )

        return response

    async def get_timestamp_to_event_from_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        utc_time_at_ms: int,
        timeout: float = 10.0,
    ) -> MatrixResponse:
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

        if response.http_code != 200:
            self.logger.warning(
                f"get_timestamp_to_event_from_server: {destination_server}: got {response}"
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
    ) -> MatrixResponse:

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/backfill/{room_id}",
            query_args=[("v", event_id), ("limit", limit)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"get_backfill_from_server: {destination_server}: got {response}"
            )

        return response

    async def get_user_devices_from_server(
        self,
        origin_server: str,
        destination_server: str,
        user_mxid: str,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        # url = URL(
        #     f"https://{destination_server}/_matrix/federation/v1/user/devices/{mxid}"
        # )

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/user/devices/{user_mxid}",
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"get_user_devices_from_server: {destination_server}: got {response}"
            )

        return response

    async def get_room_alias_from_server(
        self,
        origin_server: str,
        room_alias: str,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        try:
            _, destination_server = room_alias.split(":", maxsplit=1)
        except ValueError:
            self.logger.warning(
                f"get_room_alias_from_server: {room_alias} had malformed destination server"
            )
            return MatrixError(
                http_code=0,
                reason="Malformed Room Alias: missing a domain",
                json_response={},
            )
        response = await self.federation_request(
            destination_server_name=destination_server,
            path="/_matrix/federation/v1/query/directory",
            query_args=[("room_alias", room_alias)],
            origin_server=origin_server,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"get_room_alias_from_server: {destination_server}: got {response.http_code}: {response.reason}"
            )

        return response

    async def _send_transaction_to_server(
        self,
        origin_server: str,
        destination_server: str,
        pdus_to_send: Sequence[Dict[str, Any]],
        timeout: float = 10.0,
    ) -> MatrixResponse:
        formatted_data: Dict[str, Any] = {}
        now = int(time.time() * 1000)
        formatted_data["origin"] = origin_server
        formatted_data["origin_server_ts"] = now
        formatted_data["pdus"] = []
        for pdu in pdus_to_send:
            formatted_data["pdus"].append(pdu)

        # self.logger.info(
        #     f"outgoing transaction:\n{json.dumps(formatted_data, indent=4)}"
        # )

        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/send/{now}",
            method="PUT",
            content=formatted_data,
            timeout_seconds=timeout,
            origin_server=origin_server,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"_send_transaction_to_server: {destination_server}: got {response}"
            )

        return response

    async def send_events_to_server(
        self,
        origin_server: str,
        destination_server: str,
        event_data: Sequence[Dict[str, Any]],
        timeout: float = 10.0,
    ) -> MatrixResponse:
        response = await self._send_transaction_to_server(
            origin_server=origin_server,
            destination_server=destination_server,
            pdus_to_send=event_data,
            timeout=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"_send_events_to_server: {destination_server}: got {response}"
            )

        return response

    async def _get_public_rooms_from_server(
        self,
        origin_server: Optional[str],
        destination_server: Optional[str],
        include_all_networks: bool = False,
        limit: int = 10,
        since: Optional[str] = None,
        third_party_instance_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        if not origin_server:
            origin_server = self.hosting_server
        if not destination_server:
            destination_server = origin_server

        query_args = [
            ("include_all_networks", str(include_all_networks).lower()),
            ("limit", limit),
        ]

        if since:
            query_args.append(("since", since))
        if third_party_instance_id:
            query_args.append(("third_party_instance_id", third_party_instance_id))

        response = await self.federation_request(
            origin_server=origin_server,
            path="/_matrix/federation/v1/publicRooms",
            destination_server_name=destination_server,
            query_args=query_args,
            timeout_seconds=timeout,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"_send_transaction_to_server: {destination_server}: got {response}"
            )

        return response

    async def discover_room_version(
        self,
        origin_server: Optional[str],
        destination_server: Optional[str],
        room_id: str,
        timeout: float = 10.0,
    ) -> int:
        room_version = self.room_version_cache.get(room_id)
        if room_version:
            return room_version

        if not origin_server:
            origin_server = self.hosting_server
        if not destination_server:
            destination_server = origin_server

        try:
            response = await self.make_join_to_server(
                origin_server=origin_server,
                destination_server=destination_server,
                room_id=room_id,
                user_id=self.bot_mxid,
                timeout=timeout,
            )
        except MatrixError:
            # TODO: Could do something smarter here, like check state
            room_version = 0

        else:
            room_version = response.room_version

        if room_version > 0:
            self.room_version_cache.set(room_id, room_version)

        return room_version

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
        if isinstance(ts_response, MatrixFederationResponse):
            event_id = ts_response.json_response.get("event_id")

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

    async def _make_join_to_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        user_id: str,
        timeout: float = 10.0,
    ) -> MatrixResponse:
        query_list = []
        for i in range(0, 11):
            query_list.extend([("ver", f"{i + 1}")])
        # self.logger.info(f"make_join: dest: {destination_server}, {query_list}")
        response = await self.federation_request(
            destination_server_name=destination_server,
            path=f"/_matrix/federation/v1/make_join/{room_id}/{user_id}",
            query_args=query_list,
            # query_args={
            #     "ver": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"]
            # },
            timeout_seconds=timeout,
            origin_server=origin_server,
        )

        if response.http_code != 200:
            self.logger.warning(
                f"_make_join_to_server: {destination_server}: got {response}"
            )

        return response

    async def make_join_to_server(
        self,
        origin_server: str,
        destination_server: str,
        room_id: str,
        user_id: str,
        timeout: float = 10.0,
    ) -> MakeJoinResponse:
        """

        Args:
            origin_server:
            destination_server:
            room_id:
            user_id:
            timeout:

        Returns: A MakeJoinResponse
        Raises: MatrixError with the data of why

        """
        response = await self._make_join_to_server(
            origin_server=origin_server,
            destination_server=destination_server,
            room_id=room_id,
            user_id=user_id,
            timeout=timeout,
        )
        if response.http_code != 200:
            self.logger.warning(f"make_join: dest: {destination_server}: {response}")
            assert isinstance(response, MatrixError)
            raise response

        room_version: int = int(response.json_response.get("room_version", 0))
        assert room_version > 0
        prev_events = response.json_response.get("event", {}).get("prev_events", [])
        auth_events = response.json_response.get("event", {}).get("auth_events", [])
        return MakeJoinResponse(
            room_version=room_version, prev_events=prev_events, auth_events=auth_events
        )


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
        authorization_header = f'X-Matrix origin="{origin_name}",key="{key}",sig="{sig}",destination="{destination_name}"'

    return authorization_header


def filter_events_based_on_type(
    events: List[EventBase], filter_by: str
) -> List[EventBase]:
    events_to_return = []
    for event in events:
        if event.event_type == filter_by:
            events_to_return.append(event)
    return events_to_return


def filter_state_events_based_on_membership(
    events: List[RoomMemberStateEvent], filter_by: str
) -> List[RoomMemberStateEvent]:
    events_to_return = []
    for event in events:
        if event.membership == filter_by:
            events_to_return.append(event)
    return events_to_return


def parse_list_response_into_list_of_event_bases(
    list_from_response: List[Dict[str, Any]], room_version: Optional[int] = None
) -> List[EventBase]:
    """
    Parse a list returned from a federation request into a list of EventBase type
    objects. Best used when we don't have any event id's to add to the new EventBase.

    Returns: list of processed Event type objects, in the order they were received

    """
    list_of_event_bases = []
    for event_dict in list_from_response:
        list_of_event_bases.append(
            determine_what_kind_of_event(
                event_id=None, room_version=room_version, data_to_use=event_dict
            )
        )

    return list_of_event_bases
