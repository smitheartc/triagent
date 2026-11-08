"""Scorer rules from CLAUDE.md, no network."""
import scorer

KEY = {
    "scenario": "bad_config_change",
    "root_cause": {"category": "change.configuration", "entity": "lb-1", "entity_type": "load_balancer",
                   "change_id": "CHG0006441", "commit": "fc76b47999cab1d1c2e23d9cc16244a57d525f5d"},
    "causal_chain": [{"event_id": "a"}, {"event_id": "b"}, {"event_id": "c"}, {"what": "emergent, no id"}],
}
HERRINGS = [{"type": "unrelated_deploy", "event_id": "rh-1"}, {"type": "unrelated_change", "entity": "CHG0005780"}]


def result(**rc):
    report = {"root_cause": {"entity": "lb-1", "category": "config", "identifiers": {}, "event_ids": ["a"], **rc},
              "causal_chain": [{"event_id": "a"}, {"event_id": "b"}, {"event_id": "x"}], "confidence": 0.8}
    return {"status": "done", "report": report, "totals": {"tool_calls": 12, "prompt_tokens": 100,
                                                           "completion_tokens": 20}, "seconds": 3.0}


def test_category_mapping():
    m = scorer.expected_categories
    assert m({"category": "facility.cooling"}) == {"cooling"}
    assert m({"category": "network.hardware"}) == {"network"}
    assert m({"category": "software.release"}) == {"memory", "deploy"}
    assert m({"category": "change.configuration", "entity_type": "certificate"}) == {"certificate"}
    assert m({"category": "change.configuration", "entity_type": "service"}) == {"config"}
    assert m({"category": "security.intrusion"}) == {"security"}


def test_pass_and_jaccard():
    r = scorer.score(KEY, HERRINGS, result())
    assert r["entity_ok"] and r["category_ok"] and r["passed"] and not r["red_herring"]
    assert r["jaccard"] == 0.5   # {a,b} of {a,b,c,x}
    assert r["tokens"] == 120 and r["tool_calls"] == 12


def test_entity_matches_by_commit_prefix_or_change_id():
    assert scorer.score(KEY, HERRINGS, result(entity="haproxy", identifiers={"commit": "fc76b479"}))["entity_ok"]
    assert scorer.score(KEY, HERRINGS, result(entity="x", identifiers={"ids": ["CHG0006441"]}))["entity_ok"]
    assert not scorer.score(KEY, HERRINGS, result(entity="lb-2", identifiers={"commit": "fc76"}))["entity_ok"]


def test_red_herring_is_hard_fail():
    by_id = scorer.score(KEY, HERRINGS, result(event_ids=["a", "rh-1"]))
    by_entity = scorer.score(KEY, HERRINGS, result(identifiers={"change_id": "chg0005780"}))
    assert by_id["red_herring"] and not by_id["passed"]
    assert by_entity["red_herring"] and not by_entity["passed"]


def test_wrong_category_and_failed_run():
    assert not scorer.score(KEY, HERRINGS, result(category="deploy"))["passed"]
    failed = scorer.score(KEY, HERRINGS, {"status": "failed", "error": "boom", "seconds": 1.0})
    assert failed["passed"] is False


def test_noise_ticket_wants_unknown_low_confidence():
    noise = {"scenario": "none", "root_cause": None, "causal_chain": []}
    ok = result(category="unknown")
    ok["report"]["confidence"] = 0.2
    assert scorer.score(noise, [], ok)["passed"]
    assert not scorer.score(noise, [], result())["passed"]


def test_before_after_scores_both_reports():
    res = result()
    res["report_initial"] = result(entity="lb-2", category="network")["report"]
    res["totals_initial"] = {"tool_calls": 20, "prompt_tokens": 50, "completion_tokens": 5}
    res["critic"] = {"first": {"verdict": "fail", "failed": ["category_matches"]}, "final": {"verdict": "pass"},
                     "retried": True}
    r = scorer.score_before_after(KEY, HERRINGS, res)
    assert r["before"]["passed"] is False and r["passed"] is True
    assert r["before"]["tokens"] == 55 and r["tokens"] == 120
    assert r["critic_first"] == "fail" and r["retried"] and r["critic_failed"] == ["category_matches"]
    md = scorer.markdown([dict(r, run=1)], {"stamp": "s", "models": {}, "answer_key": "k", "sim_seed": 7,
                                            "runs": 1, "seed": 1})
    assert "## Before and after the critic" in md and "fail -> pass (retried, kept retry)" in md


def test_markdown_has_contract_columns():
    rows = [dict(scorer.score_before_after(KEY, HERRINGS, result()), run=1)]
    md = scorer.markdown(rows, {"stamp": "s", "models": {}, "answer_key": "k", "sim_seed": 7, "runs": 1, "seed": 1})
    assert ("| scenario | run | cause entity | category | chain overlap | red herring cited | confidence "
            "| tool calls | seconds | tokens | critic | pass |") in md
    assert "**all**" in md
