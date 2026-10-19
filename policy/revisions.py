"""Bounded deterministic policy validation and a shared preview/live evaluator."""

import hashlib
import json
import math
import re
import uuid
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Workflow

RISK = Literal["R0", "R1", "R2", "R3"]
SCALAR = (str, int, float, bool, type(None))


def number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Numeric operands must be JSON numbers")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Numeric operands must be finite")
    return Decimal(str(value))


def domain(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("Invalid domain")
    result = value.rstrip(".").encode("idna").decode().lower()
    if not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in result.split(".")
    ):
        raise ValueError("Invalid domain")
    return result


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=500, pattern=r"^/")
    op: Literal["exists", "eq", "in", "lt", "lte", "gt", "gte", "recipient_domain_in"]
    value: Any
    include_subdomains: bool = False

    @model_validator(mode="after")
    def operands(self) -> "Condition":
        if re.search(r"~(?![01])", self.path):
            raise ValueError("Invalid JSON Pointer escape")
        json.dumps(self.value, allow_nan=False)
        if self.op == "exists" and not isinstance(self.value, bool):
            raise ValueError("Presence comparison requires a boolean")
        if self.op == "eq" and not isinstance(self.value, SCALAR):
            raise ValueError("Equality requires a JSON scalar")
        if self.op in ("in", "recipient_domain_in"):
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 100:
                raise ValueError("Membership requires 1–100 values")
            if any(not isinstance(v, SCALAR) for v in self.value):
                raise ValueError("Membership values must be scalars")
        if self.op in ("lt", "lte", "gt", "gte"):
            number(self.value)
        if self.op == "recipient_domain_in":
            self.value = [domain(v) for v in self.value]
        if self.include_subdomains and self.op != "recipient_domain_in":
            raise ValueError("Subdomains apply only to recipient restrictions")
        return self


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")
    action_name: str = Field(min_length=1, max_length=100)
    outcome: Literal["deny", "review", "allow"]
    conditions: list[Condition] = Field(default_factory=list, max_length=30)
    risk_class: RISK = "R0"
    timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    on_timeout: Literal["deny", "allow"] = "deny"


class Loop(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_steps: int = Field(default=100, gt=0, le=2**31 - 1)
    rpm: int = Field(default=120, gt=0, le=1000000)
    run_cost_micro: int | None = Field(default=None, gt=0, le=2**63 - 1)


class RunBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    per_run_micro: int = Field(gt=0, le=2**63 - 1)


class Outage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["open", "closed"] = "closed"


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    models: list[str] = Field(default_factory=list, max_length=100)
    rpm: int = Field(default=60, gt=0, le=1000000)
    per_run_micro: int | None = Field(default=None, gt=0, le=2**63 - 1)
    budget: RunBudget | None = None
    loop_breaker: Loop = Field(default_factory=Loop)
    max_output_tokens: int = Field(default=32768, gt=0, le=32768)
    outage: Outage = Field(default_factory=Outage)
    stop_escalation: Literal["run", "workflow", "client"] = "run"
    approval_channels: list[Literal["telegram", "email", "webhook"]] = Field(
        default=["telegram", "email", "webhook"], max_length=3
    )
    rules: list[Rule] = Field(default_factory=list, max_length=100)
    strict_actions: Literal[True] = True


def normalize(policy: dict[str, Any], risk: str, *, legacy: bool = False) -> dict[str, Any]:
    candidate = json.loads(json.dumps(policy, allow_nan=False))
    actions = candidate.pop("actions", None)
    if actions is not None:
        if "rules" in candidate or not isinstance(actions, dict):
            raise ValueError("Use rules or legacy actions, not both")
        rules = []
        for name, old in actions.items():
            if not isinstance(old, dict) or set(old) - {
                "risk_class",
                "require_approval",
                "timeout_seconds",
                "on_timeout",
            }:
                raise ValueError("Legacy action rule is invalid")
            if not isinstance(old.get("require_approval", False), bool):
                raise ValueError("require_approval must be boolean")
            action_risk = old.get("risk_class", risk)
            rules.append(
                {
                    "id": "legacy-" + hashlib.sha256(name.encode()).hexdigest()[:16],
                    "action_name": name,
                    "risk_class": action_risk,
                    "outcome": "review"
                    if action_risk >= "R2" or old.get("require_approval", False)
                    else "allow",
                    "timeout_seconds": old.get("timeout_seconds", 3600),
                    "on_timeout": old.get("on_timeout", "deny"),
                }
            )
        candidate["rules"] = rules
    if legacy:
        candidate["strict_actions"] = True
    parsed = Policy.model_validate(candidate)
    if risk not in ("R0", "R1", "R2", "R3"):
        raise ValueError("Invalid workflow risk")
    if parsed.outage.mode == "open" and risk >= "R2":
        raise ValueError("High-risk workflows must fail closed")
    if len({r.id for r in parsed.rules}) != len(parsed.rules):
        raise ValueError("Rule IDs must be unique")
    for rule in parsed.rules:
        if max(risk, rule.risk_class) >= "R2" and rule.on_timeout == "allow":
            raise ValueError("High-risk rules must deny on timeout")
    if any(not pattern or len(pattern) > 200 for pattern in parsed.models):
        raise ValueError("Model patterns must contain 1–200 characters")
    result = parsed.model_dump(exclude_none=True)
    limits = [
        value
        for value in (
            parsed.per_run_micro,
            parsed.budget.per_run_micro if parsed.budget else None,
            parsed.loop_breaker.run_cost_micro,
        )
        if value is not None
    ]
    if limits and min(limits) < 2**63 - 1:
        result["per_run_micro"] = min(limits)
    else:
        result.pop("per_run_micro", None)
    result.pop("budget", None)
    result["loop_breaker"].pop("run_cost_micro", None)
    # Existing budget parser accepts an absent object, not null.
    return dict(result)


def digest(policy: dict[str, Any], risk: str) -> bytes:
    return hashlib.sha256(
        json.dumps(
            {"policy": policy, "risk_class": risk},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).digest()


def pointer(action: dict[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = action
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif (
            isinstance(node, list)
            and re.fullmatch(r"0|[1-9][0-9]*", part)
            and int(part) < len(node)
        ):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def equal(left: Any, right: Any) -> bool:
    if (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
    ):
        return number(left) == number(right)
    return type(left) is type(right) and left == right


def matches(condition: Condition, action: dict[str, Any]) -> bool:
    found, value = pointer(action, condition.path)
    if condition.op == "exists":
        return found is condition.value
    if not found:
        raise ValueError("missing_operand:" + condition.path)
    if condition.op == "eq":
        if not isinstance(value, SCALAR):
            raise ValueError("invalid_operand:" + condition.path)
        return equal(value, condition.value)
    if condition.op == "in":
        if not isinstance(value, SCALAR):
            raise ValueError("invalid_operand:" + condition.path)
        return any(equal(value, item) for item in condition.value)
    if condition.op in ("lt", "lte", "gt", "gte"):
        a, b = number(value), number(condition.value)
        return {"lt": a < b, "lte": a <= b, "gt": a > b, "gte": a >= b}[condition.op]
    recipients = value if isinstance(value, list) else [value]
    if not recipients:
        raise ValueError("invalid_recipient")
    for recipient in recipients:
        if (
            not isinstance(recipient, str)
            or recipient.count("@") != 1
            or re.search(r"[\s<>;,]", recipient)
        ):
            raise ValueError("invalid_recipient")
        local, host = recipient.rsplit("@", 1)
        if not local:
            raise ValueError("invalid_recipient")
        host = domain(host)
        if not any(
            host == allowed or condition.include_subdomains and host.endswith("." + allowed)
            for allowed in condition.value
        ):
            return False
    return True


def evaluate(
    policy: dict[str, Any], risk: str, action_name: str, action: dict[str, Any]
) -> dict[str, Any]:
    normalized = normalize(policy, risk)
    matched = []
    try:
        for data in normalized["rules"]:
            rule = Rule.model_validate(data)
            if rule.action_name != action_name:
                continue
            # Evaluate all operands: a false comparison must not hide malformed input.
            values = [matches(c, action) for c in rule.conditions]
            if all(values):
                matched.append(rule)
    except ValueError as error:
        return {
            "decision": "deny",
            "reason": str(error),
            "matched_rules": [],
            "risk_class": risk,
            "timeout_seconds": 3600,
            "on_timeout": "deny",
        }
    effective = max([risk, *[r.risk_class for r in matched]])
    for outcome, decision in (("deny", "deny"), ("review", "pending"), ("allow", "allow")):
        selected = [r for r in matched if r.outcome == outcome]
        if selected:
            return {
                "decision": "pending" if decision == "allow" and effective >= "R2" else decision,
                "reason": "rule_match",
                "matched_rules": [r.id for r in matched],
                "risk_class": effective,
                "timeout_seconds": min(r.timeout_seconds for r in selected),
                "on_timeout": "allow"
                if effective < "R2" and all(r.on_timeout == "allow" for r in selected)
                else "deny",
            }
    return {
        "decision": "deny",
        "reason": "unmatched_action",
        "matched_rules": [],
        "risk_class": effective,
        "timeout_seconds": 3600,
        "on_timeout": "deny",
    }


async def lock(session: AsyncSession, workflow_id: uuid.UUID) -> Workflow:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": "policy:" + str(workflow_id)},
    )
    workflow = (
        await session.execute(
            select(Workflow)
            .where(Workflow.id == workflow_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return workflow
