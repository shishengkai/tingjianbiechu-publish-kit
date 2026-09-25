"""Per-call cost accounting from API responses + official price tables.

None of the v0.1 smoke responses returned a money field; costs are computed from
``usage`` (tokens / image counts) using ``config/pricing.yaml``. If a response
later includes an explicit amount, that value wins (USD amounts × usd_to_cny).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PRICING = _REPO_ROOT / "config" / "pricing.yaml"
_MILLION = Decimal("1000000")


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal, decimals: int = 8) -> Decimal:
    quant = Decimal("1").scaleb(-decimals)
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def _money_str(value: Decimal, decimals: int = 8) -> str:
    return f"{_money(value, decimals):.{decimals}f}"


def load_pricing(path: Path | None = None) -> dict:
    cfg_path = path if path is not None else _DEFAULT_PRICING
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"pricing config must be a mapping: {cfg_path}")
    return data


def resolve_model_key(model: str, pricing: dict) -> str:
    models = pricing.get("models") or {}
    if model in models:
        return model
    for key, spec in models.items():
        aliases = spec.get("aliases") or []
        if model in aliases:
            return key
    raise KeyError(f"no pricing entry for model: {model}")


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def is_deepseek_peak(when: datetime, pricing: dict) -> bool:
    """Peak hours per DeepSeek docs: UTC Mon–Fri windows, not Chinese holidays."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    holidays = set(pricing.get("chinese_public_holidays_utc_dates") or [])
    if when.date().isoformat() in holidays:
        return False
    peak_cfg = pricing.get("deepseek_peak_utc") or {}
    if peak_cfg.get("weekdays_only", True) and when.weekday() >= 5:
        return False
    for window in peak_cfg.get("windows") or []:
        start = _parse_hhmm(window["start"])
        end = _parse_hhmm(window["end"])
        t = when.timetz().replace(tzinfo=None)
        if start <= t < end:
            return True
    return False


def extract_explicit_cost_cny(response: dict, usd_to_cny: Decimal) -> Decimal | None:
    """Use vendor-reported money if present; otherwise None."""
    candidates: list[tuple[Decimal, str]] = []

    def consider(amount: Any, currency: str | None) -> None:
        if amount is None:
            return
        try:
            value = _d(amount)
        except Exception:
            return
        cur = (currency or "").lower()
        if cur in ("usd", "dollar", "$"):
            candidates.append((value * usd_to_cny, "usd"))
        elif cur in ("cny", "rmb", "cnh", "yuan", "元", ""):
            # bare numbers without currency are treated as CNY only for
            # well-known money keys handled below.
            candidates.append((value, "cny"))

    for key in ("cost", "total_cost", "price", "amount", "fee"):
        raw = response.get(key)
        if isinstance(raw, dict):
            consider(raw.get("amount") or raw.get("total") or raw.get("value"),
                     raw.get("currency") or raw.get("unit"))
        elif isinstance(raw, (int, float, str, Decimal)):
            # Ambiguous bare number: only accept under *_usd / *_cny keys.
            pass

    for key, currency in (
        ("cost_usd", "usd"), ("total_cost_usd", "usd"), ("price_usd", "usd"),
        ("cost_cny", "cny"), ("total_cost_cny", "cny"), ("price_cny", "cny"),
        ("cost_rmb", "cny"),
    ):
        if key in response:
            consider(response[key], currency)

    usage = response.get("usage")
    if isinstance(usage, dict):
        for key, currency in (
            ("cost_usd", "usd"), ("total_cost_usd", "usd"),
            ("cost_cny", "cny"), ("total_cost_cny", "cny"),
            ("cost", None),
        ):
            if key in usage:
                if key == "cost" and not isinstance(usage[key], dict):
                    continue
                if isinstance(usage[key], dict):
                    consider(usage[key].get("amount"), usage[key].get("currency"))
                else:
                    consider(usage[key], currency)

    if not candidates:
        return None
    # Prefer first discovered explicit amount.
    return candidates[0][0]


def _token_parts(usage: dict) -> tuple[int, int, int]:
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(
        usage.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens")
        or 0
    )
    cached = min(cached, prompt)
    return prompt, completion, cached


def _thinking(usage: dict, response: dict) -> bool:
    details = usage.get("completion_tokens_details") or {}
    if details.get("reasoning_tokens"):
        return True
    if response.get("thinking") or (response.get("choices") or [{}])[0].get("message", {}).get("reasoning_content"):
        return True
    return False


def _tier_for_input(tiers: list[dict], prompt_tokens: int) -> dict:
    for tier in tiers:
        if prompt_tokens <= int(tier["max_input_tokens"]):
            return tier
    return tiers[-1]


def compute_cost_cny(
    model: str,
    response: dict,
    *,
    pricing: dict | None = None,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Return a cost record in CNY for one API response."""
    pricing = pricing if pricing is not None else load_pricing()
    when = when or datetime.now(timezone.utc)
    usd_to_cny = _d(pricing.get("usd_to_cny", 7))
    decimals = int(pricing.get("display_decimals", 8))
    explicit = extract_explicit_cost_cny(response, usd_to_cny)
    model_key = resolve_model_key(model, pricing)
    spec = (pricing.get("models") or {})[model_key]
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}

    if explicit is not None:
        amount = _money(explicit, decimals)
        return {
            "model": model,
            "model_key": model_key,
            "amount_cny": amount,
            "currency_out": "CNY",
            "source": "response",
            "usage": usage,
            "details": {"note": "vendor-reported amount"},
        }

    unit = spec.get("unit")
    details: dict[str, Any] = {}
    amount = Decimal("0")

    if unit == "per_1m_tokens" and model_key == "deepseek-flash":
        prompt, completion, cached = _token_parts(usage)
        miss = prompt - cached
        peak = is_deepseek_peak(when, pricing)
        rates = spec["peak" if peak else "off_peak"]
        amount = (
            _d(cached) / _MILLION * _d(rates["input_cache_hit"])
            + _d(miss) / _MILLION * _d(rates["input_cache_miss"])
            + _d(completion) / _MILLION * _d(rates["output"])
        )
        if spec.get("currency") == "USD":
            amount *= usd_to_cny
        details = {
            "peak": peak,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": cached,
            "cache_miss_tokens": miss,
            "rates_usd": rates,
            "usd_to_cny": float(usd_to_cny),
        }
    elif unit == "per_1m_tokens" and model_key == "qwen-plus":
        prompt, completion, cached = _token_parts(usage)
        miss = prompt - cached
        thinking = _thinking(usage, response)
        tier = _tier_for_input(spec["tiers"], prompt)
        if thinking:
            hit_rate = _d(tier["input_thinking_cache_hit"])
            miss_rate = _d(tier["input_thinking"])
            out_rate = _d(tier["output_thinking"])
        else:
            hit_rate = _d(tier["input_cache_hit"])
            miss_rate = _d(tier["input"])
            out_rate = _d(tier["output"])
        amount = (
            _d(cached) / _MILLION * hit_rate
            + _d(miss) / _MILLION * miss_rate
            + _d(completion) / _MILLION * out_rate
        )
        details = {
            "thinking": thinking,
            "tier_max_input_tokens": tier["max_input_tokens"],
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": cached,
            "cache_miss_tokens": miss,
            "rates_cny_per_1m": {
                "input_cache_hit": float(hit_rate),
                "input": float(miss_rate),
                "output": float(out_rate),
            },
        }
    elif unit == "per_1m_tokens":
        prompt, completion, cached = _token_parts(usage)
        miss = prompt - cached
        hit_rate = _d(spec.get("input_cache_hit", spec["input"]))
        miss_rate = _d(spec["input"])
        out_rate = _d(spec["output"])
        # Models without cache support still may report cached_tokens=0.
        amount = (
            _d(cached) / _MILLION * hit_rate
            + _d(miss) / _MILLION * miss_rate
            + _d(completion) / _MILLION * out_rate
        )
        details = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "cache_hit_tokens": cached,
            "cache_miss_tokens": miss,
            "rates_cny_per_1m": {
                "input_cache_hit": float(hit_rate),
                "input": float(miss_rate),
                "output": float(out_rate),
            },
        }
    elif unit == "per_image" and model_key == "qwen-image-3.0-pro":
        in_count = int(usage.get("input_image_count") or 0)
        out_count = int(usage.get("output_image_count") or 0)
        in_type = str(usage.get("input_image_type") or "")
        out_type = str(usage.get("output_image_type") or "")

        def image_rate(kind: str, type_name: str) -> Decimal:
            if "2k" in type_name.lower():
                return _d(spec[f"{kind}_2k"])
            return _d(spec[f"{kind}_1k"])

        in_rate = image_rate("input", in_type) if in_count else Decimal("0")
        out_rate = image_rate("output", out_type) if out_count else Decimal("0")
        amount = _d(in_count) * in_rate + _d(out_count) * out_rate
        details = {
            "input_image_count": in_count,
            "output_image_count": out_count,
            "input_image_type": in_type,
            "output_image_type": out_type,
            "rates_cny_per_image": {
                "input": float(in_rate) if in_count else None,
                "output": float(out_rate) if out_count else None,
            },
        }
    else:
        raise ValueError(f"unsupported pricing unit for {model_key}: {unit}")

    amount = _money(amount, decimals)
    return {
        "model": model,
        "model_key": model_key,
        "amount_cny": amount,
        "currency_out": "CNY",
        "source": "calculated",
        "usage": usage,
        "details": details,
        "when": when.astimezone(timezone.utc).isoformat(),
    }


@dataclass
class CostLedger:
    """Accumulate per-call costs for one publish task."""

    pricing: dict = field(default_factory=load_pricing)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add_response(
        self,
        *,
        role: str,
        model: str,
        response: dict,
        when: datetime | None = None,
    ) -> dict[str, Any]:
        record = compute_cost_cny(model, response, pricing=self.pricing, when=when)
        record["role"] = role
        self.entries.append(record)
        return record

    def summary(self) -> dict[str, Any]:
        decimals = int(self.pricing.get("display_decimals", 8))
        by_model: dict[str, Decimal] = {}
        for entry in self.entries:
            key = entry["model_key"]
            by_model[key] = by_model.get(key, Decimal("0")) + _d(entry["amount_cny"])
        total = sum(by_model.values(), Decimal("0"))
        return {
            "currency": "CNY",
            "usd_to_cny": self.pricing.get("usd_to_cny", 7),
            "display_decimals": decimals,
            # Strings avoid binary float rounding at 8 decimal places.
            "by_model_cny": {k: _money_str(v, decimals) for k, v in sorted(by_model.items())},
            "total_cny": _money_str(total, decimals),
            "calls": len(self.entries),
            "entries": [
                {
                    **{k: v for k, v in e.items() if k != "amount_cny"},
                    "amount_cny": _money_str(_d(e["amount_cny"]), decimals),
                }
                for e in self.entries
            ],
        }

    def format_report(self) -> str:
        summary = self.summary()
        lines = ["## 模型花费（人民币）", ""]
        for model, amount in summary["by_model_cny"].items():
            lines.append(f"- `{model}`: ¥{amount}")
        lines.append(f"- **合计**: ¥{summary['total_cny']}")
        return "\n".join(lines)
