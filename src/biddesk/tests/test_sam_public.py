"""Tests for the unkeyed sam.gov routes in :mod:`biddesk.sam_client`. No network call anywhere.

The fixtures below are verbatim excerpts of two responses cached on 2026-09-13:

* ``PUB_BODY`` / ``HIT``: notice ``e5081111d5e74d3ebae79926a2bc21be`` (VA, Natchez National
  Cemetery Tree Pruning), from ``data/raw/pub_817dda2b72fb3aef.json`` and the sgs search hit
  shape returned by ``https://sam.gov/api/prod/sgs/v1/search/``.
* ``AWARD_PUB_BODY``: notice ``2badd9a87fc54cf0ba4b37830ed8f2f7`` (GSA Multiple Award Schedule),
  from ``data/raw/pub_031f54d4d1fef385.json`` - the shape where the set-aside sits at
  ``data2.setAside`` and reads ``"N/A"``.
* ``RESOURCES_BODY``: the five attachments of the first notice, from
  ``https://sam.gov/api/prod/opps/v3/opportunities/<id>/resources``.

Only fields the tests assert on are kept; nothing is invented.
"""
from __future__ import annotations

import datetime as dt

import pytest

from biddesk import sam_client
from biddesk.sam_client import (RESOURCE_DOWNLOAD_URL, _public_naics, _public_set_aside,
                                in_window, normalize_public, search_public)

PUB_BODY = {
    "data2": {
        "type": "o",
        "award": {},
        "naics": [{"code": ["561730"], "type": "primary"}],
        "title": "Natchez National Cemetery Tree Pruning Cascading Evaluation",
        "archive": {"date": "2026-10-07", "type": "auto15"},
        "solicitation": {"setAside": "SBA",
                         "deadlines": {"response": "2026-09-22T10:00:00-04:00",
                                       "responseTz": "America/New_York"}},
        "organizationId": "100173491",
        "placeOfPerformance": {"zip": "39120",
                               "city": {"code": "50440", "name": "Natchez"},
                               "state": {"code": "MS", "name": "Mississippi"},
                               "country": {"code": "USA", "name": "UNITED STATES"}},
        "solicitationNumber": "36C78626Q50259",
    },
    "parent": {},
    "status": {"code": "published", "value": "Published"},
    "archived": False,
    "postedDate": "2026-09-11T18:59:12.552+00:00",
    "modifiedDate": "2026-09-11T18:59:12.557+00:00",
    "description": [{"opportunityId": "e5081111d5e74d3ebae79926a2bc21be",
                     "body": "<p>Please review the attached document&nbsp;</p>\n\n"
                             "<p><u>Vendor Questions:</u></p>\n\n"
                             "<p>All vendor questions regarding this solicitation shall be submitted "
                             "by email to the david.hester@va.gov no later than 9/16/2026.</p>\n"}],
    "opportunityId": "e5081111d5e74d3ebae79926a2bc21be",
    "id": "e5081111d5e74d3ebae79926a2bc21be",
}

HIT = {
    "_id": "e5081111d5e74d3ebae79926a2bc21be",
    "_type": "opportunity",
    "publishDate": "2026-09-11T18:59:12+00:00",
    "isActive": True,
    "title": "Natchez National Cemetery Tree Pruning Cascading Evaluation",
    "type": {"code": "o", "value": "Solicitation"},
    "solicitationNumber": "36C78626Q50259",
    "responseDate": "2026-09-22T14:00:00+00:00",
    "responseDateActual": "2026-09-22T10:00:00-04:00",
    "modifiedDate": "2026-09-11T18:59:12+00:00",
    "organizationHierarchy": [
        {"level": 1, "name": "VETERANS AFFAIRS, DEPARTMENT OF", "type": "DEPARTMENT",
         "address": {"city": None, "state": None, "zip": None, "country": "US"}},
        {"level": 3, "name": "NATIONAL CEMETERY ADMIN (36C786)", "type": "OFFICE",
         "address": {"city": "QUANTICO", "state": "VA", "zip": "221346050", "country": "USA"}},
    ],
    "modifications": {"count": 0},
}

AWARD_PUB_BODY = {
    "data2": {
        "type": "a",
        "award": {"date": "2026-09-09", "amount": "18566456", "number": "47QTCA26D009F"},
        "naics": [{"code": ["541519"], "type": "primary"}],
        "title": "Multiple Award Schedule",
        "archive": {"date": "2031-09-08", "type": "autocustom"},
        "setAside": "N/A",
        "solicitation": {"deadlines": {}},
        "placeOfPerformance": None,
        "solicitationNumber": "47QSMD20R0001",
    },
    "postedDate": "2026-09-09T11:40:42.537+00:00",
    "description": [{"body": ""}],
    "opportunityId": "2badd9a87fc54cf0ba4b37830ed8f2f7",
    "id": "2badd9a87fc54cf0ba4b37830ed8f2f7",
}

AWARD_HIT = {
    "_id": "2badd9a87fc54cf0ba4b37830ed8f2f7",
    "publishDate": "2026-09-09T11:40:42+00:00",
    "isActive": True,
    "type": {"code": "a", "value": "Award Notice"},
    "organizationHierarchy": [],
}

RESOURCES_BODY = {"_embedded": {"opportunityAttachmentList": [{
    "opportunityId": "e5081111d5e74d3ebae79926a2bc21be",
    "attachments": [
        {"resourceId": "c2f2b7e80103490aa92c3e4a324e51f4", "attachmentOrder": 9, "fileExists": "1",
         "name": "QSE_36C78626Q50259.pdf", "type": "file", "accessLevel": "public",
         "deletedFlag": "0", "mimeType": ".pdf", "accessStatus": "public"},
        {"resourceId": "3d43026da238458ca40703928d1b7c11", "attachmentOrder": 8, "fileExists": "1",
         "name": "Tree Maint Map_FY26_Natchez.pdf", "type": "file", "accessLevel": "public",
         "deletedFlag": "0", "mimeType": ".pdf", "accessStatus": "public"},
        {"resourceId": "01cb7aa95bff4c1b94f9e4db547c7a1c", "attachmentOrder": 7, "fileExists": "1",
         "name": "Tree1.pdf", "type": "file", "accessLevel": "public",
         "deletedFlag": "0", "mimeType": ".pdf", "accessStatus": "public"},
        # a deleted row and a missing-file row: SAM keeps both in the list, neither is downloadable
        {"resourceId": "deadbeefdeadbeefdeadbeefdeadbeef", "attachmentOrder": 6, "fileExists": "1",
         "name": "Withdrawn.pdf", "type": "file", "accessLevel": "public",
         "deletedFlag": "1", "mimeType": ".pdf", "accessStatus": "public"},
        {"resourceId": "0000000000000000000000000000ffff", "attachmentOrder": 4, "fileExists": "0",
         "name": "Gone.pdf", "type": "file", "accessLevel": "public",
         "deletedFlag": "0", "mimeType": ".pdf", "accessStatus": "public"},
    ]}]}}


# ---------------------------------------------------------------- normalize_public

def test_normalize_public_maps_the_solicitation():
    n = normalize_public(PUB_BODY, HIT, "2026-09-13T20:07:23-05:00",
                         resource_links=["https://example.invalid/1"])
    assert n.notice_id == "e5081111d5e74d3ebae79926a2bc21be"
    assert n.title == "Natchez National Cemetery Tree Pruning Cascading Evaluation"
    assert n.solicitation_number == "36C78626Q50259"
    # the human-readable type comes from the hit; data2.type is the raw code "o"
    assert n.type == "Solicitation"
    assert n.base_type == "Solicitation"
    assert n.naics == "561730" and n.naics_all == ["561730"]
    assert n.set_aside_code == "SBA"
    assert n.set_aside_desc == "Small Business Set Aside - Total"
    assert n.posted == "2026-09-11"
    assert n.response_deadline == "2026-09-22T10:00:00-04:00"
    assert n.archive_date == "2026-10-07"
    assert n.pop_city == "Natchez" and n.pop_state == "MS"
    assert n.pop_zip == "39120" and n.pop_country == "USA"
    assert n.agency_path == "VETERANS AFFAIRS, DEPARTMENT OF.NATIONAL CEMETERY ADMIN (36C786)"
    assert n.office_city == "QUANTICO" and n.office_state == "VA"
    assert n.ui_link == "https://sam.gov/opp/e5081111d5e74d3ebae79926a2bc21be/view"
    assert n.active is True
    assert n.award is None
    assert n.resource_links == ["https://example.invalid/1"]
    # the keyed noticedesc URL was never fetched for a public-route notice: it stays unset
    assert n.description_url is None
    assert n.fetched_at == "2026-09-13T20:07:23-05:00"


def test_normalize_public_description_is_plain_text():
    n = normalize_public(PUB_BODY, HIT, "2026-09-13T20:07:23-05:00")
    assert "<p>" not in n.description_text
    assert n.description_text.startswith("Please review the attached document")
    assert "no later than 9/16/2026." in n.description_text
    assert n.resource_links == []


def test_normalize_public_award_notice_has_no_set_aside_and_no_place():
    """`N/A` at data2.setAside is unrestricted, not a set-aside the firm must be able to claim."""
    n = normalize_public(AWARD_PUB_BODY, AWARD_HIT, "2026-09-13T20:07:36-05:00")
    assert n.type == "Award Notice"
    assert n.set_aside_code is None and n.set_aside_desc is None
    assert n.pop_city is None and n.pop_state is None and n.pop_country is None
    assert n.award and n.award["number"] == "47QTCA26D009F"
    assert n.description_text is None        # the body is empty; nothing is invented
    assert n.agency_path is None


def test_public_naics_flattens_the_code_list():
    primary, all_codes = _public_naics({"naics": [
        {"code": ["561720", "561790"], "type": "primary"},
        {"code": ["561730"], "type": "secondary"}]})
    assert primary == "561720"
    assert all_codes == ["561720", "561790", "561730"]


def test_public_set_aside_prefers_the_solicitation_field():
    assert _public_set_aside({"solicitation": {"setAside": "SDVOSBC"}, "setAside": "N/A"}) == "SDVOSBC"
    assert _public_set_aside({"setAside": "8A"}) == "8A"
    assert _public_set_aside({"setAside": "N/A"}) is None
    assert _public_set_aside({"solicitation": {}, "setAside": ""}) is None
    assert _public_set_aside({}) is None


def test_unknown_set_aside_code_has_no_invented_description():
    body = {**PUB_BODY, "data2": {**PUB_BODY["data2"],
                                  "solicitation": {**PUB_BODY["data2"]["solicitation"], "setAside": "ZZZ"}}}
    n = normalize_public(body, HIT, "2026-09-13T20:07:23-05:00")
    assert n.set_aside_code == "ZZZ"
    assert n.set_aside_desc is None


# ---------------------------------------------------------------- resources route

def test_fetch_resource_links_keeps_only_live_public_files(monkeypatch):
    monkeypatch.setattr(sam_client, "_get_public_json",
                        lambda url, params, kind, use_cache=True: {"body": RESOURCES_BODY})
    links = sam_client.fetch_resource_links("e5081111d5e74d3ebae79926a2bc21be")
    assert links == [RESOURCE_DOWNLOAD_URL.format(file_id=f) for f in
                     ["01cb7aa95bff4c1b94f9e4db547c7a1c",      # attachmentOrder 7
                      "3d43026da238458ca40703928d1b7c11",      # 8
                      "c2f2b7e80103490aa92c3e4a324e51f4"]]     # 9
    assert all(u.endswith("/download") for u in links)


# ---------------------------------------------------------------- client-side date cutoff

@pytest.mark.parametrize("publish_date, keep", [
    ("2026-09-14T02:00:38+00:00", True),
    ("2026-08-14T00:00:00+00:00", True),      # the cutoff day itself is inside the window
    ("2026-08-13T23:59:59+00:00", False),
    ("2025-12-04T12:00:00+00:00", False),
    ("", False),                              # a hit with no publishDate is not assumed recent
])
def test_in_window(publish_date, keep):
    assert in_window(publish_date, "2026-08-14") is keep


def test_search_public_filters_the_window_client_side(monkeypatch):
    """The sgs route rejects every date parameter, so search_public must page to exhaustion and
    drop out-of-window hits itself. Results are sorted by -modifiedDate, and modifiedDate is
    always >= publishDate, so an old notice can appear on page 0."""
    pages = [
        {"_embedded": {"results": [
            {"_id": "in-1", "publishDate": "2026-09-11T00:00:00+00:00"},
            {"_id": "old-1", "publishDate": "2026-02-13T00:00:00+00:00"},   # modified recently
        ]}, "page": {"size": 2, "totalElements": 4, "totalPages": 2, "number": 0}},
        {"_embedded": {"results": [
            {"_id": "in-2", "publishDate": "2026-08-20T00:00:00+00:00"},
            {"_id": "in-1", "publishDate": "2026-09-11T00:00:00+00:00"},    # duplicate across pages
        ]}, "page": {"size": 2, "totalElements": 4, "totalPages": 2, "number": 1}},
    ]
    seen_params = []

    def fake(url, params, kind, use_cache=True):
        seen_params.append(dict(params))
        return {"_fetched_at": "2026-09-13T22:10:00-05:00", "body": pages[params["page"]]}

    monkeypatch.setattr(sam_client, "_get_public_json", fake)
    hits, meta = search_public(["561720", "561730"], days=30, size=2,
                               posted_to=dt.date(2026, 9, 13))
    assert sorted(h["_id"] for h in hits) == ["in-1", "in-2"]
    assert meta["posted_from"] == "2026-08-14" and meta["posted_to"] == "2026-09-13"
    assert meta["totalActiveRecords"] == 4 and meta["pages_fetched"] == 2
    assert meta["kept_in_window"] == 2 and meta["is_active"] is True
    assert meta["fetched_at"] == "2026-09-13T22:10:00-05:00"
    assert [p["page"] for p in seen_params] == [0, 1]
    assert seen_params[0]["naics"] == "561720,561730"
    assert seen_params[0]["sort"] == "-modifiedDate" and seen_params[0]["is_active"] == "true"
    assert "notice_type" not in seen_params[0]      # the full stream, as the keyed pull had it


def test_search_public_never_sends_the_api_key(monkeypatch):
    seen = []
    monkeypatch.setattr(sam_client, "_get_public_json",
                        lambda url, params, kind, use_cache=True: (
                            seen.append((url, dict(params))),
                            {"_fetched_at": "x", "body": {"_embedded": {"results": []},
                                                          "page": {"totalPages": 1, "totalElements": 0}}})[1])
    search_public(["561720"], days=30, posted_to=dt.date(2026, 9, 13))
    url, params = seen[0]
    assert "api_key" not in params and "api_key" not in url
    assert url.startswith("https://sam.gov/api/prod/sgs/v1/search")
