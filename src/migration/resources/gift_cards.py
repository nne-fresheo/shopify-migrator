from __future__ import annotations

import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS

logger = logging.getLogger(__name__)

_GIFT_CARD_STRIP = _BASE_STRIP_FIELDS | {
    "balance",         # Read-only; cannot restore partial redemptions
    "last_characters", # Derived from code
    "currency",        # Set by store
    "disabled_at",
    "line_item_id",
    "order_id",
    "customer",
    "template_suffix",
}


class GiftCardsResource(BaseResource):
    """
    Migrates gift cards.
    NOTE: balance cannot be restored — only initial_value is set.
    Partially redeemed cards will be created with full initial_value.
    """

    resource_name = "gift_cards"
    endpoint = "gift_cards.json"
    resource_key = "gift_card"
    list_key = "gift_cards"

    def transform(self, item: dict) -> dict:
        payload = {k: v for k, v in item.items() if k not in _GIFT_CARD_STRIP}

        # Log warning if balance differs from initial_value (partial redemption)
        balance = item.get("balance")
        initial = item.get("initial_value")
        if balance is not None and initial is not None and str(balance) != str(initial):
            logger.warning(
                f"[load] gift_cards: card '{item.get('id')}' has balance={balance} "
                f"!= initial_value={initial}. Will be created with full initial_value."
            )

        return payload

    async def find_existing(self, item: dict) -> Optional[dict]:
        # No reliable way to look up by code without knowing the full code
        # Shopify only returns last_characters; skip idempotency check
        return None
