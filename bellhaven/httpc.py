"""Minimal HTTP client on the standard library, with retries.

No third-party dependencies anywhere in this project: `python run.py ...` works on
a clean machine with nothing but Python 3.9+.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "bellhaven-crm-sync/1.0 (+sales-ops exercise)"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.url = url
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 30,
    retries: int = 3,
    backoff: float = 1.5,
) -> tuple[int, str]:
    data = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"

    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            # 4xx (except 429) are deterministic: do not burn retries on them.
            if e.code < 500 and e.code != 429:
                raise HttpError(e.code, url, text) from None
            last = HttpError(e.code, url, text)
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # transient
            last = e
        if attempt < retries - 1:
            time.sleep(backoff ** attempt)
    raise last if last else RuntimeError("unreachable")


def get_text(url: str, **kw) -> str:
    return request("GET", url, **kw)[1]


def get_json(url: str, **kw) -> Any:
    return json.loads(request("GET", url, **kw)[1])
