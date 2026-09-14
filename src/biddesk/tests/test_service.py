"""Service-layer tests. No network, no Bedrock, no model call.

Every test here exercises the payload router or a path that reads files already in
the repo (the snapshot bench files). The ``desk`` and ``refusal`` actions are not
called: they would run the Graph.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from biddesk import config, sam_client, service


def test_health_envelope():
    out = service.handle({"action": "health"})
    assert out["ok"] is True
    assert out["action"] == "health"
    assert out["produced_at"]
    assert out["ledger_scope"] in ("live", "package")
    assert set(service.ACTIONS) == set(out["actions"])


def test_router_rejects_unknown_action():
    out = service.handle({"action": "launch_missiles"})
    assert out["ok"] is False
    assert out["error"].startswith("ValueError: unknown action")
    assert "Traceback" not in json.dumps(out)


def test_router_rejects_missing_action():
    assert service.handle({})["error"].startswith("ValueError: payload needs an 'action'")
    assert service.handle([1, 2])["error"] == "ValueError: payload must be a JSON object"


def test_router_accepts_a_json_string_payload():
    out = service.handle(json.dumps({"action": "health"}))
    assert out["ok"] is True


def test_error_envelope_has_no_traceback_and_keeps_the_action():
    out = service.handle({"action": "ledger", "firm": "nope"})
    assert out == {
        "ok": False,
        "action": "ledger",
        "produced_at": out["produced_at"],
        "error": out["error"],
    }
    assert out["error"].startswith("ValueError: unknown firm 'nope'")


def test_bad_firm_type_is_an_error_not_a_crash():
    out = service.handle({"action": "week", "firm": 7})
    assert out["ok"] is False
    assert out["error"] == "ValueError: payload needs a 'firm' slug (string)"


def test_week_serves_the_committed_replay_and_never_invents_days(monkeypatch, tmp_path):
    out = service.handle({"action": "week", "firm": "red-cedar"})
    assert out["ok"] is True and out["available"] is True
    assert out["model_calls"] == 0 and out["invariants_ok"] is True
    assert sum(d["posted"] for d in out["days"]) == out["totals"]["posted"]
    monkeypatch.setattr(config, "DATA", tmp_path)          # no replay file: unavailable, no days
    out = service.handle({"action": "week", "firm": "red-cedar"})
    assert out["ok"] is True and out["available"] is False
    assert out["reason"] and "days" not in out


def _throttle(tmp_path, hours: int):
    until = dt.datetime.now(sam_client.CENTRAL) + dt.timedelta(hours=hours)
    path = tmp_path / "throttle.json"
    path.write_text(json.dumps({"until": until.isoformat(), "seen_at": "x", "message": "429"}))
    return path


def _public_fixture(monkeypatch):
    """A one-hit public route built from the committed raw cache, so no network is touched."""
    import glob
    search_files = sorted(glob.glob(str(config.RAW / "pubsearch_*.json")))
    notice_files = sorted(glob.glob(str(config.RAW / "pub_*.json")))
    if not search_files or not notice_files:
        pytest.skip("no cached public route files under data/raw")
    hits = [h for f in search_files
            for h in json.loads(Path(f).read_text(encoding="utf-8"))["body"]["_embedded"]["results"]]
    notices = {}
    for f in notice_files:
        env = json.loads(Path(f).read_text(encoding="utf-8"))
        nid = env["body"].get("opportunityId") or env["body"].get("id")
        if nid:
            notices[nid] = env
    hit = next((h for h in hits if h["_id"] in notices), None)
    if hit is None:
        pytest.skip("no cached public notice matches a cached search hit")
    calls = {"search": 0, "notice": 0}

    def _search_public(naics_list, days=30, **kw):
        calls["search"] += 1
        assert kw.get("use_cache") is False, "a live triage must not serve a cached search page"
        return [hit], {"naics": ",".join(naics_list), "kept_in_window": 1, "totalActiveRecords": 1,
                       "pages_fetched": 1, "posted_from": "x", "posted_to": "y", "url": "u"}

    def _fetch_notice_public(nid, use_cache=True, **kw):
        calls["notice"] += 1
        assert use_cache is False
        return notices[nid]

    monkeypatch.setattr(sam_client, "search_public", _search_public)
    monkeypatch.setattr(sam_client, "fetch_notice_public", _fetch_notice_public)
    return hit, calls


def test_triage_uses_the_public_route_while_the_key_is_throttled(tmp_path, monkeypatch):
    monkeypatch.setattr(sam_client, "THROTTLE_FILE", _throttle(tmp_path, 12))

    def _no_keyed(*a, **k):  # a keyed call here would be a test failure
        raise AssertionError("triage called the keyed API while throttled")

    monkeypatch.setattr(sam_client, "search", _no_keyed)
    hit, calls = _public_fixture(monkeypatch)
    from biddesk import tiering
    monkeypatch.setattr(tiering, "tier1", lambda profile, n, **k: tiering.Triage(n.notice_id, "pass", 2, None, "test: no model", ""))
    out = service.handle({"action": "triage", "firm": "red-cedar"})
    assert out["ok"] is True
    assert out["cached"] is False
    assert out["route"] == "public"
    assert "unkeyed" in out["reason"]
    assert out["as_of"] and out["produced_at"]
    assert out["counts"]["total"] == 1
    assert calls == {"search": 1, "notice": 1}
    assert out["windows"][0]["is_active_only"] is True
    assert len(out["candidates"]) == out["counts"]["tier2_sent"]


def test_triage_falls_to_public_when_a_keyed_pull_throttles_mid_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(sam_client, "THROTTLE_FILE", tmp_path / "absent.json")

    def _throttled(*a, **k):
        raise sam_client.SamThrottled("throttled", None)

    monkeypatch.setattr(sam_client, "search", _throttled)
    _public_fixture(monkeypatch)
    from biddesk import tiering
    monkeypatch.setattr(tiering, "tier1", lambda profile, n, **k: tiering.Triage(n.notice_id, "pass", 2, None, "test: no model", ""))
    out = service.handle({"action": "triage", "firm": "red-cedar"})
    assert out["ok"] is True and out["cached"] is False and out["route"] == "public"


def test_triage_serves_the_cached_snapshot_only_when_both_routes_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(sam_client, "THROTTLE_FILE", _throttle(tmp_path, 12))
    monkeypatch.setattr(sam_client, "search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("keyed")))

    def _down(*a, **k):
        raise sam_client.SamError("SAM.gov HTTP 503 on pubsearch")

    monkeypatch.setattr(sam_client, "search_public", _down)
    out = service.handle({"action": "triage", "firm": "red-cedar"})
    assert out["ok"] is True
    assert out["cached"] is True
    assert out["reason"].startswith("SAM.gov unavailable on both routes")
    assert out["as_of"]
    assert out["counts"]["total"] > 0
    assert out["by_rule"]
    assert all("notice_id" in c for c in out["candidates"])
    assert len(out["candidates"]) == out["counts"]["tier2_sent"]


def test_triage_without_a_bench_file_is_an_error_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(sam_client, "THROTTLE_FILE", _throttle(tmp_path, 12))
    monkeypatch.setattr(sam_client, "search_public",
                        lambda *a, **k: (_ for _ in ()).throw(sam_client.SamError("down")))
    monkeypatch.setattr(service, "_bench_path", lambda slug: tmp_path / "missing.json")
    out = service.handle({"action": "triage", "firm": "red-cedar"})
    assert out["ok"] is False
    assert out["error"].startswith("FileNotFoundError: no cached triage")


def test_ledger_scope_skips_the_linux_mount_on_a_dev_box(tmp_path):
    mount = tmp_path / "mnt" / "data" / "ledger"
    package = tmp_path / "pkg" / "ledger"
    scope, directory = service.ledger_location(mount_dir=mount, package_dir=package,
                                               allow_mount=False)
    assert scope == "package"
    assert directory == package


def test_ledger_scope_is_live_when_the_mount_is_writable(tmp_path):
    mount = tmp_path / "mnt" / "data" / "ledger"
    package = tmp_path / "pkg" / "ledger"
    scope, directory = service.ledger_location(mount_dir=mount, package_dir=package,
                                               allow_mount=True)
    assert scope == "live"
    assert directory == mount


def test_ledger_scope_falls_back_to_the_package_when_the_mount_is_absent(tmp_path, monkeypatch):
    package = tmp_path / "pkg" / "ledger"
    monkeypatch.setattr(service, "_writable", lambda d: d == package)
    scope, directory = service.ledger_location(mount_dir=tmp_path / "no" / "such", package_dir=package,
                                               allow_mount=True)
    assert scope == "package"
    assert directory == package


def test_ledger_scope_uses_a_temp_dir_when_nothing_is_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_writable", lambda d: False)
    scope, directory = service.ledger_location(mount_dir=tmp_path / "a", package_dir=tmp_path / "b",
                                               allow_mount=True)
    assert scope == "package"
    assert directory.exists()


def test_ledger_answer_and_undo_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "ledger_location", lambda **k: ("package", tmp_path))
    answered = service.handle({"action": "answer_card", "firm": "red-cedar",
                               "notice_id": "abc123", "answer": "no_bid",
                               "reason": "distance"})
    assert answered["ok"] is True
    assert answered["answer"] == "no-bid"
    assert answered["ledger_scope"] == "package"
    assert answered["row"]["action"] == "card_answered"
    ts = answered["row"]["ts"]

    listed = service.handle({"action": "ledger", "firm": "red-cedar", "limit": 5})
    assert listed["ok"] is True
    assert listed["rows"][-1]["ts"] == ts

    undone = service.handle({"action": "undo", "firm": "red-cedar", "ts": ts,
                             "reason": "mis-tap"})
    assert undone["ok"] is True
    assert undone["row"]["action"] == "undone"
    assert ts in service.handle({"action": "ledger", "firm": "red-cedar"})["undone_ts"]

    again = service.handle({"action": "undo", "firm": "red-cedar", "ts": ts, "reason": "twice"})
    assert again["ok"] is False
    assert again["error"].startswith("ValueError: ledger row at ts")


def test_answer_card_rejects_a_free_text_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "ledger_location", lambda **k: ("package", tmp_path))
    out = service.handle({"action": "answer_card", "firm": "red-cedar",
                          "notice_id": "abc123", "answer": "maybe"})
    assert out["ok"] is False
    assert out["error"] == "ValueError: 'answer' must be 'bid' or 'no-bid'"


def test_ledger_limit_must_be_a_positive_int(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "ledger_location", lambda **k: ("package", tmp_path))
    out = service.handle({"action": "ledger", "firm": "red-cedar", "limit": 0})
    assert out["error"] == "ValueError: 'limit' must be a positive integer"


@pytest.mark.parametrize("action", sorted(service.HANDLERS))
def test_every_documented_action_is_routed(action):
    assert action in service.ACTIONS
    assert callable(service.HANDLERS[action])


def test_account_ids_are_redacted_from_the_error_envelope():
    msg = "User: arn:aws:iam::123456789012:user/x is not authorized"
    assert service.redact(msg) == "User: arn:aws:iam::<account>:user/x is not authorized"
    out = service._err("desk", RuntimeError(msg))
    assert "123456789012" not in json.dumps(out)
    assert out["error"].startswith("RuntimeError: ")
