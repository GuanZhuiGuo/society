#!/usr/bin/env python3
"""Check HTTP(S) links used by monthly archive files without changing them."""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LINK_RE = re.compile(r"\[[^\]\n]+\]\((https?://[^\s)]+)\)")
USER_AGENT = (
    "Mozilla/5.0 (compatible; ChinaSocietyArchiveLinkCheck/1.0; "
    "+https://github.com/GuanZhuiGuo/society)"
)
PERMANENT_FAILURES = {404, 410, 451}
TRANSIENT_OR_BLOCKED = {401, 403, 406, 408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class LinkResult:
    url: str
    status: int | None
    outcome: str
    method: str
    locations: tuple[str, ...]
    detail: str = ""


def collect_links(root: Path) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    archive_root = root / "archive"
    if not archive_root.exists():
        return links
    for path in sorted(archive_root.glob("[0-9][0-9][0-9][0-9]/*.md")):
        relative = path.relative_to(root).as_posix()
        for url in LINK_RE.findall(path.read_text(encoding="utf-8")):
            links.setdefault(url, []).append(relative)
    return links


def request_url(url: str, method: str, timeout: float) -> int:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"}
    if method == "GET":
        headers["Range"] = "bytes=0-2047"
    request = Request(url, headers=headers, method=method)
    with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        response.read(1)
        return int(response.status)


def classify_status(status: int) -> str:
    if 200 <= status < 400:
        return "ok"
    if status in PERMANENT_FAILURES:
        return "dead"
    if status in TRANSIENT_OR_BLOCKED:
        return "blocked_or_transient"
    return "http_error"


def check_one(url: str, locations: list[str], timeout: float) -> LinkResult:
    last_error = ""
    for method in ("HEAD", "GET"):
        try:
            status = request_url(url, method, timeout)
            return LinkResult(
                url, status, classify_status(status), method, tuple(locations)
            )
        except HTTPError as error:
            status = int(error.code)
            if method == "HEAD" and status in {400, 403, 405, 406, 501}:
                last_error = f"HEAD returned {status}; retried with GET"
                continue
            return LinkResult(
                url,
                status,
                classify_status(status),
                method,
                tuple(locations),
                last_error or str(error.reason),
            )
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError) as error:
            reason = getattr(error, "reason", error)
            last_error = str(reason)
            if method == "HEAD":
                continue
            return LinkResult(
                url, None, "network_error", method, tuple(locations), last_error
            )
    return LinkResult(url, None, "network_error", "GET", tuple(locations), last_error)


def render_text(results: list[LinkResult]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    lines = [
        "Link check summary: "
        + ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
    ]
    for result in results:
        status = "-" if result.status is None else str(result.status)
        lines.append(
            f"[{result.outcome}] {status} {result.url} "
            f"({', '.join(result.locations)})"
            + (f" — {result.detail}" if result.detail else "")
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--delay", type=float, default=0.15)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--fail-on-network-error",
        action="store_true",
        help="Treat timeouts, TLS failures, and DNS errors as a failing result.",
    )
    args = parser.parse_args()

    links = collect_links(args.root.resolve())
    results: list[LinkResult] = []
    for index, (url, locations) in enumerate(sorted(links.items())):
        if index and args.delay:
            time.sleep(args.delay)
        results.append(check_one(url, locations, args.timeout))

    if args.format == "json":
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        print(render_text(results))

    failing = {"dead", "http_error"}
    if args.fail_on_network_error:
        failing.add("network_error")
    return 1 if any(item.outcome in failing for item in results) else 0


if __name__ == "__main__":
    sys.exit(main())
