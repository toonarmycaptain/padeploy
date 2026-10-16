"""Summarize a web app's access log: your requests and everyone else's."""

import gzip
import ipaddress
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import requests

from .api import PAClient

LOG_DIR = "/var/log"
LOG_TIME = "%d/%b/%Y:%H:%M:%S %z"  # e.g. 13/Jun/2026:11:40:06 +0000
# api.ipify.org answers over IPv4 and api6.ipify.org over IPv6, so a dual-stack machine gets both.
IPIFY_URLS = ("https://api.ipify.org", "https://api6.ipify.org")
IPIFY_TIMEOUT = 10.0
# A home network's devices share one /64 and change their IPv6 addresses within it.
HOME_PREFIX = 64

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# IP - - [time] "METHOD path PROTOCOL" status bytes "referer" "user agent" "X-Forwarded-For"
# Some lines have no X-Forwarded-For field.
_LINE_RE = re.compile(
    r'(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3}) \S+'
    r' "[^"]*" "[^"]*"(?:\s+"(?P<xff>[^"]*)")?'
)


@dataclass(frozen=True)
class Request:
    """One request from the access log."""

    ip: str
    time: datetime
    method: str
    path: str
    status: str


@dataclass(frozen=True)
class Logs:
    """Access logs as read: their names, newest first, requests, and count of unparsed lines."""

    names: list[str]
    requests: list[Request]
    unparsed: int


def log_names(names: Iterable[str], domain: str) -> list[str]:
    """``domain``'s access logs among ``names``, newest first: live, then ``.1``, ``.2.gz``…"""
    pattern = re.compile(re.escape(f"{domain}.access.log") + r"(?:\.(\d+)(?:\.gz)?)?")
    logs = sorted((int(m[1] or 0), name) for name in names if (m := pattern.fullmatch(name)))
    return [name for _, name in logs]


def read_logs(client: PAClient, domain: str, back_to: datetime | None) -> Logs:
    """Fetch and parse ``domain``'s access logs, newest first, until one goes back to ``back_to``.

    With ``back_to`` None, all of them.
    """
    names = []
    requests = []
    unparsed = 0
    for name in log_names(client.list_dir(LOG_DIR), domain):
        content = client.get_file(f"{LOG_DIR}/{name}")
        if name.endswith(".gz"):
            content = gzip.decompress(content)
        names.append(name)
        log = []
        for line in content.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            if request := parse_line(line):
                log.append(request)
            else:
                unparsed += 1
        requests.extend(log)
        if back_to and any(r.time <= back_to for r in log):
            break
    return Logs(names, requests, unparsed)


def parse_line(line: str) -> Request | None:
    """The request on an access-log line, or None if the line doesn't parse."""
    if not (match := _LINE_RE.match(line)):
        return None
    try:
        time = datetime.strptime(match["time"], LOG_TIME)
    except ValueError:
        return None
    request = match["request"].split()
    return Request(
        ip=_client_ip(match["ip"], match["xff"]),
        time=time,
        method=request[0] if request else "-",
        path=request[1] if len(request) > 1 else "-",
        status=match["status"],
    )


def _client_ip(real_ip: str, xff: str | None) -> str:
    """The first X-Forwarded-For address, else ``real_ip``.

    Behind a proxy such as Cloudflare, ``real_ip`` is the proxy's, and X-Forwarded-For starts with
    the visitor's.
    """
    if xff and xff.strip() not in ("", "-"):
        return xff.split(",")[0].strip()
    return real_ip


def parse_networks(text: str) -> set[Network]:
    """Comma-separated IP addresses and networks; an IPv6 address stands for its /64."""
    networks: set[Network] = set()
    for item in filter(None, (item.strip() for item in text.split(","))):
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise ValueError(f"{item!r} isn't an IP address or network") from None
        if network.version == 6 and "/" not in item:
            network = network.supernet(new_prefix=HOME_PREFIX)
        networks.add(network)
    return networks


def detect_public_ips() -> set[str]:
    """This machine's public IPv4 and IPv6 addresses, as ipify sees them; empty if unreachable."""
    found = set()
    for url in IPIFY_URLS:
        try:
            response = requests.get(url, timeout=IPIFY_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException:
            continue  # e.g. no IPv6 connectivity
        try:
            found.add(str(ipaddress.ip_address(response.text.strip())))
        except ValueError:
            continue  # not an address, e.g. an empty reply
    return found


def period(
    now: datetime,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    month: str | None = None,
    year: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """The requested period's (start, end) in UTC; None where it's open.

    ``since`` and ``until`` are dates, ``until`` included. An empty ``month`` or ``year`` means the
    last 30 or 365 days.
    """
    if month == "":
        return now - timedelta(days=30), None
    if month:
        return _month(month, now)
    if year == "":
        return now - timedelta(days=365), None
    if year:
        try:
            return datetime(int(year), 1, 1, tzinfo=UTC), datetime(int(year) + 1, 1, 1, tzinfo=UTC)
        except ValueError:
            raise ValueError(f"--year must be a year, e.g. 2026, got {year!r}") from None
    return (
        since.replace(tzinfo=UTC) if since else None,
        until.replace(tzinfo=UTC) + timedelta(days=1) if until else None,
    )


def _month(value: str, now: datetime) -> tuple[datetime, datetime]:
    """The calendar month ``value``: 2026-07, or a name such as July or Jul for the latest one."""
    for fmt in ("%Y-%m", "%B", "%b"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m":
            year = parsed.year
        else:
            year = now.year if parsed.month <= now.month else now.year - 1
        start = datetime(year, parsed.month, 1, tzinfo=UTC)
        return start, (start + timedelta(days=31)).replace(day=1)
    raise ValueError(f"--month must be like 2026-07 or July, got {value!r}")


def report(
    domain: str,
    logs: Logs,
    my_networks: set[Network],
    *,
    start: datetime | None,
    end: datetime | None,
    top: int | None,
    detail: bool,
) -> str:
    """Counts of requests from ``my_networks`` and others, then others' top statuses, errors, etc.

    Only requests from ``start`` until ``end`` count; ``detail`` adds others' latest ones. Lists
    stop at ``top`` entries, or go on to the end if it's None.
    """
    records = [
        r for r in logs.requests if (not start or r.time >= start) and (not end or r.time < end)
    ]
    total = len(records)
    is_mine = {ip: _in_networks(ip, my_networks) for ip in {r.ip for r in records}}
    mine = [r for r in records if is_mine[r.ip]]
    others = [r for r in records if not is_mine[r.ip]]
    you = ", ".join(sorted(_label(network) for network in my_networks)) or "unknown"
    errors = Counter(f"{r.status}  {r.path}" for r in others if int(r.status) >= 400)
    oldest = min((r.time for r in logs.requests), default=None)

    def pct(n: int) -> str:
        """``n`` as a percentage of all requests."""
        return f"{100 * n / total:.1f}%" if total else "0.0%"

    shown = f"top {top}" if top else "all"
    rotated = f" (+{len(logs.names) - 1} rotated)" if len(logs.names) > 1 else ""
    unparsed = f" ({logs.unparsed:,} unparsed)" if logs.unparsed else ""
    out = [
        f"\nPythonAnywhere access log — {domain}",
        f"Sources: {logs.names[0]}{rotated}",
        f"Lines parsed: {len(logs.requests):,}{unparsed}",
    ]
    if records:
        times = [r.time for r in records]
        out.append(f"Time range: {min(times):%Y-%m-%d %H:%M} → {max(times):%Y-%m-%d %H:%M} UTC")
    if start and oldest and oldest > start:
        out.append(
            f"Note: the logs only go back to {oldest:%Y-%m-%d %H:%M}, not to"
            f" {start:%Y-%m-%d %H:%M} UTC."
        )
    out += [
        f"\nTotal requests: {total:,}",
        f"  You ({you}): {len(mine):,}  ({pct(len(mine))})",
        f"  Others:           {len(others):,}  ({pct(len(others))})",
        f"Unique visitor IPs (excluding you): {len({r.ip for r in others}):,}",
        f"\nOthers — status codes:\n{_top(Counter(r.status for r in others), top)}",
        f"\nOthers — {shown} errors:\n{_top(errors, top)}",
        f"\nOthers — {shown} paths:\n{_top(Counter(r.path for r in others), top)}",
        f"\nOthers — {shown} IPs:\n{_top(Counter(r.ip for r in others), top)}",
    ]
    if detail:
        heading = f"most recent {top} requests" if top else "all requests, newest first"
        out.append(f"\nOthers — {heading}:")
        recent = sorted(others, key=lambda r: r.time, reverse=True)[:top]
        width = max((len(r.ip) for r in recent), default=0)
        for r in recent:
            out.append(f"  {r.time:{LOG_TIME}}  {r.ip:<{width}}  {r.status}  {r.method} {r.path}")
    return "\n".join(out)


def _in_networks(ip: str, networks: set[Network]) -> bool:
    """Whether ``ip`` is in one of ``networks``; False if it isn't an IP address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _label(network: Network) -> str:
    """``network`` as written: a lone address without its /32 or /128."""
    if network.prefixlen == network.max_prefixlen:
        return str(network.network_address)
    return str(network)


def _top(counter: Counter[str], n: int | None) -> str:
    """The ``n`` most common entries (all if None), one per line, after right-aligned counts."""
    if not counter:
        return "  (none)"
    width = len(f"{counter.most_common(1)[0][1]:,}")
    return "\n".join(f"  {count:>{width},}  {key}" for key, count in counter.most_common(n))
