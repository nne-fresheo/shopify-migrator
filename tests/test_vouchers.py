from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import click
import pytest
from openpyxl import Workbook

from migration.vouchers import (
    VoucherGenerator,
    VoucherUpdater,
    generate_code,
    read_emails,
    read_report,
    write_report,
)


# ── read_emails ────────────────────────────────────────────────────────────────


def _make_xlsx(path: Path, header: list, rows: list[list]) -> None:
    wb = Workbook()
    sheet = wb.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    wb.save(path)


def test_read_emails_basic(tmp_path: Path):
    xlsx = tmp_path / "emails.xlsx"
    _make_xlsx(xlsx, ["email"], [["a@x.com"], ["b@x.com"]])
    assert read_emails(xlsx) == ["a@x.com", "b@x.com"]


def test_read_emails_normalizes_and_dedupes(tmp_path: Path):
    xlsx = tmp_path / "emails.xlsx"
    _make_xlsx(
        xlsx,
        ["email"],
        [["  A@X.com "], ["b@x.com"], ["a@x.com"], [None], [""]],
    )
    assert read_emails(xlsx) == ["a@x.com", "b@x.com"]


def test_read_emails_case_insensitive_header(tmp_path: Path):
    xlsx = tmp_path / "emails.xlsx"
    _make_xlsx(xlsx, ["Name", "  Email "], [["Bob", "bob@x.com"]])
    assert read_emails(xlsx) == ["bob@x.com"]


def test_read_emails_custom_column(tmp_path: Path):
    xlsx = tmp_path / "emails.xlsx"
    _make_xlsx(xlsx, ["mail"], [["c@x.com"]])
    assert read_emails(xlsx, column="mail") == ["c@x.com"]


def test_read_emails_missing_column_raises(tmp_path: Path):
    xlsx = tmp_path / "emails.xlsx"
    _make_xlsx(xlsx, ["name"], [["nobody"]])
    with pytest.raises(click.ClickException, match="no 'email' column"):
        read_emails(xlsx)


# ── generate_code ────────────────────────────────────────────────────────────────


def test_generate_code_shape():
    code = generate_code("FRESHEO")
    assert code.startswith("FRESHEO-")
    suffix = code.split("-", 1)[1]
    assert len(suffix) == 8
    assert generate_code("FRESHEO") != generate_code("FRESHEO")


# ── VoucherGenerator helpers ─────────────────────────────────────────────────────


def _gen(mock_dest_client, **kwargs) -> VoucherGenerator:
    return VoucherGenerator(
        dest_client=mock_dest_client,
        amount=kwargs.pop("amount", 10.0),
        days=kwargs.pop("days", 30),
        prefix=kwargs.pop("prefix", "FRESHEO"),
        applies_one_time=kwargs.pop("applies_one_time", True),
        applies_subscription=kwargs.pop("applies_subscription", True),
        recurring_cycles=kwargs.pop("recurring_cycles", 1),
        dry_run=kwargs.pop("dry_run", False),
    )


def _customer_get(customers):
    async def _get(path, params=None):
        if path == "customers/search.json":
            return {"customers": customers}
        raise AssertionError(f"unexpected GET {path}")

    return _get


def _empty_find():
    return {"codeDiscountNodes": {"edges": []}}


def _find_with(title, code, node_id="gid://shopify/DiscountCodeNode/999"):
    return {
        "codeDiscountNodes": {
            "edges": [
                {
                    "node": {
                        "id": node_id,
                        "codeDiscount": {
                            "__typename": "DiscountCodeBasic",
                            "title": title,
                            "codes": {"edges": [{"node": {"code": code}}]},
                        },
                    }
                }
            ]
        }
    }


def _graphql(find_result, create_node_id="gid://shopify/DiscountCodeNode/777",
             create_errors=None, recorder=None, allow_create=True):
    async def _handler(query, variables=None, estimated_cost=100.0):
        if "codeDiscountNodes" in query:
            return find_result
        if "discountCodeBasicCreate" in query:
            if not allow_create:
                raise AssertionError("create mutation should not run")
            if recorder is not None:
                recorder.append(variables)
            return {
                "discountCodeBasicCreate": {
                    "codeDiscountNode": {"id": create_node_id},
                    "userErrors": create_errors or [],
                }
            }
        raise AssertionError(f"unexpected graphql query: {query[:40]}")

    return _handler


# ── VoucherGenerator: creation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creates_customer_when_missing(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([])
    recorded: list = []
    mock_dest_client.graphql.side_effect = _graphql(_empty_find(), recorder=recorded)

    posts: list = []

    async def _post(path, payload):
        posts.append(path)
        if path == "customers.json":
            return {"customer": {"id": 555, "email": payload["customer"]["email"]}}
        raise AssertionError(f"unexpected POST {path}")

    mock_dest_client.post.side_effect = _post

    gen = _gen(mock_dest_client, amount=10.0, days=30)
    row = await gen.create_voucher("new@x.com")

    assert posts == ["customers.json"]  # created the missing customer
    assert len(recorded) == 1
    d = recorded[0]["basicCodeDiscount"]

    assert d["customerSelection"]["customers"]["add"] == ["gid://shopify/Customer/555"]
    assert d["customerGets"]["value"]["discountAmount"] == {
        "amount": "10.00",
        "appliesOnEachItem": False,
    }
    assert d["customerGets"]["items"] == {"all": True}
    assert d["customerGets"]["appliesOnOneTimePurchase"] is True
    assert d["customerGets"]["appliesOnSubscription"] is True
    assert d["recurringCycleLimit"] == 1
    assert d["appliesOncePerCustomer"] is True
    assert d["code"].startswith("FRESHEO-")

    starts = datetime.fromisoformat(d["startsAt"])
    ends = datetime.fromisoformat(d["endsAt"])
    assert (ends - starts).days == 30

    assert row["status"] == "created"
    assert row["customer_id"] == "555"
    assert row["discount_id"] == "gid://shopify/DiscountCodeNode/777"
    assert row["code"].startswith("FRESHEO-")


@pytest.mark.asyncio
async def test_reuses_existing_customer(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([{"id": 42, "email": "known@x.com"}])
    recorded: list = []
    mock_dest_client.graphql.side_effect = _graphql(_empty_find(), recorder=recorded)

    async def _post(path, payload):
        raise AssertionError(f"should not POST, got {path}")

    mock_dest_client.post.side_effect = _post

    gen = _gen(mock_dest_client)
    row = await gen.create_voucher("known@x.com")

    assert row["status"] == "created"
    assert row["customer_id"] == "42"
    assert recorded[0]["basicCodeDiscount"]["customerSelection"]["customers"]["add"] == [
        "gid://shopify/Customer/42"
    ]


@pytest.mark.asyncio
async def test_idempotent_when_discount_exists(mock_dest_client):
    gen = _gen(mock_dest_client)
    title = gen._title("dup@x.com")
    mock_dest_client.graphql.side_effect = _graphql(
        _find_with(title, "FRESHEO-EXISTING"), allow_create=False
    )

    async def _post(path, payload):
        raise AssertionError(f"should not POST, got {path}")

    mock_dest_client.post.side_effect = _post

    row = await gen.create_voucher("dup@x.com")

    assert row["status"] == "exists"
    assert row["discount_id"] == "gid://shopify/DiscountCodeNode/999"
    assert row["code"] == "FRESHEO-EXISTING"


# ── VoucherGenerator: purchase type ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_only(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([{"id": 1, "email": "s@x.com"}])
    recorded: list = []
    mock_dest_client.graphql.side_effect = _graphql(_empty_find(), recorder=recorded)

    gen = _gen(mock_dest_client, applies_one_time=False, applies_subscription=True,
               recurring_cycles=0)
    await gen.create_voucher("s@x.com")

    cg = recorded[0]["basicCodeDiscount"]["customerGets"]
    assert cg["appliesOnOneTimePurchase"] is False
    assert cg["appliesOnSubscription"] is True
    assert recorded[0]["basicCodeDiscount"]["recurringCycleLimit"] == 0


@pytest.mark.asyncio
async def test_one_time_only_omits_recurring_limit(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([{"id": 1, "email": "o@x.com"}])
    recorded: list = []
    mock_dest_client.graphql.side_effect = _graphql(_empty_find(), recorder=recorded)

    gen = _gen(mock_dest_client, applies_one_time=True, applies_subscription=False)
    await gen.create_voucher("o@x.com")

    d = recorded[0]["basicCodeDiscount"]
    assert d["customerGets"]["appliesOnOneTimePurchase"] is True
    assert d["customerGets"]["appliesOnSubscription"] is False
    assert "recurringCycleLimit" not in d


# ── VoucherGenerator: dry-run, errors, isolation ─────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_makes_no_writes(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([])
    mock_dest_client.graphql.side_effect = _graphql(_empty_find(), allow_create=False)

    async def _post(path, payload):
        raise AssertionError(f"dry-run must not POST, got {path}")

    mock_dest_client.post.side_effect = _post

    gen = _gen(mock_dest_client, dry_run=True)
    row = await gen.create_voucher("preview@x.com")

    assert row["status"] == "would-create"
    assert row["code"].startswith("FRESHEO-")


@pytest.mark.asyncio
async def test_user_errors_mark_failed(mock_dest_client):
    mock_dest_client.get.side_effect = _customer_get([{"id": 1, "email": "e@x.com"}])
    mock_dest_client.graphql.side_effect = _graphql(
        _empty_find(), create_errors=[{"field": ["code"], "message": "Code already exists"}]
    )

    gen = _gen(mock_dest_client)
    row = await gen.create_voucher("e@x.com")

    assert row["status"] == "failed"
    assert "Code already exists" in row["error"]


@pytest.mark.asyncio
async def test_failure_is_isolated(mock_dest_client):
    async def _get(path, params=None):
        if path == "customers/search.json":
            if params and "boom@x.com" in params["query"]:
                raise RuntimeError("API down")
            return {"customers": [{"id": 7, "email": "ok@x.com"}]}
        raise AssertionError(f"unexpected GET {path}")

    mock_dest_client.get.side_effect = _get
    mock_dest_client.graphql.side_effect = _graphql(_empty_find())

    gen = _gen(mock_dest_client)
    rows = await gen.run(["boom@x.com", "ok@x.com"])

    statuses = {r["email"]: r["status"] for r in rows}
    assert statuses["boom@x.com"] == "failed"
    assert rows[0]["error"] == "API down"
    assert statuses["ok@x.com"] == "created"  # batch continued past the failure


# ── write_report ─────────────────────────────────────────────────────────────────


def test_write_report(tmp_path: Path):
    rows = [
        {
            "email": "a@x.com",
            "status": "created",
            "code": "FRESHEO-ABCD1234",
            "customer_id": "1",
            "discount_id": "gid://shopify/DiscountCodeNode/2",
            "error": "",
        }
    ]
    out = tmp_path / "vouchers.csv"
    write_report(rows, out)

    with out.open(newline="", encoding="utf-8") as fh:
        read_back = list(csv.DictReader(fh))
    assert read_back[0]["email"] == "a@x.com"
    assert read_back[0]["code"] == "FRESHEO-ABCD1234"
    assert read_back[0]["discount_id"] == "gid://shopify/DiscountCodeNode/2"


# ── read_report ──────────────────────────────────────────────────────────────────


def test_read_report_roundtrip(tmp_path: Path):
    # Old REST-format report (price_rule_id column) should still read fine.
    src = tmp_path / "vouchers.csv"
    src.write_text(
        "email,status,code,customer_id,price_rule_id,error\n"
        "a@x.com,created,FRESHEO-AAA,1,111,\n"
        "b@x.com,created,FRESHEO-BBB,2,222,\n",
        encoding="utf-8",
    )
    rows = read_report(src)
    assert [r["code"] for r in rows] == ["FRESHEO-AAA", "FRESHEO-BBB"]
    assert rows[0]["email"] == "a@x.com"


# ── VoucherUpdater: build_input (partial) ────────────────────────────────────────


def test_build_input_only_includes_provided_fields():
    upd = VoucherUpdater(None, amount=15.0)
    payload = upd.build_input()
    assert payload == {
        "customerGets": {
            "value": {"discountAmount": {"amount": "15.00", "appliesOnEachItem": False}}
        }
    }


def test_build_input_purchase_type_and_cycles():
    upd = VoucherUpdater(None, purchase_type="both", recurring_cycles=0)
    payload = upd.build_input()
    assert payload["customerGets"]["appliesOnOneTimePurchase"] is True
    assert payload["customerGets"]["appliesOnSubscription"] is True
    assert payload["recurringCycleLimit"] == 0
    assert "value" not in payload["customerGets"]  # amount untouched


def test_build_input_expiry_usage_and_once_per_customer():
    upd = VoucherUpdater(None, days=14, usage_limit=5, once_per_customer=False)
    payload = upd.build_input()
    assert payload["usageLimit"] == 5
    assert payload["appliesOncePerCustomer"] is False
    assert "endsAt" in payload
    assert "customerGets" not in payload


def test_has_changes():
    assert VoucherUpdater(None).has_changes() is False
    assert VoucherUpdater(None, amount=1.0).has_changes() is True


# ── VoucherUpdater: run ──────────────────────────────────────────────────────────


def _update_graphql(node_for_code, update_errors=None, recorder=None, allow_update=True):
    """node_for_code: dict code -> node id (or None for not-found)."""

    async def _handler(query, variables=None, estimated_cost=100.0):
        if "codeDiscountNodeByCode" in query:
            node_id = node_for_code.get(variables["code"])
            return {"codeDiscountNodeByCode": ({"id": node_id} if node_id else None)}
        if "discountCodeBasicUpdate" in query:
            if not allow_update:
                raise AssertionError("update mutation should not run")
            if recorder is not None:
                recorder.append(variables)
            return {
                "discountCodeBasicUpdate": {
                    "codeDiscountNode": {"id": variables["id"]},
                    "userErrors": update_errors or [],
                }
            }
        raise AssertionError(f"unexpected graphql query: {query[:40]}")

    return _handler


@pytest.mark.asyncio
async def test_update_resolves_by_code_and_applies(mock_dest_client):
    recorded: list = []
    mock_dest_client.graphql.side_effect = _update_graphql(
        {"FRESHEO-AAA": "gid://shopify/DiscountCodeNode/111"}, recorder=recorded
    )

    upd = VoucherUpdater(mock_dest_client, purchase_type="both", recurring_cycles=1)
    results = await upd.run([{"email": "a@x.com", "code": "FRESHEO-AAA"}])

    assert results[0]["status"] == "updated"
    assert results[0]["discount_id"] == "gid://shopify/DiscountCodeNode/111"
    sent = recorded[0]
    assert sent["id"] == "gid://shopify/DiscountCodeNode/111"
    assert sent["basicCodeDiscount"]["customerGets"]["appliesOnSubscription"] is True
    assert sent["basicCodeDiscount"]["recurringCycleLimit"] == 1


@pytest.mark.asyncio
async def test_update_not_found(mock_dest_client):
    mock_dest_client.graphql.side_effect = _update_graphql({}, allow_update=False)
    upd = VoucherUpdater(mock_dest_client, amount=10.0)
    results = await upd.run([{"email": "x@x.com", "code": "FRESHEO-MISSING"}])
    assert results[0]["status"] == "not-found"


@pytest.mark.asyncio
async def test_update_dry_run(mock_dest_client):
    mock_dest_client.graphql.side_effect = _update_graphql(
        {"FRESHEO-AAA": "gid://shopify/DiscountCodeNode/111"}, allow_update=False
    )
    upd = VoucherUpdater(mock_dest_client, amount=10.0, dry_run=True)
    results = await upd.run([{"email": "a@x.com", "code": "FRESHEO-AAA"}])
    assert results[0]["status"] == "would-update"


@pytest.mark.asyncio
async def test_update_user_errors_fail(mock_dest_client):
    mock_dest_client.graphql.side_effect = _update_graphql(
        {"FRESHEO-AAA": "gid://shopify/DiscountCodeNode/111"},
        update_errors=[{"field": ["endsAt"], "message": "is invalid"}],
    )
    upd = VoucherUpdater(mock_dest_client, days=30)
    results = await upd.run([{"email": "a@x.com", "code": "FRESHEO-AAA"}])
    assert results[0]["status"] == "failed"
    assert "is invalid" in results[0]["error"]


@pytest.mark.asyncio
async def test_update_isolation_and_blank_code(mock_dest_client):
    mock_dest_client.graphql.side_effect = _update_graphql(
        {"FRESHEO-OK": "gid://shopify/DiscountCodeNode/9"}
    )
    upd = VoucherUpdater(mock_dest_client, amount=10.0)
    results = await upd.run(
        [
            {"email": "blank@x.com", "code": ""},
            {"email": "ok@x.com", "code": "FRESHEO-OK"},
        ]
    )
    statuses = {r["email"]: r["status"] for r in results}
    assert statuses["blank@x.com"] == "skipped"
    assert statuses["ok@x.com"] == "updated"
