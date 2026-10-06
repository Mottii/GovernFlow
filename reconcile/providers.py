import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, cast

import httpx

from core.models import ProviderAccount


@dataclass
class Observation:
    usage: dict[str, dict[str, int]]
    cost_micro: int
    cost_groups: list[dict[str, Any]]


def money(value: Any, factor: int) -> int:
    amount = Decimal(str(value)) * factor
    if not amount.is_finite() or amount < 0:
        raise ValueError("Provider cost is invalid")
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


async def pages(
    http: httpx.AsyncClient, url: str, params: list[tuple[str, str]], headers: dict[str, str]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(1000):
        response = await http.get(url, params=tuple(params), headers=headers)
        response.raise_for_status()
        data = response.json()
        for bucket in data["data"]:
            results.extend(bucket["results"])
        if not data.get("has_more"):
            return results
        cursor = data.get("next_page")
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Provider pagination is invalid")
        seen.add(cursor)
        params = [(name, value) for name, value in params if name != "page"] + [("page", cursor)]
    raise ValueError("Provider pagination exceeded its limit")


def count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Provider token count is invalid")
    return cast(int, value)


def add(
    usage: dict[str, dict[str, int]],
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> None:
    bucket = usage.setdefault(
        model,
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )
    for name, value in zip(
        bucket, (input_tokens, output_tokens, cache_read, cache_write), strict=True
    ):
        bucket[name] += value


async def fetch(account: ProviderAccount, day: date) -> Observation:
    start = datetime.combine(day, time(), UTC)
    end = start + timedelta(days=1)
    usage: dict[str, dict[str, int]] = {}
    groups: list[dict[str, Any]] = []
    key = os.environ.get(account.provider.upper() + "_ADMIN_KEY")
    if not key:
        raise RuntimeError("Reporting credential is not configured")
    async with httpx.AsyncClient(timeout=30) as http:
        if account.provider == "openai":
            base = os.environ.get("OPENAI_REPORTING_BASE_URL", "https://api.openai.com").rstrip("/")
            headers = {"authorization": "Bearer " + key}
            query = [
                ("start_time", str(int(start.timestamp()))),
                ("end_time", str(int(end.timestamp()))),
                ("bucket_width", "1d"),
                ("project_ids", account.external_id),
            ]
            for endpoint in ("completions", "embeddings"):
                rows = await pages(
                    http,
                    base + "/v1/organization/usage/" + endpoint,
                    query + [("group_by", "project_id"), ("group_by", "model")],
                    headers,
                )
                for row in rows:
                    if row.get("project_id") != account.external_id:
                        raise ValueError("Provider usage escaped the mapped project")
                    if not isinstance(row.get("model"), str):
                        raise ValueError("Provider usage model is missing")
                    add(
                        usage,
                        row["model"],
                        count(row.get("input_tokens", 0)),
                        count(row.get("output_tokens", 0)),
                        count(row.get("input_cached_tokens", 0)),
                        count(row.get("input_cache_write_tokens", 0))
                        + count(row.get("input_cache_write_12h_tokens", 0)),
                    )
            groups = await pages(
                http,
                base + "/v1/organization/costs",
                query + [("group_by", "project_id"), ("group_by", "line_item")],
                headers,
            )
            if any(row.get("project_id") != account.external_id for row in groups):
                raise ValueError("Provider costs escaped the mapped project")
            if any(row["amount"]["currency"].upper() != "USD" for row in groups):
                raise ValueError("Provider currency is unsupported")
            total = money(
                sum((Decimal(str(row["amount"]["value"])) for row in groups), Decimal(0)), 1000000
            )
        else:
            base = os.environ.get(
                "ANTHROPIC_REPORTING_BASE_URL", "https://api.anthropic.com"
            ).rstrip("/")
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            query = [
                ("starting_at", start.isoformat().replace("+00:00", "Z")),
                ("ending_at", end.isoformat().replace("+00:00", "Z")),
                ("bucket_width", "1d"),
            ]
            rows = await pages(
                http,
                base + "/v1/organizations/usage_report/messages",
                query
                + [
                    ("workspace_ids[]", account.external_id),
                    ("group_by[]", "workspace_id"),
                    ("group_by[]", "model"),
                ],
                headers,
            )
            for row in rows:
                if row.get("workspace_id") != account.external_id or not isinstance(
                    row.get("model"), str
                ):
                    raise ValueError("Provider usage escaped the mapped workspace")
                cache = row.get("cache_creation", {})
                writes = count(cache.get("ephemeral_5m_input_tokens", 0)) + count(
                    cache.get("ephemeral_1h_input_tokens", 0)
                )
                reads = count(row.get("cache_read_input_tokens", 0))
                add(
                    usage,
                    row["model"],
                    count(row.get("uncached_input_tokens", 0)) + writes + reads,
                    count(row.get("output_tokens", 0)),
                    reads,
                    writes,
                )
            # Cost reporting supports grouping by workspace/description, not a workspace filter.
            all_costs = await pages(
                http,
                base + "/v1/organizations/cost_report",
                query + [("group_by[]", "workspace_id"), ("group_by[]", "description")],
                headers,
            )
            groups = [row for row in all_costs if row.get("workspace_id") == account.external_id]
            if any(row["currency"].upper() != "USD" for row in groups):
                raise ValueError("Provider currency is unsupported")
            total = money(sum((Decimal(row["amount"]) for row in groups), Decimal(0)), 10000)
    return Observation(usage, total, groups)
