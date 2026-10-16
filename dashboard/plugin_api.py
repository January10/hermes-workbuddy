"""WorkBuddy 面板后端 —— 挂载在 ``/api/plugins/hermes-workbuddy``。

桌面 App 的 WorkBuddy 页面（``desktop-plugins/hermes-workbuddy/plugin.js``，统一包的桌面半边）通过
``ctx.rest('/snapshot')`` 读这里；数据全部来自同一个统一包注册的两个提供方 profile
（同进程，无子进程、无端口、无服务）。

* ``GET /snapshot`` — 两个变体的账号 / 模型（含倍率、上下文、能力、推理档位）/ 额度
* ``GET /ping``     — 轻量健康检查

结果带 30 秒内存缓存：页面轮询不会每次打上游。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger(__name__)

PROVIDERS = ("workbuddy", "workbuddy-ai")
CACHE_TTL_SECONDS = 30.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "payload": None}


def _rate_number(text: Any) -> float | None:
    digits = ""
    for char in str(text or ""):
        if char.isdigit() or char == ".":
            digits += char
        elif digits:
            break
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def _variant_payload(provider: str) -> dict[str, Any]:
    from providers import get_provider_profile

    profile = get_provider_profile(provider)
    models: list[str] = []
    live = True
    try:
        models = list(profile.fetch_models() or [])
    except Exception:
        log.debug("workbuddy: catalog fetch failed for %s", provider, exc_info=True)
        live = False
    if not models:
        models = [str(model) for model in (profile.fallback_models or ())]
        live = False
    rates = dict(getattr(profile, "model_credits", {}) or {})
    capabilities = dict(getattr(profile, "model_capabilities", {}) or {})
    efforts = dict(getattr(profile, "model_efforts", {}) or {})
    account: dict[str, Any] = {}
    try:
        account = dict(profile.describe_account() or {})
    except Exception:
        log.debug("workbuddy: account snapshot failed for %s", provider, exc_info=True)
    usage_payload: dict[str, Any] | None = None
    credits_payload: dict[str, Any] | None = None
    try:
        usage = profile.fetch_account_usage()
    except Exception:
        usage = None
        log.debug("workbuddy: usage fetch failed for %s", provider, exc_info=True)
    if usage is not None:
        usage_payload = {
            "title": usage.title,
            "identity": usage.identity,
            "windows": [
                {"label": window.label, "used_percent": window.used_percent, "detail": window.detail}
                for window in usage.windows
            ],
            "details": list(usage.details),
        }
    credits_payload = getattr(profile, "last_credits", None)
    if isinstance(credits_payload, dict):
        credits_payload = dict(credits_payload)
        # 页面口径（用户定的）：只要总分 + 3 天内到期的可用分数，包里明细不外发。
        credits_payload.pop("soon", None)
    else:
        credits_payload = None

    promotions: list[dict[str, Any]] = []
    try:
        promotions = [dict(item) for item in profile.promotions_list()]
    except Exception:
        log.debug("workbuddy: promotions snapshot failed for %s", provider, exc_info=True)

    rows: list[dict[str, Any]] = []
    for mid in models:
        capability = capabilities.get(mid) or {}
        try:
            status = profile.model_status(mid)
            status_detail = profile.model_status_detail(mid)
            effective = profile.model_effective_multiplier(mid)
        except Exception:
            status, status_detail, effective = None, "", _rate_number(rates.get(mid))
        # 运行时事实（用户定的 A 方案）：最近撞到上游限流就标出来，他自己决定换不换模型。
        limited_until = 0.0
        try:
            limited_until = float(profile.model_rate_limited_until(mid) or 0.0)
        except Exception:
            limited_until = 0.0
        rows.append({
            "id": mid,
            "credits": rates.get(mid),
            "multiplier": _rate_number(rates.get(mid)),
            "effective_multiplier": effective,
            "free": effective == 0,
            "status": status,
            "status_detail": status_detail,
            "rate_limited_until": limited_until,
            "context_window": capability.get("context_window"),
            "supports_tools": bool(capability.get("supports_tools")),
            "supports_vision": bool(capability.get("supports_vision")),
            "supports_reasoning": bool(capability.get("supports_reasoning")),
            "reasoning_efforts": list(efforts.get(mid) or ()),
        })
    rows.sort(key=lambda row: (row["effective_multiplier"] is None, row["effective_multiplier"] or 0.0, row["id"]))
    account_limited_until = 0.0
    try:
        account_limited_until = float(profile.account_rate_limited_until() or 0.0)
    except Exception:
        account_limited_until = 0.0
    return {
        "provider": provider,
        "label": profile.display_name,
        "region": account.get("region") or ("cn" if provider == "workbuddy" else "global"),
        "live": live,
        "rate_limited_until": account_limited_until,
        "account": account,
        "usage": usage_payload,
        "credits": credits_payload,
        "promotions": promotions,
        "models": rows,
    }


def _snapshot(force: bool = False) -> dict[str, Any]:
    now = time.time()
    with _LOCK:
        cached = _CACHE.get("payload")
        if cached is not None and not force and now - float(_CACHE.get("at") or 0.0) < CACHE_TTL_SECONDS:
            return cached
    variants: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        try:
            variants.append(_variant_payload(provider))
        except Exception as exc:  # one broken variant must not blank the page
            log.debug("workbuddy: variant %s failed", provider, exc_info=True)
            variants.append({"provider": provider, "label": provider, "error": f"{type(exc).__name__}: {exc}"})
    payload = {"ok": True, "fetched_at": int(now), "variants": variants}
    with _LOCK:
        _CACHE["at"] = now
        _CACHE["payload"] = payload
    return payload


@router.get("/ping")
def ping() -> dict[str, Any]:
    """轻量探活：不碰上游，也不触发目录抓取。"""
    return {"ok": True, "providers": list(PROVIDERS)}


@router.get("/snapshot")
def snapshot(force: bool = False) -> dict[str, Any]:
    """两个变体的账号 / 模型倍率 / 额度（30 秒缓存；``?force=1`` 强制刷新）。"""
    try:
        return _snapshot(force=bool(force))
    except Exception as exc:
        log.warning("workbuddy: snapshot failed", exc_info=True)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
