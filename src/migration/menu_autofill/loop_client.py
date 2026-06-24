from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx

from ..rate_limiter import LeakyBucketRateLimiter

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout))


class _LoopHTTP:
    """Shared retry/limiter plumbing for the Loop Admin and Storefront clients."""

    def __init__(self, base_url: str, max_retries: int = 5) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(
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
            raise RuntimeError(f"{type(self).__name__} must be used as an async context manager")
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict,
        limiter: Optional[LeakyBucketRateLimiter] = None,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> dict:
        url = f"{self._base_url}/{path.lstrip('/')}"
        attempt = 0
        delay = 2.0
        while True:
            try:
                if limiter is not None:
                    await limiter.acquire()
                response = await self._client().request(
                    method, url, headers=headers, params=params, json=json
                )
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "2"))
                    backoff = min(retry_after * (2 ** attempt), 60.0)
                    logger.warning(
                        "[loop] 429 on %s %s, sleeping %.1fs (attempt %d)",
                        method, path, backoff, attempt + 1,
                    )
                    await asyncio.sleep(backoff)
                    attempt += 1
                    if attempt > self._max_retries:
                        response.raise_for_status()
                    continue
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
            except httpx.HTTPStatusError as exc:
                if not _is_retryable(exc) or attempt >= self._max_retries:
                    if 400 <= exc.response.status_code < 500:
                        logger.error(
                            "[loop] HTTP %d on %s %s: %s",
                            exc.response.status_code, method, path,
                            exc.response.text[:500],
                        )
                    raise
                attempt += 1
                logger.warning(
                    "[loop] HTTP %d on %s %s, retry %d/%d in %.1fs",
                    exc.response.status_code, method, path, attempt, self._max_retries, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                logger.warning(
                    "[loop] network error on %s %s: %s, retry %d/%d in %.1fs",
                    method, path, exc, attempt, self._max_retries, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)


class LoopAdminClient(_LoopHTTP):
    """Loop **Admin API** client (``/admin/{version}``).

    Authenticated with the account-wide ``X-Loop-Token`` header. Used to
    discover subscriptions with an upcoming anchor, read subscription contents,
    and mint per-customer storefront session tokens.

    The scheduled-orders endpoint is limited to **2 requests / 3 seconds**; all
    other endpoints share a 10 req/s pool. We keep two leaky buckets so a slow
    discovery scan never starves the faster reads.
    """

    def __init__(
        self,
        admin_token: str,
        *,
        base_url: str,
        api_version: str = "2023-10",
        max_retries: int = 5,
    ) -> None:
        super().__init__(f"{base_url.rstrip('/')}/admin/{api_version}", max_retries)
        self._token = admin_token
        self._schedule_limiter = LeakyBucketRateLimiter(bucket_size=2.0, refill_rate=2.0 / 3.0)
        self._general_limiter = LeakyBucketRateLimiter(bucket_size=10.0, refill_rate=10.0)

    @property
    def _headers(self) -> dict:
        return {"X-Loop-Token": self._token, "Content-Type": "application/json"}

    async def iter_scheduled_orders(
        self,
        *,
        billing_start_epoch: int,
        billing_end_epoch: int,
        status: str = "UNPROCESSED",
        page_size: int = 50,
    ) -> AsyncIterator[dict]:
        """Yield scheduled-order rows in the billing window, following pages.

        Paginates on ``pageInfo.hasNextPage`` using ``pageNo`` (1-based). Filters
        to ``status`` client-side as well, in case the API ignores the param.
        """
        page_no = 1
        while True:
            data = await self._request(
                "GET",
                "order/schedule/",
                headers=self._headers,
                limiter=self._schedule_limiter,
                params={
                    "billingDateStartEpoch": billing_start_epoch,
                    "billingDateEndEpoch": billing_end_epoch,
                    "status": status,
                    "pageNo": page_no,
                    "pageSize": min(page_size, 50),
                },
            )
            rows = data.get("data") or data.get("orders") or []
            for row in rows:
                if status and str(row.get("status", status)).upper() != status.upper():
                    continue
                yield row
            page_info = data.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            page_no += 1

    async def get_subscription(self, subscription_id: str | int) -> dict:
        """Read full subscription details (meal lines + bundleTransactionId)."""
        data = await self._request(
            "GET",
            f"subscription/{subscription_id}",
            headers=self._headers,
            limiter=self._general_limiter,
        )
        return data.get("data") or data

    async def mint_session_token(self, customer_shopify_id: str | int) -> str:
        """Mint a storefront **session token** for a customer (admin-initiated)."""
        data = await self._request(
            "POST",
            f"customer/{customer_shopify_id}/sessionToken",
            headers=self._headers,
            limiter=self._general_limiter,
        )
        payload = data.get("data") or data
        token = payload.get("sessionToken", "")
        if not token:
            raise RuntimeError(
                f"No sessionToken returned for customer {customer_shopify_id}: {data}"
            )
        return token


@dataclass
class _CustomerTokens:
    access_token: str
    refresh_token: str


class LoopStorefrontClient(_LoopHTTP):
    """Loop **Storefront Bundle API** client (``/storefront/{version}``).

    Per-customer ``Bearer`` access tokens are minted lazily via the admin
    session-token flow (Section 2.1 of the spec) and cached in-memory for the
    run. On a 401 the access token is rotated using the refresh token and the
    call is retried once.

    NOTE: the write path (``bundle/transaction/update``) is a Loop **beta** that
    must be enabled on the account before it can be exercised live.
    """

    def __init__(
        self,
        admin: LoopAdminClient,
        *,
        base_url: str,
        api_version: str = "2023-10",
        max_retries: int = 5,
    ) -> None:
        super().__init__(f"{base_url.rstrip('/')}/storefront/{api_version}", max_retries)
        self._admin = admin
        self._limiter = LeakyBucketRateLimiter(bucket_size=10.0, refill_rate=10.0)
        self._tokens: dict[str, _CustomerTokens] = {}

    @staticmethod
    def _bearer(token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _ensure_tokens(self, customer_shopify_id: str | int) -> _CustomerTokens:
        key = str(customer_shopify_id)
        cached = self._tokens.get(key)
        if cached:
            return cached
        session_token = await self._admin.mint_session_token(customer_shopify_id)
        data = await self._request(
            "POST",
            "auth/refreshToken",
            headers={"Content-Type": "application/json"},
            limiter=self._limiter,
            json={"sessionToken": session_token},
        )
        payload = data.get("data") or data
        tokens = _CustomerTokens(
            access_token=payload.get("accessToken", ""),
            refresh_token=payload.get("refreshToken", ""),
        )
        if not tokens.access_token:
            raise RuntimeError(f"No accessToken minted for customer {key}: {data}")
        self._tokens[key] = tokens
        return tokens

    async def _rotate(self, customer_shopify_id: str | int) -> _CustomerTokens:
        key = str(customer_shopify_id)
        current = self._tokens.get(key)
        if current is None or not current.refresh_token:
            # Nothing to rotate from; mint fresh.
            self._tokens.pop(key, None)
            return await self._ensure_tokens(customer_shopify_id)
        data = await self._request(
            "POST",
            "auth/rotateToken",
            headers={"Content-Type": "application/json"},
            limiter=self._limiter,
            json={"refreshToken": current.refresh_token},
        )
        payload = data.get("data") or data
        tokens = _CustomerTokens(
            access_token=payload.get("accessToken", current.access_token),
            refresh_token=payload.get("refreshToken", current.refresh_token),
        )
        self._tokens[key] = tokens
        return tokens

    async def _authed(
        self,
        customer_shopify_id: str | int,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
    ) -> dict:
        """Issue an authenticated call, rotating once on a 401."""
        tokens = await self._ensure_tokens(customer_shopify_id)
        try:
            return await self._request(
                method, path, headers=self._bearer(tokens.access_token),
                limiter=self._limiter, json=json,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 401:
                raise
            logger.info("[loop] access token expired for customer %s, rotating", customer_shopify_id)
            tokens = await self._rotate(customer_shopify_id)
            return await self._request(
                method, path, headers=self._bearer(tokens.access_token),
                limiter=self._limiter, json=json,
            )

    async def get_bundle_transaction(
        self, customer_shopify_id: str | int, transaction_id: str | int
    ) -> dict:
        """Authoritative current bundle: items, boxSizeId, discountId."""
        data = await self._authed(
            customer_shopify_id, "GET", f"bundle/transaction/{transaction_id}"
        )
        return data.get("data") or data

    async def get_bundle(self, customer_shopify_id: str | int, bundle_id: str | int) -> dict:
        """Bundle config: available box sizes, discounts and thresholds."""
        data = await self._authed(customer_shopify_id, "GET", f"bundle/{bundle_id}")
        return data.get("data") or data

    async def update_bundle(
        self,
        customer_shopify_id: str | int,
        *,
        transaction_id: str | int,
        items: list[dict],
        discount_id: Optional[str | int],
        box_size_id: Optional[str | int],
    ) -> dict:
        """Write the full replacement item list (beta endpoint)."""
        payload: dict[str, Any] = {"id": transaction_id, "items": items}
        if discount_id is not None:
            payload["discountId"] = discount_id
        if box_size_id is not None:
            payload["boxSizeId"] = box_size_id
        data = await self._authed(
            customer_shopify_id, "POST", "bundle/transaction/update", json=payload
        )
        return data.get("data") or data
