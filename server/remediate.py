"""Runbook-grounded remediation: report in, {immediate, verify, prevent, rollback_risk} out.

run(report, trace) loads only runbooks/<category>.md for the report's root_cause.category (unknown.md when the
category has no runbook) and asks the small model (TRIAGENT_MODEL_SMALL, reasoning off) to turn it into steps for
this incident, filling the runbook's <placeholders> from the report. If the model fails or returns something
unusable, the runbook's own steps are returned with the placeholders filled in code, so remediation never comes
back empty.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

import llm
from contracts import CATEGORIES, Remediation

log = logging.getLogger("triagent.remediate")

RUNBOOKS = Path(__file__).resolve().parent.parent / "runbooks"
PROMPT = (Path(__file__).parent / "prompts" / "remediate.md").read_text(encoding="utf-8")
SECTIONS = {"immediate": "immediate steps", "verify": "how to verify", "prevent": "how to prevent",
            "rollback_risk": "rollback risk", "triage": "safe triage", "symptoms": "symptoms this covers"}
MAX_TOKENS = 3072
_PLACEHOLDER = re.compile(r"<([a-z_]+)>")


class _ModelOutput(BaseModel):
    """What the small model must return; stricter than contracts.Remediation (no empty lists), lenient on shape."""
    covered: bool = True
    immediate: list[str] = Field(min_length=1)
    verify: list[str] = Field(min_length=1)
    prevent: list[str] = Field(min_length=1)
    rollback_risk: str = Field(min_length=1)

    @field_validator("immediate", "verify", "prevent", mode="before")
    @classmethod
    def _as_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [x if isinstance(x, str) else json.dumps(x) for x in v if x]
        return v

    @field_validator("rollback_risk", mode="before")
    @classmethod
    def _as_text(cls, v: Any) -> Any:
        return " ".join(str(x) for x in v) if isinstance(v, list) else v


_CODE = re.compile(r"`([^`]*)`")


def step_compliant(step: str, entity_types: list[str]) -> tuple[bool, bool]:
    """(names an entity type, has a concrete command or console action).

    The entity type must appear in the prose (outside code spans). A command is a code span with a space in it.
    """
    prose = _PLACEHOLDER.sub(" ", _CODE.sub(" ", step)).lower()
    has_type = any(t.lower() in prose for t in entity_types)
    has_action = any(" " in span.strip() for span in _CODE.findall(step)) or "console" in prose
    return has_type, has_action


def noncompliant(steps: list[str], entity_types: list[str]) -> list[str]:
    return [s for s in steps if not all(step_compliant(s, entity_types))]


# ---------------------------------------------------------------------------------------------
# runbook loading and parsing
# ---------------------------------------------------------------------------------------------
def runbook_path(category: str | None) -> Path:
    cat = category if category in CATEGORIES else "unknown"
    path = RUNBOOKS / f"{cat}.md"
    return path if path.exists() else RUNBOOKS / "unknown.md"


def parse_runbook(text: str) -> dict[str, Any]:
    """{"entity_types": [...], <section key>: [lines] or str} from a runbook's markdown."""
    out: dict[str, Any] = {"entity_types": []}
    m = re.search(r"^Entity types:\s*(.+)$", text, re.MULTILINE)
    if m:
        out["entity_types"] = [t.strip() for t in m.group(1).split(",") if t.strip()]
    for block in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        head, _, body = block.partition("\n")
        key = next((k for k, h in SECTIONS.items() if head.strip().lower().startswith(h)), None)
        if key is None:
            continue
        items = [ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")]
        out[key] = items if key != "rollback_risk" else " ".join(ln.strip() for ln in body.splitlines()
                                                                   if ln.strip())
    return out


def placeholder_values(report: dict[str, Any], ticket: dict[str, Any] | None = None) -> dict[str, str]:
    """Values for the runbook placeholders: from the report first, then the ticket's service, host and labels."""
    rc = report.get("root_cause") or {}
    ids = {str(k).lower().replace(".", "_"): v for k, v in (rc.get("identifiers") or {}).items()
           if isinstance(v, (str, int, float)) and str(v).strip()}
    aff = report.get("affected") or {}
    vals: dict[str, str] = {}

    def put(key: str, val: Any) -> None:
        if key not in vals and val not in (None, "", []):
            vals[key] = str(val[0] if isinstance(val, list) else val)

    etype = str(rc.get("entity_type", "")).lower()
    # "auth-svc (config values-prod.yaml via ...)" -> "auth-svc": placeholders take the bare name
    m = re.match(r"[\w.:/-]+", str(rc.get("entity") or ""))
    entity = m.group(0) if m else None
    for key, words in (("host", ("host", "node", "server")), ("service", ("service", "deployment")),
                       ("pdu", ("pdu",)), ("crac", ("crac",)), ("switch", ("switch",)),
                       ("lb", ("load_balancer", "load balancer", "lb")), ("certificate", ("certificate",)),
                       ("user", ("user", "account"))):
        if any(w in etype for w in words):
            put(key, entity)
    for k, v in ids.items():
        put(k, v)
    for alias, keys in (("commit", ("cicd_commit", "commit_sha", "sha")), ("file", ("file_path", "path")),
                        ("user", ("user_name", "change_requested_by", "compromised_user")),
                        ("change_id", ("change_id", "change")), ("interface", ("iface", "interface_name")),
                        ("hostname", ("certificate_common_name", "common_name", "cn", "endpoint")),
                        ("certificate", ("certificate_name", "secret", "certificate_secret")),
                        ("service", ("service_name",))):
        put(alias, next((ids[k] for k in keys if ids.get(k)), None))
    put("previous_version", ids.get("previous_version"))
    put("source_ip", ids.get("attacker_ip") or ids.get("source_ip"))
    put("host", aff.get("host") or aff.get("hosts") or aff.get("nodes"))
    put("service", aff.get("service") or aff.get("services"))
    put("rack", aff.get("rack") or aff.get("racks"))
    put("row", aff.get("row") or aff.get("rows"))
    put("lb", aff.get("load_balancer") or (aff.get("observer") if str(aff.get("observer", "")).startswith("lb-")
                                            else None))
    labels = aff.get("labels") if isinstance(aff.get("labels"), dict) else {}
    put("rack", labels.get("rack"))
    put("row", labels.get("row"))
    put("namespace", aff.get("namespace") if aff.get("namespace") not in (None, "dal1") else "prod")
    if entity:
        name = str(entity)
        if name.startswith("crac-"):
            put("crac", name)
            put("row", name.split("-", 1)[1].upper())
        elif name.startswith("pdu-"):
            put("pdu", name)
            put("rack", name.split("-")[1].upper())
        elif name.startswith("tor-"):
            put("switch", name)
            put("rack", name.split("-", 1)[1].upper())
        elif name.startswith("lb-"):
            put("lb", name)
        elif "." in name and not name.startswith("dns:") and "certificate" in etype:
            put("hostname", name)
    if "hostname" in vals:
        put("certificate", vals["hostname"].split(".")[0] + "-tls")
    t = ticket or {}
    put("service", t.get("service"))
    put("host", t.get("host"))
    for lvl in ("rack", "row"):
        put(lvl, (t.get("labels") or {}).get(lvl))
    return vals


def fill(text: str, vals: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: vals.get(m.group(1), m.group(0)), text)


def from_runbook(rb: dict[str, Any], vals: dict[str, str]) -> dict[str, Any]:
    """Deterministic fallback: the runbook's own steps with placeholders filled from the report."""
    return {"covered": True, "immediate": [fill(s, vals) for s in rb.get("immediate", [])],
            "verify": [fill(s, vals) for s in rb.get("verify", [])],
            "prevent": [fill(s, vals) for s in rb.get("prevent", [])],
            "rollback_risk": fill(rb.get("rollback_risk", ""), vals)}


# ---------------------------------------------------------------------------------------------
# model call
# ---------------------------------------------------------------------------------------------
def build_message(runbook_text: str, report: dict[str, Any], vals: dict[str, str]) -> str:
    rc = report.get("root_cause") or {}
    brief = {"problem": report.get("problem"), "root_cause": rc, "affected": report.get("affected"),
             "causal_chain": [{"timestamp": s.get("timestamp"), "what": s.get("what")}
                              for s in report.get("causal_chain") or []][:10],
             "open_questions": report.get("open_questions")}
    return "\n\n".join([f"Runbook ({runbook_text.splitlines()[0].lstrip('# ').strip()}):\n{runbook_text}",
                        "Report:\n" + json.dumps(brief, indent=1, default=str),
                        "Placeholder values found in the report:\n" + json.dumps(vals, indent=1)])


def parse(text: str) -> dict[str, Any] | None:
    for obj in llm._json_objects(llm.strip_think(text)):
        if "immediate" in obj:
            return obj
    return None


def _dedupe(steps: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for s in steps:
        key = re.sub(r"\W+", " ", s.lower()).strip()
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def run(report: dict[str, Any], trace: Any, ticket: dict[str, Any] | None = None) -> dict[str, Any]:
    """ticket (optional) fills placeholders the report leaves open, such as the ticket's host or service."""
    category = (report.get("root_cause") or {}).get("category")
    path = runbook_path(category)
    text = path.read_text(encoding="utf-8")
    rb = parse_runbook(text)
    vals = placeholder_values(report, ticket)
    out: dict[str, Any] | None = None
    note = None
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": build_message(text, report, vals)}]
    try:
        res = llm.chat(messages, role="small", thinking=False, max_tokens=MAX_TOKENS,
                       trace=lambda d: trace.add("llm", **d))
        raw = parse(res.text)
        problem = None
        if raw is None:
            problem = "Return only the JSON object."
        else:
            try:
                bad = noncompliant(_ModelOutput.model_validate(raw).immediate, rb["entity_types"])
                if bad:
                    problem = ("These immediate steps do not name an entity type in plain words or lack a command "
                               "or console action from the runbook. Rewrite them and return the full JSON object:\n"
                               + "\n".join(f"- {s}" for s in bad))
            except ValidationError as exc:
                problem = f"The JSON did not match the required shape: {exc.errors()[0]['loc']} " \
                          f"{exc.errors()[0]['msg']}. Return the full JSON object."
        if problem:   # one repair attempt, then the runbook's own steps
            res = llm.chat(messages + [{"role": "assistant", "content": res.text or ""},
                                       {"role": "user", "content": problem}], role="small", thinking=False,
                           max_tokens=MAX_TOKENS, trace=lambda d: trace.add("llm", **d))
            raw = parse(res.text)
        if raw is not None:
            cand = _ModelOutput.model_validate(raw).model_dump()
            bad = noncompliant(cand["immediate"], rb["entity_types"])
            if bad:
                note = f"{len(bad)} model step(s) lacked an entity type or a command; runbook steps used"
            else:
                out = cand
        else:
            note = "model output was not JSON; runbook steps used"
    except ValidationError as exc:
        err = exc.errors()[0]
        note = f"model output failed validation ({err['loc']}: {err['msg']}); runbook steps used"
    except Exception as exc:  # noqa: BLE001 - remediation must never be empty
        log.warning("remediation model call failed: %s", exc)
        note = f"model call failed ({type(exc).__name__}); runbook steps used"
    source = "model"
    if out is None:
        out, source = from_runbook(rb, vals), "runbook"
    out.update(category=category, runbook=f"runbooks/{path.name}", entity_types=rb["entity_types"], source=source)
    if path.stem != category:
        out["covered"] = False
    if note:
        out["note"] = note
    for key in ("immediate", "verify", "prevent"):
        out[key] = _dedupe(out[key])
    out = Remediation.model_validate(out).model_dump(exclude_none=True)
    trace.add("remediation", runbook=out["runbook"], covered=out["covered"], source=source,
              steps=len(out["immediate"]), **({"note": note} if note else {}))
    return out
