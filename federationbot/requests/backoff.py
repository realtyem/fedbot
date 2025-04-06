import logging

from backoff._typing import Details

backoff_logger = logging.getLogger("dns_backoff")


def backoff_srv_backoff_logging_handler(details: Details) -> None:
    wait = details.get("wait", 0.0)
    tries = details.get("tries", 0)
    # args is a tuple(self, server_name, diag_info), we want the second slot
    host = details.get("args", (None, "arg not found"))[1]
    backoff_logger.debug(
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
    backoff_logger.info(
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
    backoff_logger.debug(
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
    backoff_logger.info(
        "DNS query giving up after %d tries and %.2f seconds on host %s",
        tries,
        elapsed,
        host,
    )
