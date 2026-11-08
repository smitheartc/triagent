"""Remediation: loads only the matching runbook, fills placeholders, never comes back empty. No network."""
import json

import llm
import remediate
from tracelog import Trace

REPORT = {"problem": "rack B03 dark", "root_cause": {
    "summary": "breaker trip", "category": "power", "entity": "pdu-b03-a", "entity_type": "pdu",
    "identifiers": {"change_id": "CHG0005665"}, "event_ids": ["e1"]},
    "affected": {"hosts": ["cmp-b03-01", "cmp-b03-02"]}, "causal_chain": [], "confidence": 0.9}


def fake_chat(text, seen):
    def chat(messages, tools=None, model=None, thinking=True, max_tokens=4096, seed=None, trace=None,
             role="investigator"):
        assert role == "small" and tools is None and thinking is False
        seen.append(messages[1]["content"])
        return llm.ChatResult(text=text, tool_calls=[], usage={"prompt_tokens": 1, "completion_tokens": 1},
                              latency_ms=1)
    return chat


def test_runbook_selection_and_parsing():
    assert remediate.runbook_path("cooling").name == "cooling.md"
    assert remediate.runbook_path("aliens").name == "unknown.md"
    rb = remediate.parse_runbook(remediate.runbook_path("power").read_text())
    assert "PDU" in rb["entity_types"] and rb["immediate"] and rb["verify"] and rb["prevent"]
    assert rb["rollback_risk"] and rb["triage"]


def test_placeholders_come_from_the_report():
    vals = remediate.placeholder_values(REPORT)
    assert vals["pdu"] == "pdu-b03-a" and vals["rack"] == "B03" and vals["host"] == "cmp-b03-01"
    assert vals["change_id"] == "CHG0005665"
    assert remediate.fill("PDU `<pdu>` in rack `<rack>`, `<unknown_thing>`", vals) == \
        "PDU `pdu-b03-a` in rack `B03`, `<unknown_thing>`"


def test_model_gets_only_the_matching_runbook(monkeypatch):
    seen = []
    good = {"covered": True, "immediate": ["In the DCIM console, open PDU pdu-b03-a."], "verify": ["v"],
            "prevent": ["p"], "rollback_risk": "r"}
    monkeypatch.setattr(llm, "chat", fake_chat(json.dumps(good), seen))
    out = remediate.run(REPORT, Trace())
    assert out["source"] == "model" and out["runbook"] == "runbooks/power.md" and out["covered"]
    assert "# Power:" in seen[0] and "# Cooling:" not in seen[0] and "# Network:" not in seen[0]


def test_bad_model_output_falls_back_to_filled_runbook(monkeypatch):
    monkeypatch.setattr(llm, "chat", fake_chat("sorry, no JSON", []))
    out = remediate.run(REPORT, Trace())
    assert out["source"] == "runbook" and out["immediate"] and out["verify"] and out["rollback_risk"]
    assert any("pdu-b03-a" in s for s in out["immediate"]) and "note" in out
    monkeypatch.setattr(llm, "chat", fake_chat(json.dumps({"immediate": []}), []))   # fails validation
    assert remediate.run(REPORT, Trace())["source"] == "runbook"


def test_unknown_category_is_marked_not_covered(monkeypatch):
    monkeypatch.setattr(llm, "chat", fake_chat("no", []))
    rep = {**REPORT, "root_cause": {**REPORT["root_cause"], "category": "unknown"}}
    out = remediate.run(rep, Trace())
    assert out["runbook"] == "runbooks/unknown.md"


def test_step_rule_uses_real_code_spans_and_prose_types():
    types = ["service", "commit", "change"]
    # the text between two code spans is not a command
    assert remediate.step_compliant("Tell the author of commit `abc123` and change `CHG1`.", types) == (True, False)
    # a type word only inside a placeholder or a code span does not count
    assert remediate.step_compliant("Raise it: `kubectl set resources deployment/<service>`", types) == (False, True)
    assert remediate.step_compliant("Roll service web back: `kubectl rollout undo deployment/web`", types) == (True, True)
    assert remediate.step_compliant("In the change console, freeze change CHG1.", types) == (True, True)


def test_placeholder_aliases_from_real_report_shapes():
    rep = {"root_cause": {"entity": "auth-svc (config values-prod.yaml via change CHG0005760)",
                          "entity_type": "service config", "category": "config",
                          "identifiers": {"change.id": "CHG0005760", "cicd.commit": "f906", "file.path": "a/b.yaml",
                                          "user.name": "asmith"}},
           "affected": {"service": "auth-svc", "nodes": ["cmp-a02-01"], "observer": "lb-1",
                        "labels": {"rack": "A01"}}}
    v = remediate.placeholder_values(rep)
    assert v["service"] == "auth-svc" and v["commit"] == "f906" and v["file"] == "a/b.yaml"
    assert v["user"] == "asmith" and v["host"] == "cmp-a02-01" and v["lb"] == "lb-1" and v["rack"] == "A01"


def test_noncompliant_model_steps_get_one_repair_then_runbook(monkeypatch):
    weak = {"covered": True, "immediate": ["Fix it."], "verify": ["v"], "prevent": ["p"], "rollback_risk": ["a", "b"]}
    good = {**weak, "immediate": ["In the DCIM console, open PDU pdu-b03-a and record the current per feed."]}
    seen = []
    replies = iter([json.dumps(weak), json.dumps(good)])
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: (seen.append(messages[-1]["content"]) or
                                                             llm.ChatResult(text=next(replies), tool_calls=[],
                                                                            usage={}, latency_ms=1)))
    out = remediate.run(REPORT, Trace())
    assert out["source"] == "model" and "Fix it." in seen[1] and out["rollback_risk"] == "a b"
    replies = iter([json.dumps(weak), json.dumps(weak)])
    out = remediate.run(REPORT, Trace())
    assert out["source"] == "runbook" and "lacked" in out["note"]
    assert not remediate.noncompliant(out["immediate"], out["entity_types"])
