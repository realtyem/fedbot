# FederationBot

FederationBot is a Maubot plugin for testing federation between [Matrix](https://spec.matrix.org/latest/) servers. It provides functionality to start, pause, and stop tasks through reaction commands. You must give it the local server's signing key so it can run tests as the local server.

## Installation

This plugin is installed in [Maubot](https://github.com/maubot/maubot) so you can then assign it to a client to operate in the account and rooms you choose.

## Commands

FederationBot supports the following commands:

```bash
Usage: !fed <subcommand> [...]

summary - Print summary of the delegation portion of the spec
event_raw [event_id] [server_to_request_from] - None
event [event_id] [server_to_request_from] - None
state [room_id_or_alias] [event_id] [server_to_request_from] - Request state over federation for a room.
version <Server to check> - Check a server in the room for version info
server_keys <server_to_check> - None
server_keys_raw <server_to_check> - None
notary_server_keys <server_to_check> [notary_server_to_use] - None
notary_server_keys_raw <server_to_check> [notary_server_to_use] - None
backfill [room_id_or_alias] [event_id] [limit] [server_to_request_from] - Request backfill over federation for a room.
event_auth [room_id_or_alias] <event_id> [server_to_request_from] - Request the auth chain for an event over federation
user_devices <user_mxid> - Request user devices over federation for a user.
```

## Contributing

Issues and PRs are welcome. Please follow the style of the existing project.

## License

Currently pending a license. Currently all rights reserved.
