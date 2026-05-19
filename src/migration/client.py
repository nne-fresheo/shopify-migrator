from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator, Optional

import httpx

from .rate_limiter import GraphQLCostRateLimiter, LeakyBucketRateLimiter

logger = logging.getLogger(__name__)

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout))


class ShopifyClient:
    """
    Single entry point for all Shopify Admin API calls.
    Handles authentication, rate limiting, retries, and pagination.

    Authentication modes:
    - Static token: pass ``access_token`` only.
    - Client credentials: pass ``client_id`` + ``client_secret`` (and optionally
      ``access_token`` as a fallback). A 24-hour token is fetched automatically
      on ``__aenter__``.
    """

    def __init__(
        self,
        shop_domain: str,
        access_token: str = "",
        api_version: str = "2024-01",
        rest_bucket_size: float = 40.0,
        rest_refill_rate: float = 2.0,
        graphql_max_cost: float = 1000.0,
        graphql_restore_rate: float = 50.0,
        graphql_cost_threshold: float = 200.0,
        max_retries: int = 5,
        client_id: str = "",
        client_secret: str = "",
    ) -> None:
        self._shop = shop_domain
        self._static_token = access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._api_version = api_version
        self._max_retries = max_retries
        self._base_url = f"https://{shop_domain}/admin/api/{api_version}"
        self._rest_limiter = LeakyBucketRateLimiter(rest_bucket_size, rest_refill_rate)
        self._graphql_limiter = GraphQLCostRateLimiter(
            graphql_max_cost, graphql_restore_rate, graphql_cost_threshold
        )
        self._http: Optional[httpx.AsyncClient] = None

    async def _exchange_token(self) -> str:
        """Exchange client_id + client_secret for a short-lived access token."""
        url = f"https://{self._shop}/admin/oauth/access_token"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as tmp:
            response = await tmp.post(
                url,
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
            response.raise_for_status()
            body = response.json()
            token = body.get("access_token", "")
            if not token:
                raise RuntimeError(
                    f"No access_token in response from {url}: {response.text}"
                )
            granted_scope = body.get("scope", "<not returned>")
            logger.info(f"Token granted for {self._shop} — scopes: {granted_scope}")
            return token

    async def __aenter__(self) -> "ShopifyClient":
        if self._client_id and self._client_secret:
            token = await self._exchange_token()
            logger.debug("Obtained short-lived access token via client credentials")
        elif self._static_token:
            token = self._static_token
        else:
            raise RuntimeError(
                "ShopifyClient requires either access_token or client_id + client_secret"
            )

        self._http = httpx.AsyncClient(
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("ShopifyClient must be used as an async context manager")
        return self._http

    async def _rest_request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        url = f"{self._base_url}/{path.lstrip('/')}"
        attempt = 0
        delay = 2.0

        while True:
            try:
                await self._rest_limiter.acquire()
                response = await self._client().request(method, url, **kwargs)

                # Sync rate limiter from response header
                call_limit = response.headers.get("X-Shopify-Shop-Api-Call-Limit")
                if call_limit:
                    self._rest_limiter.sync_from_header(call_limit)

                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "2"))
                    backoff = min(retry_after * (2 ** attempt), 60.0)
                    logger.warning(f"429 on {method} {path}, sleeping {backoff:.1f}s (attempt {attempt + 1})")
                    await asyncio.sleep(backoff)
                    attempt += 1
                    if attempt > self._max_retries:
                        response.raise_for_status()
                    continue

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as exc:
                if not _is_retryable(exc) or attempt >= self._max_retries:
                    if 400 <= exc.response.status_code < 500:
                        try:
                            body = exc.response.json()
                            logger.error(
                                f"HTTP {exc.response.status_code} on {method} {path}: {body}"
                            )
                        except Exception:
                            logger.error(
                                f"HTTP {exc.response.status_code} on {method} {path}: "
                                f"{exc.response.text[:500]}"
                            )
                    raise
                attempt += 1
                logger.warning(
                    f"HTTP {exc.response.status_code} on {method} {path}, "
                    f"retry {attempt}/{self._max_retries} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                logger.warning(
                    f"Network error on {method} {path}: {exc}, "
                    f"retry {attempt}/{self._max_retries} in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def get(self, path: str, params: dict | None = None) -> dict:
        response = await self._rest_request("GET", path, params=params)
        return response.json()

    async def get_paginated(
        self,
        path: str,
        resource_key: str,
        params: dict | None = None,
    ) -> AsyncIterator[list]:
        """Yield one page (list) at a time, following cursor-based pagination."""
        first_params = {**(params or {}), "limit": 250}
        response = await self._rest_request("GET", path, params=first_params)
        data = response.json()
        yield data.get(resource_key, [])

        while True:
            link_header = response.headers.get("Link", "")
            match = _LINK_NEXT_RE.search(link_header)
            if not match:
                break
            next_url = match.group(1)
            pi_match = re.search(r"page_info=([^&]+)", next_url)
            if not pi_match:
                break
            page_info = pi_match.group(1)
            response = await self._rest_request(
                "GET", path, params={"limit": 250, "page_info": page_info}
            )
            data = response.json()
            yield data.get(resource_key, [])

    async def post(self, path: str, payload: dict) -> dict:
        response = await self._rest_request("POST", path, json=payload)
        return response.json()

    async def put(self, path: str, payload: dict) -> dict:
        response = await self._rest_request("PUT", path, json=payload)
        return response.json()

    async def delete(self, path: str) -> None:
        await self._rest_request("DELETE", path)

    async def graphql(
        self,
        query: str,
        variables: dict | None = None,
        estimated_cost: float = 100.0,
    ) -> dict:
        """Execute a GraphQL query. Returns the `data` dict; raises on errors."""
        await self._graphql_limiter.acquire(estimated_cost)
        url = f"{self._base_url}/graphql.json"
        attempt = 0
        delay = 2.0

        while True:
            try:
                response = await self._client().post(
                    url,
                    json={"query": query, "variables": variables or {}},
                )
                response.raise_for_status()
                body = response.json()

                # Update GraphQL cost tracker
                throttle = body.get("extensions", {}).get("cost", {}).get("throttleStatus", {})
                if throttle:
                    self._graphql_limiter.update(throttle)

                if body.get("errors"):
                    raise RuntimeError(f"GraphQL errors: {body['errors']}")

                return body.get("data", {})

            except httpx.HTTPStatusError as exc:
                if not _is_retryable(exc) or attempt >= self._max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
