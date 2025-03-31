from typing import Any, Dict, List, Optional, Sequence, Tuple
from datetime import datetime
import hashlib
import json

from canonicaljson import encode_canonical_json
from mautrix.types import EventID
from unpaddedbase64 import decode_base64, encode_base64

from federationbot.types import KeyID, ServerName, Signature, Signatures, SignatureVerifyResult
from federationbot.utils import (
    DisplayLineColumnConfig,
    extract_max_key_len_from_dict,
    extract_max_value_len_from_dict,
    full_dict_copy,
)

EVENT_ID = "Event ID"
EVENT_TYPE = "Event Type"
EVENT_SENDER = "Sender"
EVENT_DEPTH = "Depth"


json_decoder = json.JSONDecoder()


class EventBase:
    """
    The lowest common denominator of an Event.

    The full JSON is converted to a Dict[str, Any] before being passed into this. All
    data is copied then placed into to other Dicts from which everything usable will be
    'pop()'ed off. Anything that was not parsed will remain in either 'content' or
    'unrecognized' for a fuller display as a form of fall-back behavior.

    Attributes:
        event_id: A string starting with '$'. This has changed over several spec changes
        content: The actual useful data of the Event.
        event_type: A string explaining what type of Event this is, such as m.room.member
        room_id: The opaque ID for the room
        sender: The mxid of the user that created this Event
        depth: A sequential number of how many events came before it plus this one
        auth_events: A List of Event IDs that allow this Event to exist
        prev_events: A List of Event IDs that preceded this one.
        raw_data: The full copy of all the data that makes this Event. to_json() uses
            this to display the information.
        unrecognized: Anything left over from the Event that didn't end up in 'content'
            will be here.
    """

    LINE_TEMPLATE = [""]
    event_id: EventID
    content: Dict[str, Any]
    event_type: str
    room_id: str
    sender: str
    # For giving out the raw JSON
    raw_data: Dict[str, Any]
    unrecognized: Dict[str, Any]
    # Unused in this class, but is used in subclasses
    depth: int = 0
    auth_events: List[str] = []
    prev_events: List[str] = []
    error: Optional[str] = None
    errcode: Optional[str] = None

    def __init__(self, event_id: EventID, data: Dict[str, Any]) -> None:
        # Make a copy of the data, as it will be mutated during the construction process
        # and just using a copy() or dict() only gives a shallow copy that reflect
        # changes made to it's iterables.
        self.raw_data = full_dict_copy(data)

        self.unrecognized = data
        self.event_id = event_id
        self.content = self.unrecognized.pop("content", {})
        self.event_type = self.unrecognized.pop("type", "Event Type Not Found")
        self.room_id = self.unrecognized.pop("room_id", "Room ID Not Found")
        self.sender = self.unrecognized.pop("sender", "Sender Not Found")
        # unsigned.age is a pointless thing, just remove it for now
        self.unrecognized.get("unsigned", {}).pop("age", None)

    def verify_content_hash(self) -> bool:
        # This should never be hit, but need to double check that. The Event class
        # should intercept it
        return False

    def generate_event_id(self, room_version: int) -> str:
        raw_data_copy = full_dict_copy(self.raw_data)
        generated_event_id = construct_event_id_from_event_v3(room_version, raw_data_copy)
        return generated_event_id

    def verify_event_id(self, room_version: int, event_id_to_test: str) -> bool:
        generated_event_id = self.generate_event_id(room_version)
        return generated_event_id == event_id_to_test

    def to_json(self, condensed: bool = False) -> str:
        indent = None if condensed else 4
        separators = (",", ":") if condensed else (", ", ": ")
        return json.dumps(self.raw_data, indent=indent, separators=separators)

    def to_template_line_summary(
        self,
        template_list: Sequence[Tuple[Sequence[str], DisplayLineColumnConfig]],
    ) -> str:
        """
        Take a template list and retrieve all the attributes then append them
        before sending them back

        The template list is a bunch of tuples containing the string name of the
        attribute to look for on the EventBase, and the DisplayLineColumnConfig
        to handle the render for the line
        """
        summary = ""
        for template_item in template_list:
            attributes, dc = template_item
            for each_attr in attributes:
                if getattr(self, each_attr, False):
                    summary += f"{dc.pad(getattr(self, each_attr))} "
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig("Depth"),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig("Event ID"),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig("Event Type"),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig("Sender"),
    ) -> str:
        """
        Construct a horizontal line summary containing:
            depth, Event ID, Event type, and sender

        Args:
            template_list: A template setup sequence comprised of:
                [[str], DisplayLineColumnConfig] repeated for each attribute to extract
            dc_depth: DisplayLineColumnConfig controlling the width of the Depth column
            dc_eid: DisplayLineColumnConfig controlling the width of the Event ID column
            dc_etype: DisplayLineColumnConfig controlling the width of the Event Type
                column
            dc_sender: DisplayLineColumnConfig controlling the width of the Sender
                column

        Returns: A full string constructed with the provided column widths, no new line
            at the end
        """
        # depth, event_id, event_type, sender
        summary = f"{dc_depth.pad(str(self.depth))} "
        summary += f"{dc_eid.pad(self.event_id)} "
        summary += f"{dc_etype.pad(self.event_type)} "
        # Unlike other places, we do pad the final space here, as there may be more
        # columns that can be added in subclasses.
        summary += f"{dc_sender.pad(self.sender)} "
        return summary

    def to_extras_summary(self) -> str:
        """
        Short single line summary used for displaying data about the Event. Generally
        used by the render for event_auth and backfill commands.
        Returns: string populated by subclasses

        """
        return ""

    def to_short_type_summary(self) -> str:
        return self.event_type

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        """
        Construct a vertical summary of most(all) the fields from the Event.

        Vertical displays look best when centered on a vertical delimiter, so a single
        DisplayLineColumnConfig is used to place that. Since this is the base class
        of all Events, only include the information that will definitely be present.
        It is the responsibility of subclasses to add to this.

        Note: Unlike to_summary_line(), this does NOT include the 'depth' field

        Args:
            dc: DisplayLineColumnConfig controlling the width of the left side of the
                vertical display
            room_version: The integer representing the room version. Used to verify the
                Event hashes

        Returns: A long string constructed with several new lines and ended with a new
            line.

        """
        event_id_header = "Event ID"
        type_header = "Type"
        room_id_header = "Room ID"
        sender_header = "Sender"
        summary = ""

        # Give the column width here as the largest of the key names, which is Event ID
        dc.maybe_update_column_width(len(event_id_header))
        verify_event_id = False
        if room_version and room_version > 0:
            verify_event_id = self.verify_event_id(room_version, self.event_id)

        is_event_id_ok = f"{'✅' if verify_event_id else '❌'}"
        summary += dc.render_pretty_line(event_id_header, f"{is_event_id_ok} {self.event_id}")
        summary += dc.render_pretty_line(type_header, self.event_type)
        summary += dc.render_pretty_line(room_id_header, self.room_id)
        summary += dc.render_pretty_line(sender_header, self.sender)
        return summary

    def to_pretty_summary_content(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig("Additional Content"),
    ) -> str:
        """
        Special rendering for the fallback of additional 'content' that was not parsed.

        Args:
            dc: A different DisplayLineColumnConfig that was used for the earlier
            rendering. There isn't really a requirement for special padding, but the
            helper classes are useful.

        Returns: A formatted string containing the pretty printed JSON of anything left
            in 'content', terminated by 2 new lines. If nothing remains, this will be an
            empty string.
        """
        summary = ""
        if self.content:
            summary += f"{dc.front_pad()}:\n"
            summary += f"{json.dumps(self.content, indent=4)}\n"
            summary += "\n"
        return summary

    def to_pretty_summary_unrecognized(
        self, dc: DisplayLineColumnConfig = DisplayLineColumnConfig("Unrecognized Data")
    ) -> str:
        """
        Special rendering for the fallback of additional 'unrecognized' that was not
        parsed.

        Args:
            dc: A different DisplayLineColumnConfig that was used for the earlier
            rendering. There isn't really a requirement for special padding, but the
            helper classes are useful.

        Returns: A formatted string containing the pretty printed JSON of anything left
            in 'unrecognized', terminated by 2 new lines. If nothing remains, this will
            be an empty string.
        """
        summary = ""
        # There may be occasion that 'unsigned' no longer contains any data. In that
        # case, remove the key itself so it doesn't display
        unsigned = self.unrecognized.get("unsigned", {})
        if not unsigned:
            self.unrecognized.pop("unsigned", None)

        if self.unrecognized:
            summary = f"{dc.front_pad()}:\n"
            summary += f"{json.dumps(self.unrecognized, indent=4)}\n"
            summary += "\n"

        return summary

    def to_pretty_summary_footer(
        self,
        event_data_map: Dict[str, str],
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
    ) -> str:
        """
        Display ancestor Events to this Event, such as auth_events and prev_events.

        In order to do this, we'll need a source of data about the ancestor events.

        Args:
            event_data_map: A mapping from EventID to the data to display, which is
                collated elsewhere. If nothing is available, just print an empty string
                to avoid breaking the world.
            dc: A different DisplayLineColumnConfig that was used for the earlier
                rendering. There isn't really a requirement for special padding, but the
                helper classes are useful.

        Returns: A formatted string containing data about the ancestor events terminated
            with a single new line. If there is no data, an empty string.
        """
        auth_header = "-- Auth Event"
        prev_header = "<- Prev Event"
        # For the moment, these are the same length so either will do for a length
        dc.maybe_update_column_width(len(auth_header))

        summary = ""
        for auth_event in self.auth_events:
            summary += f"{dc.front_pad(auth_header)}: {auth_event}: {event_data_map.get(auth_event, '')}\n"

        for prev_event in self.prev_events:
            summary += f"{dc.front_pad(prev_header)}: {prev_event}: {event_data_map.get(prev_event, '')}\n"

        return summary


class EventError(EventBase):
    def __init__(self, event_id: EventID, data: Dict[str, Any]) -> None:  # noqa W0231
        # super().__init__(event_id, data)
        self.error = data.get("error", "Unknown Error")
        self.errcode = data.get("errcode", "Unknown Error")

    def to_short_type_summary(self) -> str:
        return f"{self.errcode} {self.error}"

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig("Depth"),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig("Event ID"),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig("Event Type"),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig("Sender"),
    ) -> str:
        """
        Construct a horizontal line summary containing:
            depth, Event ID, Event type, and sender

        Args:
            template_list: A template setup sequence comprised of:
                [[str], DisplayLineColumnConfig] repeated for each attribute to extract
            dc_depth: DisplayLineColumnConfig controlling the width of the Depth column
            dc_eid: DisplayLineColumnConfig controlling the width of the Event ID column
            dc_etype: DisplayLineColumnConfig controlling the width of the Event Type
                column
            dc_sender: DisplayLineColumnConfig controlling the width of the Sender
                column

        Returns: A full string constructed with the provided column widths, no new line
            at the end
        """
        # depth, event_id, event_type, sender
        summary = f"{dc_depth.pad(str(self.depth))} "
        summary += f"{dc_eid.pad(self.event_id)} "
        summary += f"{dc_etype.pad(self.errcode)} "
        # Unlike other places, we do pad the final space here, as there may be more
        # columns that can be added in subclasses.
        summary += f"{self.error}"
        return summary


def represent_signature_verify_result_as_symbol(result: SignatureVerifyResult) -> str:
    if result == SignatureVerifyResult.SUCCESS:
        return "✅"  # Green check mark
    if result == SignatureVerifyResult.FAIL:
        return "❌"  # Red X

    # This represents SignatureVerifyResult.UNKNOWN, the only other option
    return "❓"  # Red Question Mark


class RelatesTo:
    """
    Helper class to assist in parsing m.relates_to

    Can be on either Encrypted or Unencrypted Events
    """

    # Rich Replies use the m.in_reply_to key with an event_id under that instead of the
    # top level event_id. Instead, we will make in_reply_to a str and place the event_id
    # attribute there.

    # m.replace - edited content
    #  will have an additional key at same level: m.new_content

    # m.annotations
    # part of reactions, should have a event type of m.reaction but according to the
    # spec can be any event type(even a state event)

    # m.thread
    # Appears to have no special fields of its own

    # m.reference
    # I could not find any pertinent reference to this other than the client key
    # verification request framework. I think this is safe to ignore.

    RELATES_TO_LIST = ["m.replace", "m.annotation", "m.thread"]
    # Will always have these
    event_id: Optional[EventID]
    # All types use rel_type except rich replies(which uses in_reply_to instead)
    rel_type: Optional[str]
    in_reply_to: Optional[str]

    # Part of replacements, this is the room content base class
    new_content: Optional["UnencryptedRoomContent"] = None

    # Part of annotations, usually a reaction emoji but can be text
    key: Optional[str] = None

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        # See if it's a rich reply first, then do the rest
        reply_to_event_id = raw_data.pop("m.in_reply_to", {})
        self.in_reply_to = reply_to_event_id.pop("event_id", None)
        # Ok, moving on
        self.rel_type = raw_data.pop("rel_type", None)
        if self.rel_type in self.RELATES_TO_LIST:
            # This is a relation we know what to do with
            new_content = raw_data.pop("m.new_content", {})
            self.new_content = determine_content_msgtype(new_content)
            self.key = raw_data.pop("key", None)

        self.event_id = raw_data.pop("event_id", None)

    def to_pretty_relations_summary(self, dc: DisplayLineColumnConfig) -> str:
        # This one is hard. There a lots of types of relations to deal with
        event_id_header = "Relates to Event ID"
        rel_type_header = "Relation Type"
        in_reply_to_header = "In Reply To"
        reaction_key_header = "Reaction Key"
        dc.maybe_update_column_width(len(event_id_header))
        summary = ""

        summary += dc.render_pretty_line(in_reply_to_header, self.in_reply_to)
        summary += dc.render_pretty_line(rel_type_header, self.rel_type)
        summary += dc.render_pretty_line(event_id_header, self.event_id)
        summary += dc.render_pretty_line(reaction_key_header, self.key)

        return summary


class Event(EventBase):
    """
    A standard Event in a room(not a piece of state) such as a message or a reaction
    """

    depth: int
    hashes: Dict[str, str]
    origin: str
    origin_server_ts: int
    signatures: Signatures
    relations: Optional[RelatesTo] = None
    signatures_verified: Dict[ServerName, SignatureVerifyResult]

    def __init__(self, event_id: EventID, data: Dict[str, Any]) -> None:
        super().__init__(event_id, data)
        # Recall that auth_events and prev_events are part of the base class
        self.auth_events = self.unrecognized.pop("auth_events", [])
        self.prev_events = self.unrecognized.pop("prev_events", [])

        self.depth = self.unrecognized.pop("depth", 0)
        self.hashes = self.unrecognized.pop("hashes", {})
        self.origin = self.unrecognized.pop("origin", "Origin Server Not Found")
        self.origin_server_ts = self.unrecognized.pop("origin_server_ts", 0)
        signatures = self.unrecognized.pop("signatures", {})
        if signatures:
            self.signatures = Signatures(signatures)
        relations = self.content.pop("m.relates_to", {})
        if relations:
            self.relations = RelatesTo(relations)
        self.signatures_verified = {}
        for server_name in self.signatures.servers:
            self.signatures_verified[server_name] = SignatureVerifyResult.UNKNOWN

    def verify_content_hash(self) -> bool:
        base_event = dict(self.raw_data)
        base_event.pop("unsigned", None)
        base_event.pop("signatures", None)
        hashes = base_event.pop("hashes", {})
        event_json_bytes = encode_canonical_json(base_event)
        hashed_event = hashlib.sha256(event_json_bytes).digest()
        existing_hash: Optional[str] = hashes.get("sha256", None)
        if existing_hash is not None:
            decoded_existing_hash = decode_base64(existing_hash)
            return hashed_event == decoded_existing_hash

        # For some reason the hash was missing on the event, auto fail that
        return False

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        """
        Adds to the superclass version of to_pretty_summary(). Specifically, adds:
            * Depth
            * Origin
            * Origin Server Timestamp
            * any Signatures key id's
            * any Hashes(and if it passes verification)

        Args:
            dc: The DisplayLineColumnConfig to control the vertical layout
            room_version: See base class

        Returns: A long string constructed with several new lines and ended with 2 new
            lines. Specifically using the 2 new lines allows for some separation between
            the bulk of generic Event data and the specialized data of more specific
            Event types.
        """
        # content will have an algorithm key if it's encrypted and a msgtype if it's not
        depth_header = "Depth"
        origin_header = "Origin"
        origin_ts_header = "Origin Server TS"
        sig_header = "Signatures"
        hashes_header = "Hashes"
        dc.maybe_update_column_width(len(origin_ts_header))

        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(depth_header, self.depth)
        summary += dc.render_pretty_line(origin_header, self.origin)

        # datetime.fromtimestamp() will give a 'datetime' object, it will render as a
        # date and a time string for this purpose.
        summary += dc.render_pretty_line(origin_ts_header, datetime.fromtimestamp(self.origin_server_ts / 1000))

        # There is no helper class specifically designed around signatures and hashes.
        # Custom code it for now. Should look like:
        #
        #    Signatures: example.com ed25519:k3y1D
        #
        for server, signature_container in self.signatures.servers.items():
            result_symbol = represent_signature_verify_result_as_symbol(self.signatures_verified[server])
            summary += f"{dc.front_pad(sig_header)}: {result_symbol} {server} "
            for key_id in signature_container.keyid:
                summary += f"{key_id}\n"

        # Hashes should look like:
        #
        #       Hashes: sha256: somethingorotherreallylonghashthatsnotlongeno
        #
        for hash_type, hash_value in self.hashes.items():
            pretty_hash_result = "✅" if self.verify_content_hash() else "❌"
            summary += f"{dc.front_pad(hashes_header)}: {pretty_hash_result} {hash_type}: {hash_value}\n"

        # The previous output does not add a new line(well, it does now) but create some
        # space anyway
        summary += "\n"
        if self.relations:
            summary += self.relations.to_pretty_relations_summary(dc)
            summary += "\n"
        return summary


class EncryptedRoomContent:
    """
    A m.room.encrypted Event's 'content'
    """

    # ciphertext is also in this, but only it's length
    algorithm: str
    device_id: str
    sender_key: str
    session_id: str
    ciphertext_size: int

    def __init__(
        self,
        algorithm: str,
        device_id: str,
        sender_key: str,
        session_id: str,
        ciphertext_size: int,
    ) -> None:
        self.algorithm = algorithm
        self.device_id = device_id
        self.sender_key = sender_key
        self.session_id = session_id
        self.ciphertext_size = ciphertext_size


class EncryptedRoomEvent(Event):
    encrypted_content: EncryptedRoomContent

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        algorithm = self.content.pop("algorithm", "")
        device_id = self.content.pop("device_id", "")
        sender_key = self.content.pop("sender_key", "")
        session_id = self.content.pop("session_id", "")
        # This gets removed from the content, but never shown as it's not usable
        ciphertext = self.content.pop("ciphertext", "")
        ciphertext_size = len(ciphertext)
        self.encrypted_content = EncryptedRoomContent(
            algorithm=algorithm,
            device_id=device_id,
            sender_key=sender_key,
            session_id=session_id,
            ciphertext_size=ciphertext_size,
        )

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        """
        Adds to the superclass version of to_pretty_summary(). Specifically, adds:
            * Algorithm
            * Session ID
            * Ciphertext length
        Don't bother with the deprecated keys used with Olm:
            * Device ID
            * Sender Key

        Args:
            dc: The DisplayLineColumnConfig to control the vertical layout
            room_version: If this is passed in, run content hash verification

        Returns: A long string constructed with several new lines and ended with a new
            line.
        """
        algo_header = "Algorithm"
        sess_id_header = "Session ID"
        ciphertext_size_header = "CipherText Size"
        dc.maybe_update_column_width(len(ciphertext_size_header))

        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(algo_header, self.encrypted_content.algorithm)
        summary += dc.render_pretty_line(sess_id_header, self.encrypted_content.session_id)
        summary += dc.render_pretty_line(ciphertext_size_header, self.encrypted_content.ciphertext_size)
        return summary


class Mentions:
    user_ids: Optional[List[str]] = None
    room: bool

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        self.room = raw_data.pop("room", False)
        self.user_ids = raw_data.pop("user_ids", None)

    def to_list(self) -> List[str]:
        summary_list = []
        if self.room:
            summary_list.extend(["@room"])
        if self.user_ids:
            summary_list.extend(self.user_ids)
        return summary_list


class UnencryptedRoomContent:
    """
    The bulk of what content is used by a room will be in one of the various subclasses.

    All data to be parsed will be placed into _raw_content, and then removed as it's
    parsed.
    """

    _raw_content: Dict[str, Any]
    msgtype: str
    body: str
    body_size: int
    mentions: Optional[Mentions] = None

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        self._raw_content = raw_data
        self.msgtype = self._raw_content.pop("msgtype", "Not Found")
        self.body = self._raw_content.pop("body", "")
        self.body_size = len(self.body)
        mentions = self._raw_content.pop("m.mentions", {})
        if mentions:
            self.mentions = Mentions(mentions)

    def to_pretty_content_summary(self, dc: DisplayLineColumnConfig) -> str:
        content_header = "Content Summary"
        msgtype_header = "Message Type"
        body_size_header = "Body Size"
        summary = ""
        dc.maybe_update_column_width(len(content_header))
        summary += f"{dc.front_pad(content_header)}\n"
        summary += dc.render_pretty_line(msgtype_header, self.msgtype)
        summary += dc.render_pretty_line(body_size_header, self.body_size)
        return summary

    def to_extras_summary_line(self) -> str:
        return ""


class MediaMetadata:
    h: Optional[int]
    w: Optional[int]
    mimetype: Optional[str]
    size: Optional[int]
    duration: Optional[int]  # in milliseconds

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        self.h = raw_data.pop("h", None)
        self.w = raw_data.pop("w", None)
        self.mimetype = raw_data.pop("mimetype", None)
        self.size = raw_data.pop("size", None)
        self.duration = raw_data.pop("duration", None)

    def to_pretty_summary_as_list(self) -> List[str]:
        summary_list = []
        if self.mimetype:
            summary_list.extend([f"Mimetype: {self.mimetype}"])
        if self.size:
            summary_list.extend([f"File Size: {self.size}"])
        if self.h and self.w:
            summary_list.extend([f"h: {self.h}  w: {self.w}"])
        if self.duration:
            summary_list.extend([f"Duration: {self.duration}"])
        return summary_list


class FileBasedContent(UnencryptedRoomContent):
    """
    External file based content. Covers msgtypes of:
        * m.file
        * m.image
        * m.audio
        * m.video

    From the server point of view, it will never see the EncryptedFile type fields, as
    they are in the decrypted Event that only a client can see. So, just don't bother
    """

    #         body(should be an 'alt text', but is seldom used yet)(superclass)
    MESSAGE_TYPES = ["m.file", "m.image", "m.audio", "m.video"]
    # Both of these are required if the file is unencrypted
    info: Optional[MediaMetadata]
    url: Optional[str]
    # Only on m.file msgtype
    filename: Optional[str]
    # Not all file types have thumbnails, so Optional
    thumbnail_info: Optional[MediaMetadata]
    thumbnail_url: Optional[str]

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        super().__init__(raw_data)
        self.url = self._raw_content.pop("url", None)
        self.filename = self._raw_content.pop("filename", None)
        info: Optional[Dict[str, Any]] = self._raw_content.pop("info", None)
        if info:
            self.info = MediaMetadata(info)
            thumbnail_info = info.pop("thumbnail_info", {})
            self.thumbnail_url = info.pop("thumbnail_url", None)
            if thumbnail_info:
                self.thumbnail_info = MediaMetadata(thumbnail_info)

    def to_pretty_content_summary(self, dc: DisplayLineColumnConfig) -> str:
        info_header = "Media Info"
        url_header = "URL"
        filename_header = "Filename"
        thumbnail_info_header = "Thumbnail Media Info"
        thumbnail_url_header = "Thumbnail URL"
        dc.maybe_update_column_width(len(thumbnail_info_header))
        summary = super().to_pretty_content_summary(dc)
        if self.filename:
            summary += dc.render_pretty_line(filename_header, self.filename)
        if self.url and self.info:
            # This file was unencrypted, both of these are required
            summary += dc.render_pretty_line(url_header, self.url)
            summary += dc.render_pretty_list(info_header, self.info.to_pretty_summary_as_list())
        if self.thumbnail_url and self.thumbnail_info:
            # This file was unencrypted
            summary += dc.render_pretty_line(thumbnail_url_header, self.thumbnail_url)
            summary += dc.render_pretty_list(thumbnail_info_header, self.thumbnail_info.to_pretty_summary_as_list())

        return summary

    def to_extras_summary_line(self) -> str:
        summary = ""
        if self.info:
            summary += f"mimetype: {self.info.mimetype} "
        return summary


class MessageContent(UnencryptedRoomContent):
    """
    Message type Room Content. Covers msgtypes of:
        * m.text
        * m.notice
        * m.emote
        * m.key.verification.request (?)
    """

    #     m.text(Basic most common type of message)
    #     m.emote(a '/me' emote command)
    #     m.notice(Usually a response from a bot)
    #     m.key.verification.request(no idea)
    MESSAGE_TYPES = ["m.text", "m.notice", "m.emote", "m.key.verification.request"]

    format: Optional[str] = None
    formatted_body_size: Optional[int] = None

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        super().__init__(raw_data)
        self.format = self._raw_content.pop("format", None)
        formatted_body: Optional[str] = self._raw_content.pop("formatted_body", None)
        if formatted_body:
            self.formatted_body_size = len(formatted_body)

    def to_pretty_content_summary(self, dc: DisplayLineColumnConfig) -> str:
        format_header = "Format"
        formatted_body_size_header = "Formatted Body Size"
        dc.maybe_update_column_width(len(formatted_body_size_header))

        summary = super().to_pretty_content_summary(dc)
        summary += dc.render_pretty_line(format_header, self.format)
        summary += dc.render_pretty_line(formatted_body_size_header, self.formatted_body_size)

        return summary

    def to_extras_summary_line(self) -> str:
        summary = ""
        if self.body:
            if self.body_size > 20:
                summary += f"{self.body[:20]}"
            else:
                summary += f"{self.body}"
        return summary


class LocationContent(UnencryptedRoomContent):
    """
    Location type Room Content. Covers msgtype of:
        * m.location

    As a note: I have no way to test this, that I know of. Consider it experimental
    """

    MESSAGE_TYPES = ["m.location"]
    geo_uri: Optional[str]
    # info is listed as 'LocationInfo' in the spec, but it's just a Dict that contains
    # thumbnail bits
    thumbnail_info: Optional[MediaMetadata] = None
    thumbnail_url: Optional[str] = None

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        super().__init__(raw_data)
        self.geo_uri = self._raw_content.pop("geo_uri", None)
        info = self._raw_content.get("info", {})
        if info:
            self.thumbnail_url = info.pop("thumbnail_url", None)
            thumbnail_info: Optional[Dict[str, Any]] = info.pop("thumbnail_info", None)
            if thumbnail_info:
                self.thumbnail_info = MediaMetadata(thumbnail_info)

    def to_pretty_content_summary(self, dc: DisplayLineColumnConfig) -> str:
        geo_uri_header = "Geo URI"
        thumbnail_info_header = "Thumbnail Info"
        thumbnail_url_header = "Thumbnail URL"
        dc.maybe_update_column_width(len(thumbnail_info_header))

        summary = super().to_pretty_content_summary(dc)
        summary += dc.render_pretty_line(geo_uri_header, self.geo_uri)
        summary += dc.render_pretty_line(thumbnail_url_header, self.thumbnail_url)
        if self.thumbnail_info:
            summary += dc.render_pretty_list(thumbnail_info_header, self.thumbnail_info.to_pretty_summary_as_list())

        return summary

    def to_extras_summary_line(self) -> str:
        summary = ""
        if self.geo_uri:
            summary += f"geo_uri: {self.geo_uri} "
        return summary


def determine_content_msgtype(data_to_use: Dict[str, Any]) -> UnencryptedRoomContent:
    msgtype = data_to_use.get("msgtype", None)
    if msgtype in MessageContent.MESSAGE_TYPES:
        return MessageContent(data_to_use)
    if msgtype in FileBasedContent.MESSAGE_TYPES:
        return FileBasedContent(data_to_use)
    if msgtype in LocationContent.MESSAGE_TYPES:
        return LocationContent(data_to_use)

    return UnencryptedRoomContent(data_to_use)


class UnencryptedRoomEvent(Event):
    """
    m.room.message

    """

    EVENT_TYPES = ["m.room.message", "m.reaction"]
    rc: UnencryptedRoomContent

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.rc = determine_content_msgtype(self.content)

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        msgtype_header = "Message Type"
        mentions_header = "Mentions"
        column_width = 6
        if self.rc.msgtype:
            column_width = max(column_width, len(msgtype_header))
        dc.maybe_update_column_width(column_width)
        summary = super().to_pretty_summary(dc, room_version)
        summary += self.rc.to_pretty_content_summary(dc)
        if self.rc.mentions:
            summary += dc.render_pretty_list(mentions_header, self.rc.mentions.to_list())

        return summary

    def to_extras_summary(self) -> str:
        return self.rc.to_extras_summary_line()


class StickerRoomEvent(Event):
    """
    m.sticker

    Is not a m.image(even though it has the same fields) and is not a m.room.message
    """

    EVENT_TYPES = ["m.sticker"]
    rc: UnencryptedRoomContent

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        # Has all the hallmarks of a m.image, except not having the correct(or any)
        # msgtype
        self.rc = FileBasedContent(self.content)

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        mentions_header = "Mentions"
        dc.maybe_update_column_width(len(mentions_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += self.rc.to_pretty_content_summary(dc)
        if self.rc.mentions:
            summary += dc.render_pretty_list(mentions_header, self.rc.mentions.to_list())

        return summary


class StrippedStateEvent(EventBase):
    """
    Stripped State Events can only have the keys:
    content, sender, state_key, type
    """

    state_key: str

    def __init__(self, event_id: EventID, data: Dict[str, Any]) -> None:
        super().__init__(event_id, data)
        self.state_key = self.unrecognized.pop("state_key", "State Key Not Found")

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        state_key_header = "State Key"
        dc.maybe_update_column_width(len(state_key_header))
        buffered_message = super().to_pretty_summary(dc, room_version)
        buffered_message += f"{dc.front_pad(state_key_header)}: {self.state_key}\n"
        return buffered_message


class GenericStateEvent(StrippedStateEvent, Event):
    """
    The most basic of State Events. Subclass of Stripped State Event to bring in the
    state key. More specific kinds of State Events will use this super class.
    """

    prev_state: List[str]
    replaces_state: Optional[str]

    def __init__(self, event_id: EventID, data: Dict[str, Any]) -> None:
        super().__init__(event_id, data)
        # Recall that auth_events and prev_events are part of the base class EventBase
        self.replaces_state = self.unrecognized.get("unsigned", {}).pop("replaces_state", None)
        self.prev_state = self.unrecognized.pop("prev_state", [])

    def to_pretty_summary_footer(
        self,
        event_data_map: Dict[str, str],
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
    ) -> str:
        dc.maybe_update_column_width(17)
        summary = super().to_pretty_summary_footer(event_data_map, dc)
        for prev_state in self.prev_state:
            summary += (
                f"{dc.front_pad('<- Prev State')}: {prev_state}: {event_data_map.get(prev_state, 'Data Missing')}\n"
            )
        if self.replaces_state:
            summary += f"{dc.front_pad('-> Replaces State')}: {self.replaces_state}: {event_data_map.get(self.replaces_state, 'Data Missing')}\n"

        return summary


class CanonicalAliasStateEvent(GenericStateEvent):
    """
    A state Event of type m.room.canonical_alias
    """

    alias: Optional[str]
    alt_aliases: List[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.alias = self.content.pop("alias", None)
        self.alt_aliases = self.content.pop("alt_aliases", [])

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        alias_header = "Alias"
        alt_alias_header = "Alt Aliases"
        dc.maybe_update_column_width(9)
        summary = super().to_pretty_summary(dc=dc, room_version=room_version)
        summary += dc.render_pretty_line(alias_header, self.alias)
        summary += dc.render_pretty_list(alt_alias_header, self.alt_aliases)
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig("Depth"),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig("Event ID"),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig("Event Type"),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig("Sender"),
    ) -> str:
        # Content will always be the last column, as it has the most variability.
        # Therefore, it doesn't need a column config, as there will be no padding.
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.alias:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"alias: {self.alias}"


class PreviousRoom:
    room_id: str
    event_id: str

    def __init__(self, room_id: str, event_id: str) -> None:
        self.room_id = room_id
        self.event_id = event_id

    def to_pretty_list(self) -> List[str]:
        summary_list = []
        summary_list.extend([f" Room ID: {self.room_id}"])
        summary_list.extend([f"Event ID: {self.event_id}"])
        return summary_list


class CreateRoomStateEvent(GenericStateEvent):
    """
    A state Event of type m.room.create
    """

    # Creator is only used from room version 1-10, after that use the sender
    creator: Optional[str]
    # Following is used for m.federate
    federation_allowed: bool
    predecessor: Optional[PreviousRoom] = None
    predecessor_last_event_id: Optional[EventID]
    predecessor_last_room_id: Optional[str]
    room_version: int
    room_type: Optional[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.creator = self.content.pop("creator", "Not Found")
        self.federation_allowed = self.content.pop("m.federate", True)
        predecessor_content = self.content.pop("predecessor", {})
        # This one needs to stay a str
        last_event_id: Optional[str] = predecessor_content.get("event_id", None)
        last_room_id: Optional[str] = predecessor_content.get("room_id", None)
        if last_event_id and last_room_id:
            self.predecessor = PreviousRoom(room_id=last_room_id, event_id=last_event_id)
        self.room_version = int(self.content.pop("room_version", 1))
        self.room_type = self.content.pop("room_type", None)

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        creator_header = "Room Creator"
        fed_allowed_header = "m.federate"
        pred_header = "Predecessor"
        ver_header = "Room Version"
        room_type_header = "Room Type"
        # JOIN_RULE = "Join Rule"

        dc.maybe_update_column_width(12)
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(creator_header, self.creator)
        summary += dc.render_pretty_line(fed_allowed_header, self.federation_allowed)
        if self.predecessor:
            summary += dc.render_pretty_list(pred_header, self.predecessor.to_pretty_list())

        summary += dc.render_pretty_line(ver_header, self.room_version)
        summary += dc.render_pretty_line(room_type_header, self.room_type)
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"ver: {self.room_version} "


class AllowCondition:
    """
    At the moment, there is only one type of condition: m.room.membership
    If the type is m.room.membership, then room_id is required
    """

    room_id: str
    condition_type: str

    def __init__(self, room_id: str, condition_type: str) -> None:
        self.room_id = room_id
        self.condition_type = condition_type


class AllowConditions:
    """
    A wrapper around a List for each set of conditions/room_ids

    """

    list_of_conditions: List[AllowCondition] = []

    def to_pretty_list(self) -> List[str]:
        summary_list = []
        for cond in self.list_of_conditions:
            summary_list.extend([f"*   Type: {cond.condition_type}"])
            summary_list.extend([f" Room ID: {cond.room_id}"])

        return summary_list


class JoinRulesStateEvent(GenericStateEvent):
    """
    A state Event for m.room.join_rules
    """

    join_rule: str
    allow: AllowConditions

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.join_rule = self.content.pop("join_rule", "Not Found")
        self.allow = AllowConditions()
        # Allow should only be populated if join_rule is restricted. And it's a list
        if self.join_rule not in ("restricted", "knock_restricted"):
            return
        allow_list = self.content.pop("allow", [])
        for allow_entry in allow_list:
            room_id = allow_entry.pop("room_id")
            condition_type = allow_entry.pop("type")
            allow = AllowCondition(room_id=room_id, condition_type=condition_type)
            self.allow.list_of_conditions.append(allow)

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"join_rule: {self.raw_data.get("content")} "

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        join_rule_header = "Join Rule"
        dc.maybe_update_column_width(len(join_rule_header))

        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(join_rule_header, self.join_rule)
        summary += dc.render_pretty_list("Allow List", self.allow.to_pretty_list())
        return summary


class Signed:
    mxid: str
    signatures: Dict[ServerName, Dict[KeyID, Signature]]
    token: str

    def __init__(
        self,
        mxid: str,
        token: str,
        signatures: Dict[ServerName, Dict[KeyID, Signature]],
    ) -> None:
        self.mxid = mxid
        self.token = token
        self.signatures = signatures


class Invite:
    display_name: str
    signed: Signed

    def __init__(self, raw_data: Dict[str, Any]) -> None:
        self.display_name = raw_data.pop("displayname", "Not Found(Invite class)")
        signed = raw_data.pop("signed", {})
        if signed:
            mxid = signed.pop("mxid", "Not Found(Invite class)")
            token = signed.pop("token", "Not Found(Invite class)")
            signatures = signed.pop("signatures", {})
            self.signed = Signed(mxid=mxid, token=token, signatures=signatures)


class RoomMemberStateEvent(GenericStateEvent):
    """
    The state event to for type m.room.member
    """

    avatar_url: Optional[str]
    displayname: Optional[str]
    is_direct: Optional[bool]
    join_authorised_via_users_server: Optional[str]
    membership: str
    reason: Optional[str]
    third_party_invite: Optional[Invite] = None

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.avatar_url = self.content.pop("avatar_url", None)
        self.displayname = self.content.pop("displayname", None)
        self.is_direct = self.content.pop("is_direct", None)
        self.join_authorised_via_users_server = self.content.pop("join_authorised_via_users_server", None)
        self.membership = self.content.pop("membership", "Defaulting to 'leave'")
        self.reason = self.content.pop("reason", None)
        third_party_invite = self.content.pop("third_party_invite", {})
        if third_party_invite:
            self.third_party_invite = Invite(third_party_invite)

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"membership: {self.membership} target: {self.state_key} "

    def to_short_type_summary(self) -> str:
        return f"{self.event_type} {self.sender} {self.state_key} {self.membership}"

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        membership_header = "Membership"
        is_direct_header = "Is Direct"
        reason_header = "Reason"
        displayname_header = "Display Name"
        avatar_url_header = "Avatar URL"
        JAVUS_header = "Join Authorised via Users Server"
        third_party_invite_header = "Third Party Invite"

        if self.membership:
            dc.maybe_update_column_width(len(membership_header))
        if self.is_direct:
            dc.maybe_update_column_width(len(is_direct_header))
        if self.reason:
            dc.maybe_update_column_width(len(reason_header))
        if self.displayname:
            dc.maybe_update_column_width(len(displayname_header))
        if self.avatar_url:
            dc.maybe_update_column_width(len(avatar_url_header))
        if self.join_authorised_via_users_server:
            dc.maybe_update_column_width(len(JAVUS_header))
        if self.third_party_invite:
            dc.maybe_update_column_width(len(third_party_invite_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(membership_header, self.membership)
        summary += dc.render_pretty_line(is_direct_header, self.is_direct)
        summary += dc.render_pretty_line(reason_header, self.reason)
        summary += dc.render_pretty_line(displayname_header, self.displayname)
        summary += dc.render_pretty_line(avatar_url_header, self.avatar_url)
        summary += dc.render_pretty_line(JAVUS_header, self.join_authorised_via_users_server)
        summary += dc.render_pretty_line(
            third_party_invite_header,
            self.third_party_invite.signed.mxid if self.third_party_invite else None,
        )
        return summary


class PowerLevelStateEvent(GenericStateEvent):
    """
    A State Event for type m.room.power_levels
    """

    ban: str
    events: Dict[str, str]
    events_default: str
    invite: str
    kick: str
    notifications: Dict[str, str]
    redact: str
    state_default: str
    users: Dict[str, str]
    users_default: str

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.ban = self.content.pop("ban", "Not Found(Defaults to 50)")
        self.invite = self.content.pop("invite", "Not Found(Defaults to 0)")
        self.kick = self.content.pop("kick", "Not Found(Defaults to 50)")
        self.redact = self.content.pop("redact", "Not Found(Defaults to 50)")
        self.events_default = self.content.pop("events_default", "Not Found(Defaults to 0)")
        self.state_default = self.content.pop("state_default", "Not Found(Defaults to 50)")
        self.users_default = self.content.pop("users_default", "Not Found(Defaults to 0)")
        self.events = self.content.pop("events", {})
        self.notifications = self.content.pop("notifications", {})
        self.users = self.content.pop("users", {})

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        # This one is more complicated. Beyond the standard power level
        # sections/defaults, there are also(potentially) three columns to display.

        # The longest header for the main summary is 'Events Default' and it will always
        # be displayed, just hardwire it
        dc.maybe_update_column_width(14)

        user_key_col = DisplayLineColumnConfig("Users")
        user_val_col = DisplayLineColumnConfig("")
        user_num_entries = len(self.users)

        events_key_col = DisplayLineColumnConfig("Events")
        events_val_col = DisplayLineColumnConfig("")
        events_num_entries = len(self.events)

        notif_key_col = DisplayLineColumnConfig("Notifications")
        notif_val_col = DisplayLineColumnConfig("")
        notif_num_entries = len(self.notifications)

        max_entries_to_display = max(user_num_entries, events_num_entries, notif_num_entries)

        # Prepare the whole list of entries, so we know how many lines will be needed
        complex_buffer_lines_list = []
        for _ in range(max_entries_to_display):
            complex_buffer_lines_list.extend([""])

        # Calculate the width of each detail column, may only use two of the three
        if self.users:
            max_len_of_user_keys = extract_max_key_len_from_dict(self.users)
            max_len_of_user_vals = extract_max_value_len_from_dict(self.users)
            user_key_col.maybe_update_column_width(max_len_of_user_keys)
            user_val_col.maybe_update_column_width(max_len_of_user_vals)

        if self.events:
            max_len_of_events_keys = extract_max_key_len_from_dict(self.events)
            max_len_of_events_vals = extract_max_value_len_from_dict(self.events)
            events_key_col.maybe_update_column_width(max_len_of_events_keys)
            events_val_col.maybe_update_column_width(max_len_of_events_vals)

        if self.notifications:
            max_len_of_notif_keys = extract_max_key_len_from_dict(self.notifications)
            notif_key_col.maybe_update_column_width(max_len_of_notif_keys)
            max_len_of_notif_vals = extract_max_value_len_from_dict(self.notifications)
            notif_val_col.maybe_update_column_width(max_len_of_notif_vals)

        # Render the main summary bits
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line("Users Default", self.users_default)
        summary += dc.render_pretty_line("Events Default", self.events_default)
        summary += dc.render_pretty_line("State Default", self.state_default)
        summary += dc.render_pretty_line("Ban", self.ban)
        summary += dc.render_pretty_line("Invite", self.invite)
        summary += dc.render_pretty_line("Kick", self.kick)
        summary += dc.render_pretty_line("Redact", self.redact)

        # Give it a little space
        summary += "\n"
        # Build the header line of the various mappings. Exclude those that are missing.
        # Let's build the actual lines while here too. Append them afterwards
        if self.users:
            summary += f"{user_key_col.front_pad()}:{user_val_col.pad()} | "
            count = 0
            for user, level in self.users.items():
                complex_buffer_lines_list[count] += f"{user_key_col.front_pad(user)}:{user_val_col.pad(str(level))} | "
                count = count + 1
            while count < max_entries_to_display:
                complex_buffer_lines_list[count] += f"{user_key_col.front_pad('')} {user_val_col.pad('')} | "
                count = count + 1

        if self.events:
            summary += f"{events_key_col.front_pad()}:{events_val_col.pad()} | "
            count = 0
            for user, level in self.events.items():
                complex_buffer_lines_list[
                    count
                ] += f"{events_key_col.front_pad(user)}:{events_val_col.pad(str(level))} | "
                count = count + 1
            while count < max_entries_to_display:
                complex_buffer_lines_list[count] += f"{events_key_col.front_pad('')} {events_val_col.pad('')} | "
                count = count + 1

        if self.notifications:
            summary += f"{notif_key_col.front_pad()}:{notif_val_col.pad()}"
            count = 0
            for user, level in self.notifications.items():
                complex_buffer_lines_list[count] += f"{notif_key_col.front_pad(user)}:{notif_val_col.pad(str(level))}"
                count = count + 1
            while count < max_entries_to_display:
                complex_buffer_lines_list[count] += f"{notif_key_col.front_pad('')} {notif_val_col.pad('')} "
                count = count + 1

        # In case one of the above wasn't the end, make it so
        summary += "\n"
        if complex_buffer_lines_list:
            for line_item in complex_buffer_lines_list:
                summary += f"{line_item}\n"
        return summary


class RedactionStateEvent(GenericStateEvent):
    """
    A State event of type m.room.redaction
    """

    reason: str
    redacts: str

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.reason = self.content.pop("reason", "")
        self.redacts = self.content.pop("redacts", "Not Found(Is this a version 11 room?")

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        dc.maybe_update_column_width(7)
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line("Redacts", self.redacts)
        summary += dc.render_pretty_line("Reason", self.reason)
        return summary


class RoomNameStateEvent(GenericStateEvent):
    """
    m.room.name
    """

    name: Optional[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.name = self.content.pop("name", None)

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.name:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"name: {self.name} "

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        room_name_header = "Room Name"
        dc.maybe_update_column_width(len(room_name_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(room_name_header, self.name)
        return summary


class RoomTopicStateEvent(GenericStateEvent):
    """
    m.room.topic
    """

    topic: Optional[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.topic = self.content.pop("topic", None)

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        topic_header = "Room Topic"
        dc.maybe_update_column_width(len(topic_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(topic_header, self.topic)
        return summary


class RoomAvatarStateEvent(GenericStateEvent):
    """
    m.room.avatar
    """

    url: Optional[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.url = self.content.pop("url", None)

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.url:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"avatar: {self.url} "

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        avatar_url_header = "Avatar URL"
        dc.maybe_update_column_width(len(avatar_url_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(avatar_url_header, self.url)
        return summary


class RoomEncryptionStateEvent(GenericStateEvent):
    """
    m.room.encryption
    """

    algorithm: Optional[str] = None

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.algorithm = self.content.pop("algorithm", None)

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"algo: {self.algorithm} "

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        algo_header = "Algorithm"
        dc.maybe_update_column_width(len(algo_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(algo_header, self.algorithm)
        return summary


class RoomPinnedEventsStateEvent(GenericStateEvent):
    """
    m.room.pinned_events
    """

    pinned: List[EventID]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.pinned = self.content.pop("pinned", [])

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"# of events: {len(self.pinned)} "

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        pinned_header = "Pinned Events"
        dc.maybe_update_column_width(len(pinned_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_list(pinned_header, self.pinned)
        return summary


class HistoryVisibilityStateEvent(GenericStateEvent):
    """
    m.room.history_visibility
    """

    history_visibility: Optional[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.history_visibility = self.content.pop("history_visibility", None)

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        hv_header = "History Visibility"
        dc.maybe_update_column_width(len(hv_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(hv_header, self.history_visibility)
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.history_visibility:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"his_vis: {self.history_visibility} "


class SpaceParentStateEvent(GenericStateEvent):
    """
    m.space.parent
    """

    canonical: bool
    via: List[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.canonical = self.content.pop("canonical", False)
        self.via = self.content.pop("via", [])

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        sp_header = "Space Parent"
        via_header = "Via"
        can_header = "Canonical"
        dc.maybe_update_column_width(len(sp_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(sp_header, self.state_key)
        summary += dc.render_pretty_list(via_header, self.via)
        summary += dc.render_pretty_line(can_header, self.canonical)
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.state_key:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"parent: {self.state_key} "


class SpaceChildStateEvent(GenericStateEvent):
    """
    m.space.child
    """

    order: Optional[str]
    suggested: bool
    via: List[str]

    def __init__(self, event_id: EventID, raw_data: Dict[str, Any]) -> None:
        super().__init__(event_id, raw_data)
        self.order = self.content.pop("order", None)
        self.suggested = self.content.pop("suggested", False)
        self.via = self.content.pop("via", [])

    def to_pretty_summary(
        self,
        dc: DisplayLineColumnConfig = DisplayLineColumnConfig(""),
        room_version: Optional[int] = None,
    ) -> str:
        sc_header = "Space Child"
        suggested_header = "Suggested"
        order_header = "Order"
        via_header = "Via"
        dc.maybe_update_column_width(len(sc_header))
        summary = super().to_pretty_summary(dc, room_version)
        summary += dc.render_pretty_line(sc_header, self.state_key)
        summary += dc.render_pretty_list(via_header, self.via)
        summary += dc.render_pretty_line(suggested_header, self.suggested)
        summary += dc.render_pretty_line(order_header, self.order)
        return summary

    def to_line_summary(
        self,
        dc_depth: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_DEPTH),
        dc_eid: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_ID),
        dc_etype: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_TYPE),
        dc_sender: DisplayLineColumnConfig = DisplayLineColumnConfig(EVENT_SENDER),
    ) -> str:
        summary = super().to_line_summary(dc_depth, dc_eid, dc_etype, dc_sender)
        if self.state_key:
            summary += self.to_extras_summary()
        return summary

    def to_extras_summary(self) -> str:
        return f"child: {self.state_key}"


def determine_what_kind_of_event(
    event_id: Optional[EventID],
    room_version: Optional[int],
    data_to_use: Dict[str, Any],
) -> EventBase:
    # Everything is a kind of Event
    # However, the actual EventID may be missing, as most server responses don't include
    # it. Create an empty one, as it's not a big deal. Since it's for display, if it's
    # not there it won't be displayed
    if not event_id:
        if room_version and room_version > 3:
            # For the moment, we can only discover Event ID's from room version's before 4
            event_id = EventID(construct_event_id_from_event_v3(room_version, data_to_use))
        else:
            event_id = EventID("")

    if "state_key" in data_to_use:
        event_type = data_to_use.get("type", None)
        if event_type == "m.room.canonical_alias":
            return CanonicalAliasStateEvent(event_id, data_to_use)
        if event_type == "m.room.create":
            return CreateRoomStateEvent(event_id, data_to_use)
        if event_type == "m.room.join_rules":
            return JoinRulesStateEvent(event_id, data_to_use)
        if event_type == "m.room.member":
            return RoomMemberStateEvent(event_id, data_to_use)
        if event_type == "m.room.power_levels":
            return PowerLevelStateEvent(event_id, data_to_use)
        if event_type == "m.room.redaction":
            return RedactionStateEvent(event_id, data_to_use)
        if event_type == "m.room.name":
            return RoomNameStateEvent(event_id, data_to_use)
        if event_type == "m.room.topic":
            return RoomTopicStateEvent(event_id, data_to_use)
        if event_type == "m.room.avatar":
            return RoomAvatarStateEvent(event_id, data_to_use)
        if event_type == "m.room.encryption":
            return RoomEncryptionStateEvent(event_id, data_to_use)
        if event_type == "m.room.pinned_events":
            return RoomPinnedEventsStateEvent(event_id, data_to_use)
        if event_type == "m.room.history_visibility":
            return HistoryVisibilityStateEvent(event_id, data_to_use)
        if event_type == "m.space.parent":
            return SpaceParentStateEvent(event_id, data_to_use)
        if event_type == "m.space.child":
            return SpaceChildStateEvent(event_id, data_to_use)

        return GenericStateEvent(event_id, data_to_use)

    # Not a state Event
    event_type = data_to_use.get("type")
    if event_type == "m.room.encrypted":
        return EncryptedRoomEvent(event_id, data_to_use)
    if event_type in UnencryptedRoomEvent.EVENT_TYPES:
        return UnencryptedRoomEvent(event_id, data_to_use)
    if event_type in StickerRoomEvent.EVENT_TYPES:
        return StickerRoomEvent(event_id, data_to_use)

    return Event(event_id, data_to_use)


def construct_event_id_from_event_v3(
    room_version: int,
    data_to_use: Dict[str, Any],
) -> str:
    """
    Event ID's used to be generated by the server receiving and added to the Event PDU.
    In room version 3, this changed to an unsafe reference hash of sha256. In room
    version 4, this changed to a url-safe unpadded base64 encoded string of the sha256
    reference hash. This function is for room versions greater than or equal to 4.
    Args:
        room_version: The int format of the room version
        data_to_use: The Event PDU

    Returns:

    """
    # The event ID is the reference hash of the event encoded using a variation of
    # Unpadded Base64 which replaces the 62nd and 63rd characters with - and _ instead
    # of using + and /. This matches RFC4648’s definition of URL-safe base64.

    # The reference hash of an event covers the essential fields of an event, including
    # content hashes. It is used for event identifiers in some room versions. See the
    # room version specification for more information. It is calculated as follows.
    #
    #     The event is put through the redaction algorithm.
    #     The signatures and unsigned properties are removed from the event, if present.
    #     The event is converted into Canonical JSON.
    #     A sha256 hash is calculated on the resulting JSON object.
    redacted_data = redact_event(room_version, data_to_use)
    redacted_data.pop("signatures", None)
    redacted_data.pop("unsigned", None)
    encoded_redacted_event_bytes = encode_canonical_json(redacted_data)
    reference_content = hashlib.sha256(encoded_redacted_event_bytes)
    reference_hash = encode_base64(reference_content.digest(), True)
    return "$" + reference_hash


# [Redactions]
# v1_to_v5
# Upon receipt of a redaction event, the server must strip off any keys not in the
# following list:
#
#     event_id
#     type
#     room_id
#     sender
#     state_key
#     content
#     hashes
#     signatures
#     depth
#     prev_events
#     prev_state
#     auth_events
#     origin
#     origin_server_ts
#     membership

# The content object must also be stripped of all keys, unless it is one of the
# following event types:
#
#     m.room.member allows key membership.
#     m.room.create allows key creator.
#     m.room.join_rules allows key join_rule.
#     m.room.power_levels allows keys ban, events, events_default, kick, redact,
#           state_default, users, users_default.
#     m.room.aliases allows key aliases.
#     m.room.history_visibility allows key history_visibility.


v1_to_v5_list_of_keys_to_keep = [
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "prev_state",
    "auth_events",
    "origin",
    "origin_server_ts",
    "membership",
]
v1_to_v5_map_of_keys_from_content_to_keep = {
    "m.room.member": ["membership"],
    "m.room.create": ["creator"],
    "m.room.join_rules": ["join_rule"],
    "m.room.aliases": ["aliases"],
    "m.room.history_visibility": ["history_visibility"],
    "m.room.power_levels": [
        "ban",
        "events",
        "events_default",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
    ],
}

# v6_to_v7
# This is mostly the same as v1_to_v5, however we no longer apply:
# - m.room.aliases allows key aliases.

v6_to_v7_list_of_keys_to_keep = [
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "prev_state",
    "auth_events",
    "origin",
    "origin_server_ts",
    "membership",
]
v6_to_v7_map_of_keys_from_content_to_keep = {
    "m.room.member": ["membership"],
    "m.room.create": ["creator"],
    "m.room.join_rules": ["join_rule"],
    "m.room.history_visibility": ["history_visibility"],
    "m.room.power_levels": [
        "ban",
        "events",
        "events_default",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
    ],
}

# v8
# This is mostly the same as v6_to_v7, however we now apply:
# + m.room.join_rules now allows key 'allow'.

v8_list_of_keys_to_keep = [
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "prev_state",
    "auth_events",
    "origin",
    "origin_server_ts",
    "membership",
]
v8_map_of_keys_from_content_to_keep = {
    "m.room.member": ["membership"],
    "m.room.create": ["creator"],
    "m.room.join_rules": ["allow", "join_rule"],
    "m.room.history_visibility": ["history_visibility"],
    "m.room.power_levels": [
        "ban",
        "events",
        "events_default",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
    ],
}

# v9_to_v10
# Building on v8, however we now apply that:
# + 'm.room.member' events now keep 'join_authorised_via_users_server' in addition to
#       other keys in content when being redacted.

v9_to_v10_list_of_keys_to_keep = [
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "prev_state",
    "auth_events",
    "origin",
    "origin_server_ts",
    "membership",
]
v9_to_v10_map_of_keys_from_content_to_keep = {
    "m.room.member": ["join_authorised_via_users_server", "membership"],
    "m.room.create": ["creator"],
    "m.room.join_rules": ["allow", "join_rule"],
    "m.room.history_visibility": ["history_visibility"],
    "m.room.power_levels": [
        "ban",
        "events",
        "events_default",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
    ],
}


# This is mostly the same as v9_to_v10, however we now apply:
# - The top-level 'origin', 'membership', and 'prev_state' properties are no longer
#   protected from redaction.
# + The 'm.room.create' event now keeps the entire 'content' property.
# + The 'm.room.redaction' event keeps the 'redacts' property under 'content'.
# + The 'm.room.power_levels' event keeps the 'invite' property under 'content'.

# With 'm.room.create' now keeping all keys under 'content', we don't have a clean way
# to model that outside of hard-coding all potential options. If that changes in the
# future, it may be difficult to fix. Instead, a 'magic constant' allows the flexibility
ALL_KEYS = "_ALL_KEYS"
v11_list_of_keys_to_keep = [
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "auth_events",
    "origin_server_ts",
]
v11_map_of_keys_from_content_to_keep = {
    "m.room.member": ["join_authorised_via_users_server", "membership"],
    "m.room.create": [ALL_KEYS],
    "m.room.join_rules": ["allow", "join_rule"],
    "m.room.history_visibility": ["history_visibility"],
    "m.room.redaction": ["redacts"],
    "m.room.power_levels": [
        "ban",
        "events",
        "events_default",
        "invite",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
    ],
}


def _redact_with(
    data_to_use: Dict[str, Any],
    list_of_keys_to_keep: List[str],
    map_of_keys_from_content_to_keep: Dict[str, List[str]],
) -> Dict[str, Any]:
    redacted_data_result: Dict[str, Any] = {}
    # grab the type early, for use with the 'map' argument
    event_type = data_to_use.get("type")

    # top-level
    for key, value in data_to_use.items():
        if key in list_of_keys_to_keep:
            if key != "content":
                redacted_data_result[key] = value
            else:
                assert isinstance(event_type, str)
                content_list_to_keep = map_of_keys_from_content_to_keep.get(event_type, [])
                # Because of the else clause above, we know a 'content' key did exist.
                # Create an empty one, then check if something is supposed to be copied
                # over to it.
                redacted_data_result.setdefault("content", {})
                for content_key, content_value in value.items():
                    # Only in room version 11(and possibly newer versions) do we
                    # have the magic constant 'ALL_KEYS' in the 'list' and only for
                    # an event type of 'm.room.create'. FTR, I'm not a fan of magic
                    # constants.
                    if content_key in content_list_to_keep or ALL_KEYS in content_list_to_keep:
                        redacted_data_result["content"][content_key] = content_value

    return redacted_data_result


def redact_event(room_version: int, data_to_use: Dict[str, Any]) -> Dict[str, Any]:
    if room_version <= 5:
        return _redact_with(
            data_to_use,
            v1_to_v5_list_of_keys_to_keep,
            v1_to_v5_map_of_keys_from_content_to_keep,
        )
    if 5 < room_version <= 7:
        return _redact_with(
            data_to_use,
            v6_to_v7_list_of_keys_to_keep,
            v6_to_v7_map_of_keys_from_content_to_keep,
        )
    if room_version == 8:
        return _redact_with(data_to_use, v8_list_of_keys_to_keep, v8_map_of_keys_from_content_to_keep)
    if 8 < room_version <= 10:
        return _redact_with(
            data_to_use,
            v9_to_v10_list_of_keys_to_keep,
            v9_to_v10_map_of_keys_from_content_to_keep,
        )

    # Greater than room version 10
    return _redact_with(data_to_use, v11_list_of_keys_to_keep, v11_map_of_keys_from_content_to_keep)
