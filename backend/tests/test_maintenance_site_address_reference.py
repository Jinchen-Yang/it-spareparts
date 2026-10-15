"""D-29 stable-part address references never guess identity or write facts."""

from types import SimpleNamespace as Obj

import pytest

from app.services.maintenance_site_address_reference import (
    ADDRESS_MISSING, DEMAND_NOT_FOUND, MULTIPLE_ADDRESSES, AddressReferenceIndex,
)


def demand(part=1, *, raw="raw-a", no="WBDD-20261012-0001", project="p1",
           address="地址 A", active=True, status="已生效", pn="SAME-PN"):
    line = Obj(part_id=part, pn_std=pn, raw_line_id=f"line-{raw}", is_active=active)
    order = Obj(raw_order_id=raw, order_no=no, receiver_address=address, data_status=status)
    return line, order, project


def site(part=1, *, raw=None, no=None, line_id=None, pn="SAME-PN"):
    return Obj(part_id=part, source_order_id=raw, demand_order_no=no,
               source_line_id=line_id, pn=pn)


def issue(*, project="p1", no="LYD-1", source="workbook"):
    return Obj(project_id=project, issue_no=no, source=source, site_location="实际领用地点")


def test_unique_address_is_trimmed_but_preserves_full_text():
    value = "=地址正文\n" + "地址" * 200
    idx = AddressReferenceIndex.from_rows([demand(address=f"  {value}  ")])
    header = issue()
    line = site(pn="旧别名")
    before = vars(header).copy(), vars(line).copy()
    assert idx.resolve(line, header) == value
    assert (vars(header), vars(line)) == before


def test_repeated_same_address_is_one_distinct_address():
    idx = AddressReferenceIndex.from_rows([
        demand(address=" 地址 A "), demand(address="地址 A"),
        demand(raw="raw-b", no="WBDD-20261012-0002", address="地址 A"),
    ])
    assert idx.resolve(site(), issue()) == "地址 A"


def test_multiple_addresses_are_explicit_and_never_select_the_latest():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address="地址 B"),
    ])
    assert idx.resolve(site(), issue()) == MULTIPLE_ADDRESSES
    assert idx.resolve(site(raw="raw-a"), issue()) == "地址 A"
    assert idx.resolve(site(no="WBDD-20261012-0002"), issue()) == "地址 B"


@pytest.mark.parametrize("address", [None, "", " \n\t "])
def test_matching_demand_with_blank_address_is_not_missing_demand(address):
    idx = AddressReferenceIndex.from_rows([demand(address=address)])
    assert idx.resolve(site(), issue()) == ADDRESS_MISSING


def test_one_known_address_and_blank_rows_reference_only_known_address():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address=None),
    ])
    assert idx.resolve(site(), issue()) == "地址 A"
    assert idx.resolve(site(raw="raw-b"), issue()) == ADDRESS_MISSING


@pytest.mark.parametrize("line", [site(raw="does-not-exist"), site(no="WBDD-20261012-9999")])
def test_wrong_explicit_reference_does_not_fall_back_to_same_part(line):
    idx = AddressReferenceIndex.from_rows([demand()])
    assert idx.resolve(line, issue()) == DEMAND_NOT_FOUND


def test_explicit_order_without_the_part_does_not_fall_back_elsewhere():
    idx = AddressReferenceIndex.from_rows([
        demand(part=2), demand(part=1, raw="raw-b", no="WBDD-20261012-0002", address="地址 B"),
    ])
    assert idx.resolve(site(part=1, raw="raw-a"), issue()) == DEMAND_NOT_FOUND


def test_conflicting_raw_order_and_human_number_are_not_guessed():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address="地址 B"),
    ])
    assert idx.resolve(site(raw="raw-a", no="WBDD-20261012-0002"), issue()) == DEMAND_NOT_FOUND
    assert idx.resolve(site(raw="raw-a", no="WBDD-20261012-0001"), issue()) == "地址 A"


def test_duplicate_human_number_requires_stable_raw_id():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", address="地址 B"),
    ])
    assert idx.resolve(site(no="WBDD-20261012-0001"), issue()) == DEMAND_NOT_FOUND
    assert idx.resolve(site(raw="raw-a", no="WBDD-20261012-0001"), issue()) == "地址 A"


def test_same_pn_text_with_different_part_identity_does_not_match():
    idx = AddressReferenceIndex.from_rows([demand(part=1, pn="SAME-PN")])
    assert idx.resolve(site(part=2, pn="SAME-PN"), issue()) == DEMAND_NOT_FOUND


def test_project_scope_is_part_of_every_lookup_key():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-other", project="p2", address="其他项目地址"),
    ])
    assert idx.resolve(site(), issue(project="p1")) == "地址 A"
    assert idx.resolve(site(raw="raw-other"), issue(project="p1")) == DEMAND_NOT_FOUND
    assert idx.resolve(site(), issue(project="p3")) == DEMAND_NOT_FOUND


def test_wbdd_shaped_issue_number_is_verified_as_legacy_reference():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address="地址 B"),
    ])
    assert idx.resolve(site(), issue(no="WBDD-20261012-0001")) == "地址 A"
    assert idx.resolve(site(), issue(no="WBDD-20261012-9999")) == DEMAND_NOT_FOUND
    assert idx.resolve(site(raw="raw-b"), issue(no="WBDD-20261012-0001")) == "地址 B"


def test_warehouse_ckd_source_line_id_cannot_bind_another_wbdd_line():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address="地址 B"),
    ])
    # CKD's native line id happens to equal a WBDD id. The verified head wins.
    assert idx.resolve(site(raw="raw-a", line_id="line-raw-b"), issue(source="site_issue_v2")) == "地址 A"
    assert idx.resolve(site(line_id="line-raw-b"), issue(source="site_issue_v2")) == MULTIPLE_ADDRESSES


def test_explicit_inactive_or_non_effective_rows_do_not_contribute_addresses():
    idx = AddressReferenceIndex.from_rows([
        demand(), demand(raw="raw-b", no="WBDD-20261012-0002", address="失效地址", active=False),
        demand(raw="raw-c", no="WBDD-20261012-0003", address="取消地址", status="已取消"),
    ])
    assert idx.resolve(site(), issue()) == "地址 A"
    assert idx.resolve(site(raw="raw-b"), issue()) == DEMAND_NOT_FOUND
    assert idx.resolve(site(raw="raw-c"), issue()) == DEMAND_NOT_FOUND


def test_empty_index_and_missing_identity_return_no_match():
    idx = AddressReferenceIndex.from_rows([])
    assert idx.resolve(site(), issue()) == DEMAND_NOT_FOUND
    idx = AddressReferenceIndex.from_rows([demand()])
    assert idx.resolve(site(part=None), issue()) == DEMAND_NOT_FOUND
    assert idx.resolve(site(), issue(project=None)) == DEMAND_NOT_FOUND
