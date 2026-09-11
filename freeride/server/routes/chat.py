"""``POST /v1/chat/completions`` — OpenAI-compatible chat completions.

Cross-provider failover lives in :mod:`freeride.core.failover`. This
module owns the OpenAI Chat Completions envelope, streaming SSE
framing, and auto-model resolution for this route.

Streaming uses buffer-first-chunk failover: hold the first SSE event
until upstream confirms 200 + first chunk. Once the first chunk has
shipped to the client, mid-stream errors propagate as a truncated
stream (rare in practice; documented limitation).
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from freeride.core.auto_model import is_auto_model, resolve_auto_model
from freeride.core.chat_schema import ChatRequest, ChatStreamEvent
from freeride.core.cooldown import KeyCooldown
from freeride.core.events import emit as emit_event
from freeride.core.events import new_request_id
from freeride.core.failover import (
    FailoverContext,
    apply_force_provider,
    build_503_detail,
    record_health,
    resolve_provider_chain,
    suggestion,
    try_call_with_failover,
    try_stream_with_failover,
)
from freeride.core.health import sort_by_health
from freeride.core.provider import Provider
from freeride.core.provider_env import env_var_for
from freeride.core.x402_hedera import (
    X402Error,
    attach_payment_response,
    auto_pay_settle,
    build_402_response,
    decode_payment_payload,
    extract_payment_header,
    has_payer_credentials,
    load_x402_config,
    paid_openrouter_key,
    payment_requirements_from_required,
    build_payment_required,
    resolve_fee_payer,
    verify_and_settle,
)
from freeride.server.routes.models import get_or_fetch_catalog, invalidate_catalog

# Underscore aliases: sibling routes historically imported these from
# chat.py. Keep the names so existing `from ...chat import _X` still
# resolves while those files migrate.
_apply_force_provider = apply_force_provider
_build_503_detail = build_503_detail
_record_health = record_health
_resolve_provider_chain = resolve_provider_chain
_suggestion = suggestion
_try_stream_with_failover = try_stream_with_failover
_env_var_for = env_var_for


logger = logging.getLogger(__name__)
router = APIRouter()



async def _maybe_402(
    cfg,
    *,
    request: Request,
    free_detail: dict,
    error: str = "Free providers exhausted; pay for premium inference",
    body: ChatRequest | None = None,
    ctx: FailoverContext | None = None,
) -> JSONResponse | None:
    """On free exhaustion: daemon auto-pay when payer keys exist, else 402.

    Primary UX for ridex: payer credentials in ``~/.freeride/.env`` mean the
    gateway signs + settles and returns a successful paid response so the
    client never sees crypto. Without payer keys, return classic 402.
    """
    if not cfg.ready:
        return None
    resource_url = str(request.url)

    if body is not None and ctx is not None and has_payer_credentials():
        try:
            _payload, settlement = await auto_pay_settle(cfg, resource_url=resource_url)
            return await _complete_paid_lane(
                request,
                body,
                cfg=cfg,
                ctx=ctx,
                settlement=settlement,
            )
        except X402Error as e:
            logger.warning("x402 auto-pay failed, falling back to 402: %s", e.message)
            free_detail = {
                **(free_detail or {}),
                "auto_pay_error": e.message,
                "detail": e.detail,
            }
            error = f"Auto-pay failed: {e.message}"
        except Exception as e:  # noqa: BLE001
            logger.warning("x402 auto-pay unexpected error, falling back to 402: %s", e)
            free_detail = {**(free_detail or {}), "auto_pay_error": str(e)[:300]}
            error = f"Auto-pay failed: {e}"

    return await build_402_response(
        cfg,
        resource_url=resource_url,
        free_detail=free_detail,
        error=error,
    )


async def _paid_openrouter_chat(
    request: Request,
    body: ChatRequest,
    *,
    api_key: str,
) -> tuple[Provider, object]:
    """Forward one chat completion via OpenRouter using a paid key only.

    Prefer an already-registered openrouter provider instance so headers /
    timeouts match production; otherwise instantiate OpenRouterProvider.
    Does not touch global cooldown state.
    """
    from freeride.providers.openrouter import OpenRouterProvider

    providers: list[Provider] = list(request.app.state.providers)
    chosen = next((p for p in providers if p.name == "openrouter"), None)
    if chosen is None:
        chosen = OpenRouterProvider()
    response = await chosen.forward_chat(body, body.model, api_key)
    return chosen, response


async def _complete_paid_lane(
    request: Request,
    body: ChatRequest,
    *,
    cfg,
    ctx: FailoverContext,
    settlement: dict,
) -> JSONResponse:
    """After settlement: call paid OpenRouter and attach PAYMENT-RESPONSE."""
    api_key = paid_openrouter_key(cfg)
    if not api_key:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="x402_no_paid_key",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "x402_no_paid_upstream_key",
                    "message": (
                        "Payment settled, but no paid OpenRouter key is configured. "
                        "Set FREERIDE_X402_PAID_OPENROUTER_API_KEY (or OPENROUTER_API_KEY)."
                    ),
                    "request_id": ctx.request_id,
                }
            },
        )

    if body.is_streaming():
        # MVP: paid lane is non-streaming for demo reliability.
        body.stream = False

    try:
        chosen_provider, response = await _paid_openrouter_chat(
            request, body, api_key=api_key
        )
    except Exception as e:
        logger.warning("paid OpenRouter forward failed after settle: %s", e)
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="x402_paid_upstream_failed",
            error=str(e)[:200],
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "x402_paid_upstream_failed",
                    "message": f"Paid upstream failed after settlement: {e}",
                    "request_id": ctx.request_id,
                    "settlement": settlement,
                }
            },
        ) from e

    from freeride.core.telemetry import record_request
    from freeride.core.usage import Kind, extract_usage

    usage = extract_usage(Kind.OPENAI, response.model_dump())
    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name,
        streaming=False,
        paid=True,
        input_tokens=usage.input,
        output_tokens=usage.output,
    )
    record_request(
        input_tokens=usage.input,
        output_tokens=usage.output,
        provider=chosen_provider.name,
    )
    out = response.model_dump(exclude_none=False)
    out["_freeride_provider"] = chosen_provider.name
    out["_freeride_request_id"] = ctx.request_id
    out["_freeride_paid"] = "hedera-x402"
    json_resp = JSONResponse(
        content=out,
        headers={
            "X-FreeRide-Provider": chosen_provider.name,
            "X-FreeRide-Request-ID": ctx.request_id,
        },
    )
    return attach_payment_response(json_resp, settlement)


async def _handle_paid_lane(
    request: Request,
    body: ChatRequest,
    *,
    cfg,
    ctx: FailoverContext,
) -> JSONResponse:
    """Verify+settle PAYMENT-SIGNATURE then call paid OpenRouter upstream."""
    raw = extract_payment_header(request.headers)
    if not raw:
        raise RuntimeError("paid lane invoked without payment header")

    try:
        payment_payload = decode_payment_payload(raw)
    except X402Error as e:
        resp = await build_402_response(
            cfg,
            resource_url=str(request.url),
            error=f"Invalid PAYMENT-SIGNATURE: {e.message}",
        )
        return resp

    fee_payer = await resolve_fee_payer(cfg)
    payment_required = build_payment_required(
        cfg, resource_url=str(request.url), fee_payer=fee_payer
    )
    # Prefer requirements embedded in payload.accepted when present.
    accepted = payment_payload.get("accepted")
    if isinstance(accepted, dict) and accepted.get("payTo"):
        payment_requirements = accepted
    else:
        payment_requirements = payment_requirements_from_required(payment_required)

    try:
        settlement = await verify_and_settle(
            cfg,
            payment_payload=payment_payload,
            payment_requirements=payment_requirements,
        )
    except X402Error as e:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="x402_payment_failed",
            error=e.message[:200],
        )
        return await build_402_response(
            cfg,
            resource_url=str(request.url),
            free_detail={"payment_error": e.message, "detail": e.detail},
            error=e.message,
        )

    return await _complete_paid_lane(
        request, body, cfg=cfg, ctx=ctx, settlement=settlement
    )


def _format_sse(event: ChatStreamEvent) -> bytes:
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n".encode("utf-8")


def _format_done() -> bytes:
    return b"data: [DONE]\n\n"


async def _build_stream_response(
    chain: list[tuple[Provider, list[str]]],
    body: ChatRequest,
    cooldown: KeyCooldown,
    ctx: FailoverContext,
) -> StreamingResponse:
    chosen, first_event, rest_or_err = await try_stream_with_failover(
        chain, body, cooldown, ctx, on_model_not_found=invalidate_catalog
    )
    if chosen is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="pre_first_chunk",
            tried=[t.provider for t in ctx.tried],
        )
        raise HTTPException(status_code=503, detail=build_503_detail(ctx))
    rest = rest_or_err  # AsyncIterator at this point

    async def emit() -> AsyncIterator[bytes]:
        from freeride.core.usage import Kind, extract_usage

        last_usage = extract_usage(Kind.OPENAI, first_event.model_dump())
        yield _format_sse(first_event)
        try:
            async for evt in rest:
                u = extract_usage(Kind.OPENAI, evt.model_dump())
                if u.has_any:
                    last_usage = u
                yield _format_sse(evt)
        except Exception as e:
            logger.warning("mid-stream upstream error after first chunk shipped: %s", e)
            emit_event(
                "request_mid_stream_error",
                request_id=ctx.request_id,
                provider=chosen.name,
                error=str(e)[:200],
            )
        emit_event(
            "request_complete",
            request_id=ctx.request_id,
            provider=chosen.name,
            streaming=True,
            input_tokens=last_usage.input,
            output_tokens=last_usage.output,
        )
        from freeride.core.telemetry import record_request

        record_request(
            input_tokens=last_usage.input,
            output_tokens=last_usage.output,
            provider=chosen.name,
        )
        yield _format_done()

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "X-FreeRide-Provider": chosen.name,
            "X-FreeRide-Request-ID": ctx.request_id,
        },
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, body: ChatRequest):
    providers: list[Provider] = sort_by_health(list(request.app.state.providers))

    providers, forced = apply_force_provider(providers, request)
    if forced is not None and not providers:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "type": "force_provider_unknown",
                    "message": f"X-FreeRide-Force-Provider={forced!r} is not a registered provider.",
                    "registered": [p.name for p in request.app.state.providers],
                }
            },
        )

    ctx = FailoverContext(request_id=new_request_id())

    emit_event(
        "request_start",
        request_id=ctx.request_id,
        model=body.model,
        streaming=body.is_streaming(),
    )

    x402_cfg = load_x402_config()
    payment_hdr = extract_payment_header(request.headers)
    if payment_hdr and x402_cfg.enabled:
        if not x402_cfg.pay_to:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "x402_misconfigured",
                        "message": (
                            "x402 enabled and payment presented, but "
                            "FREERIDE_X402_PAY_TO is not set."
                        ),
                        "request_id": ctx.request_id,
                    }
                },
            )
        return await _handle_paid_lane(
            request, body, cfg=x402_cfg, ctx=ctx
        )

    if not providers:
        emit_event("request_failed", request_id=ctx.request_id, phase="no_providers")
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "no_providers",
                    "message": "No provider plugins registered. This is a server-config bug.",
                    "request_id": ctx.request_id,
                }
            },
        )

    cooldown = KeyCooldown()
    chain = resolve_provider_chain(providers)
    if not chain:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="no_usable_keys",
            providers=[p.name for p in providers],
        )
        detail = {
            "error": {
                "type": "no_usable_keys",
                "message": "No providers have usable (non-cooling) API keys for this request.",
                "request_id": ctx.request_id,
                "configured_providers": [p.name for p in providers],
                "suggestion": (
                    "Either set a provider env var (e.g. OPENROUTER_API_KEY) "
                    "or wait for cooldowns to expire."
                ),
            }
        }
        maybe = await _maybe_402(
            x402_cfg, request=request, free_detail=detail, body=body, ctx=ctx
        )
        if maybe is not None:
            return maybe
        raise HTTPException(status_code=503, detail=detail)

    if is_auto_model(body.model):
        catalog = await get_or_fetch_catalog(providers, group=True)
        resolved_id, resolved_provider = resolve_auto_model(providers, catalog)
        if resolved_id is None:
            emit_event(
                "request_failed",
                request_id=ctx.request_id,
                phase="auto_resolution_failed",
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "type": "no_model_available",
                        "message": "model='auto' was requested but no provider has a usable model + key right now.",
                        "request_id": ctx.request_id,
                        "suggestion": "Run `freeride list` to see the catalog and `freeride keys` to see cooldowns.",
                    }
                },
            )
        body.model = resolved_id
        emit_event(
            "auto_model_resolved",
            request_id=ctx.request_id,
            resolved_model=resolved_id,
            resolved_provider=resolved_provider,
        )

    if body.is_streaming():
        try:
            return await _build_stream_response(chain, body, cooldown, ctx)
        except HTTPException as he:
            if he.status_code == 503 and x402_cfg.ready:
                free_detail = he.detail if isinstance(he.detail, dict) else {"error": he.detail}
                maybe = await _maybe_402(
                    x402_cfg,
                    request=request,
                    free_detail=free_detail,
                    body=body,
                    ctx=ctx,
                )
                if maybe is not None:
                    return maybe
            raise

    chosen_provider, response = await try_call_with_failover(
        chain,
        cooldown,
        ctx,
        call=lambda p, k: p.forward_chat(body, body.model, k),
        model=body.model,
        on_model_not_found=invalidate_catalog,
    )

    if response is None or chosen_provider is None:
        emit_event(
            "request_failed",
            request_id=ctx.request_id,
            phase="all_attempts_exhausted",
            tried=[t.provider for t in ctx.tried],
        )
        detail = build_503_detail(ctx)
        maybe = await _maybe_402(
            x402_cfg, request=request, free_detail=detail, body=body, ctx=ctx
        )
        if maybe is not None:
            return maybe
        return JSONResponse(status_code=503, content=detail)

    from freeride.core.telemetry import record_request
    from freeride.core.usage import Kind, extract_usage

    usage = extract_usage(Kind.OPENAI, response.model_dump())
    emit_event(
        "request_complete",
        request_id=ctx.request_id,
        provider=chosen_provider.name,
        streaming=False,
        input_tokens=usage.input,
        output_tokens=usage.output,
    )
    record_request(
        input_tokens=usage.input,
        output_tokens=usage.output,
        provider=chosen_provider.name,
    )
    out = response.model_dump(exclude_none=False)
    out["_freeride_provider"] = chosen_provider.name
    out["_freeride_request_id"] = ctx.request_id
    return JSONResponse(
        content=out,
        headers={
            "X-FreeRide-Provider": chosen_provider.name,
            "X-FreeRide-Request-ID": ctx.request_id,
        },
    )
