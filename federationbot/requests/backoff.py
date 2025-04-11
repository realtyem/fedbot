import logging

from backoff._typing import Details

from federationbot.server_result import ServerResult

dns_bo_logger = logging.getLogger("dns_backoff")
srv_bo_logger = logging.getLogger("srv_backoff")
fed_bo_logger = logging.getLogger("fed_backoff")
dns_gu_logger = logging.getLogger("dns_giveup")
srv_gu_logger = logging.getLogger("srv_giveup")
fed_gu_logger = logging.getLogger("fed_giveup")


def backoff_logging_backoff_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    host = details.get("args", (None, "arg not found"))[1]
    fed_bo_logger.debug(
        "Backing off %.2f seconds after %d tries on host %s",
        wait,
        tries,
        host,
    )


def backoff_logging_giveup_handler(details: Details) -> None:
    elapsed = details.get("elapsed", 0.0)
    tries = details.get("tries", 0)
    host = details.get("args", (None, "arg not found"))[1]
    fed_gu_logger.debug(
        "Giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
    )


def backoff_update_retries_handler(details: Details) -> None:
    server_result: ServerResult | None = details.get("kwargs", {}).get("server_result", None)
    if server_result and server_result.diag_info:
        server_result.diag_info.retries += 1


def backoff_srv_backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    srv_bo_logger.debug(
        "DNS SRV query backing off %.2f seconds after %d tries on host %s",
        wait,
        tries,
        host,
    )


def backoff_srv_giveup_logging_handler(details: Details) -> None:
    elapsed = details.get("elapsed", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    srv_gu_logger.info(
        "DNS SRV query giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
    )


def backoff_dns_backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    dns_bo_logger.debug(
        "DNS query backing off %.2f seconds after %d tries on host %s",
        wait,
        tries,
        host,
    )


def backoff_dns_giveup_logging_handler(details: Details) -> None:
    elapsed = details.get("elapsed", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    dns_gu_logger.info(
        "DNS query giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
    )
