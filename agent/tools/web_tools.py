"""The web tools: fetching a page, and searching through Tavily.

Why this module exists
----------------------
``web_fetch`` / ``web_search`` / ``tavily_search`` are three registrations but
eight methods: the fetch path, the Tavily path, and the small helpers only they
use (HTML stripping, proxy selection, request construction, the API key).  They
were methods on ``BuiltinTools``, a 5428-line class, where the two pieces of
registry context they read -- ``web_proxy`` and ``tavily_api_key`` -- were
indistinguishable from the forty other tools' context.

Two decisions travelled with the code, because each is the reason it is shaped
the way it is:

- **The proxy is chosen here, not in the network boundary.**  ``proxy_for``
  reads ``web_proxy`` first and only then defers to the environment, because a
  security boundary must not change which address it dials on the strength of
  ambient configuration.  ``_WEB_PROXY_DISABLED`` are the words that mean
  "never proxy", overriding an environment that asks for one.
- **A body that fills the cap is reported as truncated even when the character
  limit would not have cut it.**  The network layer stops at
  ``WEB_FETCH_MAX_BYTES`` without signalling the cut, so ``fetch`` derives
  ``truncated`` from the byte count as well as from the character count.

The one bound the two search tools share now has one name:
``TAVILY_SEARCH_MAX_RESULTS`` is both what the schemas advertise and what the
handler clamps with.  The survey had recorded ``WEB_SEARCH_MAX_RESULTS`` and
``TAVILY_SEARCH_MAX_RESULTS`` as two names for one number -- agreeing today and
free to drift tomorrow.  The advertised-only name is gone.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import urllib.request
from typing import Any, Callable

from agent.security.network import fetch_public_http_url, parse_proxy_url

from .executor import report_tool_progress


WEB_FETCH_MAX_BYTES = 512 * 1024
WEB_FETCH_TIMEOUT = 20

#: ``web_proxy`` values that mean "never proxy", overriding the environment.
_WEB_PROXY_DISABLED = frozenset({"none", "off", "no", "direct", "disable", "disabled"})

TAVILY_SEARCH_MAX_RESULTS = 10
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
WEB_USER_AGENT = (
    "Mozilla/5.0 (compatible; PersonalAgent/1.0; +https://github.com/your/agent)"
)


class WebTools:
    """The web tools, with the registry and responders they need."""

    def __init__(
        self,
        registry: Any,
        ok: Callable[..., dict[str, Any]],
        error: Callable[..., dict[str, Any]],
    ) -> None:
        # Public on purpose: ``ToolRegistry.call`` reads ``fn.__self__.registry``
        # to install the calling registry's merged context on the registry this
        # tool implementation reads.  A cluster that hid it would silently stop
        # receiving a session's own write scope, workspace and output dir.
        self.registry = registry
        self._ok = ok
        self._error = error

    @staticmethod
    def strip_html(raw: str) -> str:
        """Very lightweight HTML → plain-text: remove tags, decode entities."""
        # Remove <script> and <style> blocks entirely
        raw = re.sub(
            r"<(script|style)[^>]*>.*?</(script|style)>",
            " ",
            raw,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove all remaining tags
        raw = re.sub(r"<[^>]+>", " ", raw)
        # Decode HTML entities (e.g. &amp; &lt; &#39;)
        raw = html.unescape(raw)
        # Collapse whitespace
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()

    def proxy_for(self, url: str) -> tuple[Any, bool]:
        """``(proxy, trust_env)`` for one fetch.

        ``web_proxy`` in config wins outright: a proxy URL names the proxy to
        use, and the words in ``_WEB_PROXY_DISABLED`` turn proxying off even
        when the environment asks for one.  With no config value the standard
        environment variables decide.

        The network boundary deliberately does not read the environment on its
        own — a security boundary must not change which address it dials
        because of ambient configuration — so the choice is made here, where an
        operator can also override it.
        """
        raw = self.registry.get_context("web_proxy", "")
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            if text.lower() in _WEB_PROXY_DISABLED:
                return None, False
            parsed = parse_proxy_url(text)
            if parsed is not None:
                return parsed, False
        return None, True

    def make_urllib_request(self, url: str, timeout: int = WEB_FETCH_TIMEOUT) -> bytes:
        """Fetch *url* through the validated network boundary.

        Proxied when config or the environment names a proxy.  A machine behind
        a fake-IP resolver needs this: every name answers with a reserved
        address, which the direct path correctly refuses, so without the proxy
        path nothing can be fetched at all.
        """
        proxy, trust_env = self.proxy_for(url)
        result = fetch_public_http_url(
            url,
            timeout=timeout,
            max_bytes=WEB_FETCH_MAX_BYTES,
            proxy=proxy,
            trust_env=trust_env,
            headers={
                "User-Agent": WEB_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            on_progress=lambda bytes_done, total: report_tool_progress(
                status="downloading",
                current=bytes_done,
                total=total,
                bytes_done=bytes_done,
                bytes_total=total,
            ),
        )
        return result.body

    @staticmethod
    def make_tavily_request(
        api_key: str,
        query: str,
        max_results: int,
        search_depth: str,
        include_answer: bool,
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": search_depth,
                "include_answer": include_answer,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TAVILY_SEARCH_URL,
            data=payload,
            method="POST",
            headers={
                "User-Agent": WEB_USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=WEB_FETCH_TIMEOUT) as resp:
            raw = resp.read(WEB_FETCH_MAX_BYTES)
        return json.loads(raw.decode("utf-8"))

    def resolve_tavily_api_key(self) -> str:
        raw = self.registry.get_context("tavily_api_key", "")
        if isinstance(raw, str) and raw.startswith("$"):
            return os.environ.get(raw[1:], "")
        if raw:
            return str(raw)
        return os.environ.get("TAVILY_API_KEY", "")

    async def fetch(
        self,
        url: str,
        max_chars: int = 8000,
        raw_html: bool = False,
    ) -> dict[str, Any]:
        """Fetch a single URL and return its text content."""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            return self._error("URL must start with http:// or https://", url=url)
        max_chars = max(100, min(int(max_chars), WEB_FETCH_MAX_BYTES))
        try:
            raw_bytes = await asyncio.to_thread(self.make_urllib_request, url)
            # Decode – UTF-8 with replacement (never raises)
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            # The network layer caps the body at WEB_FETCH_MAX_BYTES and does
            # not signal the cut, so a page that fills the cap must be reported
            # as truncated even when the char limit below would not have.
            truncated_network = len(raw_bytes) >= WEB_FETCH_MAX_BYTES

            if raw_html:
                body = raw_text[:max_chars]
                truncated = truncated_network or len(raw_text) > max_chars
            else:
                body = self.strip_html(raw_text)
                truncated = truncated_network or len(body) > max_chars
                body = body[:max_chars]

            return self._ok(
                url=url,
                content=body,
                truncated=truncated,
                chars=len(body),
            )
        except Exception as exc:
            return self._error(f"Fetch failed: {exc}", url=url)

    async def search(
        self,
        query: str,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Search the web through the Tavily backend under the generic tool name.

        There is no ``region`` parameter: it was a DuckDuckGo hint, Tavily has no
        equivalent, and advertising a knob that does nothing is how a model comes
        to believe it localised a search it did not.
        """
        response = await self.tavily_search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=False,
        )
        if response.get("ok"):
            response["backend"] = "tavily"
        return response

    async def tavily_search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        api_key = self.resolve_tavily_api_key()
        if not api_key:
            return self._error(
                "Tavily API key not configured. Set TAVILY_API_KEY or registry context 'tavily_api_key'.",
                query=query,
            )

        max_results = max(1, min(int(max_results), TAVILY_SEARCH_MAX_RESULTS))
        search_depth = str(search_depth).strip().lower() or "basic"
        if search_depth not in {"basic", "advanced"}:
            return self._error(
                "search_depth must be 'basic' or 'advanced'",
                query=query,
                search_depth=search_depth,
            )

        try:
            payload = await asyncio.to_thread(
                self.make_tavily_request,
                api_key,
                query.strip(),
                max_results,
                search_depth,
                include_answer,
            )
        except Exception as exc:
            return self._error(f"Tavily search failed: {exc}", query=query)

        items = []
        for result in payload.get("results", [])[:max_results]:
            items.append(
                {
                    "title": result.get("title", ""),
                    "url": result.get("url", ""),
                    "snippet": result.get("content", ""),
                    "score": result.get("score"),
                }
            )

        response = self._ok(
            query=query,
            count=len(items),
            results=items,
        )
        if payload.get("answer"):
            response["answer"] = payload["answer"]
        return response
