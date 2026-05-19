from __future__ import annotations

import json
import logging
from typing import Optional

from .base import BaseResource, _BASE_STRIP_FIELDS
from ..id_map import IDMap

logger = logging.getLogger(__name__)

_GQL_CREATE_GROUP = """
mutation sellingPlanGroupCreate($input: SellingPlanGroupInput!, $resources: SellingPlanGroupResourceInput) {
  sellingPlanGroupCreate(input: $input, resources: $resources) {
    sellingPlanGroup {
      id
      name
    }
    userErrors { field message }
  }
}
"""

_GQL_ADD_PRODUCTS = """
mutation sellingPlanGroupAddProducts($id: ID!, $productIds: [ID!]!) {
  sellingPlanGroupAddProducts(id: $id, productIds: $productIds) {
    sellingPlanGroup { id }
    userErrors { field message }
  }
}
"""

_GQL_GET_GROUPS = """
query getSellingPlanGroups($cursor: String) {
  sellingPlanGroups(first: 50, after: $cursor) {
    edges {
      node {
        id
        name
        merchantCode
        options
        position
        sellingPlans(first: 50) {
          edges {
            node {
              id
              name
              options
              position
              billingPolicy { ... on SellingPlanRecurringBillingPolicy { interval intervalCount } }
              deliveryPolicy { ... on SellingPlanRecurringDeliveryPolicy { interval intervalCount } }
              pricingPolicies {
                ... on SellingPlanFixedPricingPolicy { adjustmentType adjustmentValue { ... on SellingPlanPricingPolicyPercentageValue { percentage } ... on MoneyV2 { amount currencyCode } } }
                ... on SellingPlanRecurringPricingPolicy { adjustmentType adjustmentValue { ... on SellingPlanPricingPolicyPercentageValue { percentage } ... on MoneyV2 { amount currencyCode } } }
              }
            }
          }
        }
        products(first: 250) { edges { node { id } } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class SellingPlansResource(BaseResource):
    """Migrates selling plan groups via GraphQL. Requires products to be loaded first."""

    resource_name = "selling_plans"
    endpoint = ""  # Not used — GraphQL only
    resource_key = ""
    list_key = ""

    def __init__(self, *args, products_id_map: IDMap, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._products_id_map = products_id_map

    async def _fetch_all(self) -> list[dict]:
        all_groups: list[dict] = []
        cursor = None

        while True:
            data = await self.source.graphql(
                _GQL_GET_GROUPS,
                variables={"cursor": cursor},
                estimated_cost=150,
            )
            edges = data.get("sellingPlanGroups", {}).get("edges", [])
            page_info = data.get("sellingPlanGroups", {}).get("pageInfo", {})

            for edge in edges:
                node = edge["node"]
                # Flatten nested structures
                node["sellingPlans"] = [
                    sp["node"] for sp in node.get("sellingPlans", {}).get("edges", [])
                ]
                node["_product_ids"] = [
                    p["node"]["id"].split("/")[-1]
                    for p in node.get("products", {}).get("edges", [])
                ]
                all_groups.append(node)

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return all_groups

    def transform(self, item: dict) -> dict:
        return item  # Handled inline in _create

    async def _create(self, payload: dict) -> str:
        group = payload

        # Build the SellingPlanGroupInput
        selling_plans_input = []
        for plan in group.get("sellingPlans", []):
            billing = plan.get("billingPolicy", {})
            delivery = plan.get("deliveryPolicy", {})
            pricing = []
            for p in plan.get("pricingPolicies", []):
                adj_val = p.get("adjustmentValue", {})
                if "percentage" in adj_val:
                    pricing.append({
                        "recurring": {
                            "adjustmentType": p.get("adjustmentType"),
                            "adjustmentValue": {"percentage": adj_val["percentage"]},
                        }
                    })
                elif "amount" in adj_val:
                    pricing.append({
                        "fixed": {
                            "adjustmentType": p.get("adjustmentType"),
                            "adjustmentValue": {"fixedValue": adj_val["amount"]},
                        }
                    })

            plan_input = {
                "name": plan.get("name"),
                "options": plan.get("options", []),
                "position": plan.get("position"),
                "billingPolicy": {
                    "recurring": {
                        "interval": billing.get("interval"),
                        "intervalCount": billing.get("intervalCount"),
                    }
                },
                "deliveryPolicy": {
                    "recurring": {
                        "interval": delivery.get("interval"),
                        "intervalCount": delivery.get("intervalCount"),
                    }
                },
                "pricingPolicies": pricing,
            }
            selling_plans_input.append(plan_input)

        group_input = {
            "name": group.get("name"),
            "merchantCode": group.get("merchantCode"),
            "options": group.get("options", []),
            "position": group.get("position"),
            "sellingPlansToCreate": selling_plans_input,
        }

        data = await self.dest.graphql(
            _GQL_CREATE_GROUP,
            variables={"input": group_input},
            estimated_cost=200,
        )

        result = data.get("sellingPlanGroupCreate", {})
        errors = result.get("userErrors", [])
        if errors:
            raise RuntimeError(f"sellingPlanGroupCreate errors: {errors}")

        dest_gid = result["sellingPlanGroup"]["id"]
        dest_id = dest_gid.split("/")[-1]

        # Associate products
        src_product_ids = self._get_source_product_ids(group)
        dest_product_gids = []
        for src_id in src_product_ids:
            dest_id_str = self._products_id_map.get(src_id)
            if dest_id_str:
                dest_product_gids.append(f"gid://shopify/Product/{dest_id_str}")

        if dest_product_gids:
            assoc_data = await self.dest.graphql(
                _GQL_ADD_PRODUCTS,
                variables={"id": dest_gid, "productIds": dest_product_gids},
                estimated_cost=100,
            )
            assoc_errors = assoc_data.get("sellingPlanGroupAddProducts", {}).get("userErrors", [])
            if assoc_errors:
                logger.warning(
                    f"[load] selling_plans: product association errors for "
                    f"group {dest_id}: {assoc_errors}"
                )

        return dest_id

    def _get_source_product_ids(self, group: dict) -> list[str]:
        """Extract source product IDs associated with this group from its GID."""
        # If stored during extract with _product_ids field
        return [str(pid) for pid in group.get("_product_ids", [])]

    async def find_existing(self, item: dict) -> Optional[dict]:
        return None  # GraphQL groups — skip idempotency for now
