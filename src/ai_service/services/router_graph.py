"""The router of `router_service.route()`, rebuilt as a LangGraph graph (Day 8, task 5).

Same flow, same prompts, same helpers (rules, label parser, prompt names, `RouterResult`); only
the orchestration is different: the steps are nodes, and "what comes next" is an edge.

    START -> rules --(intent found)--------------------+
                 \\--(no rule)--> classify --------------+--> answer_known --> END
                                                        +--> answer_large --> END   (unclear)

The model clients are NOT LangChain model objects: each node calls my own `LLMClient` (retry,
fallback, limits, cost logging stay as they are), so LangGraph adds no prompt and no extra call.
`tests/test_router_graph.py` checks that against the plain flow request by request.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from ai_service.core.exceptions import LLMUnavailableError
from ai_service.services.llm_types import (
    CallContext,
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    Usage,
)
from ai_service.services.prompt_registry import get_prompt
from ai_service.services.router_service import (
    _CLASSIFIER_PROMPT_NAME,
    _CLASSIFIER_PROMPT_VERSION,
    _GENERAL_PROMPT_NAME,
    _GENERAL_PROMPT_VERSION,
    _PROMPT_NAME,
    _PROMPT_VERSION,
    KNOWN_INTENTS,
    Intent,
    RouterResult,
    _parse_intent,
    _rule_based_intent,
)

logger = logging.getLogger("ai_service.router_graph")


class RouterState(TypedDict, total=False):
    """Everything the flow knows. Each node returns only the keys it changes."""

    message: str
    intent: Intent
    rule_matched: bool
    classify_latency_ms: float
    classify_usage: Usage | None
    answer: str
    answer_model: str
    answer_backend: str
    answer_latency_ms: float
    answer_usage: Usage | None


@dataclass(frozen=True)
class RouterDeps:
    """What the nodes need besides the state: passed per call, so one compiled graph serves all."""

    small_client: LLMClient
    large_client: LLMClient
    context: CallContext
    max_tokens: int = 512
    large_max_tokens: int = 2000


def _ctx(deps: RouterDeps, feature: str, prompt: Any) -> CallContext:
    c = deps.context
    return CallContext(
        request_id=c.request_id,
        tenant_id=c.tenant_id,
        user_id=c.user_id,
        feature=feature,
        prompt_name=prompt.name,
        prompt_version=prompt.version,
    )


async def rules(state: RouterState) -> RouterState:
    intent = _rule_based_intent(state["message"])
    if intent is None:
        return {"rule_matched": False}
    return {"intent": intent, "rule_matched": True, "classify_latency_ms": 0.0}


async def classify(state: RouterState, runtime: Runtime[RouterDeps]) -> RouterState:
    deps = runtime.context
    prompt = get_prompt(_CLASSIFIER_PROMPT_NAME, _CLASSIFIER_PROMPT_VERSION)
    started = time.perf_counter()
    try:
        response = await deps.small_client.generate(
            LLMRequest(
                messages=[ChatMessage("user", prompt.render(message=state["message"]))],
                max_tokens=20,
                context=_ctx(deps, "router_classify", prompt),
            )
        )
    except LLMError as exc:
        logger.warning("router graph: classifier failed (%s); using unclear", exc.kind)
        return {
            "intent": "unclear",
            "classify_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    return {
        "intent": _parse_intent(response.text),
        "classify_usage": response.usage,
        "classify_latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def _answer(
    state: RouterState,
    client: LLMClient,
    prompt: Any,
    feature: str,
    max_tokens: int,
    deps: RouterDeps,
) -> RouterState:
    started = time.perf_counter()
    try:
        response = await client.generate(
            LLMRequest(
                messages=[
                    ChatMessage("system", prompt.render()),
                    ChatMessage("user", state["message"]),
                ],
                max_tokens=max_tokens,
                context=_ctx(deps, feature, prompt),
            )
        )
    except LLMError as exc:
        logger.warning("router graph: the answer call failed (%s)", exc.kind)
        raise LLMUnavailableError() from exc
    return {
        "answer": response.text,
        "answer_model": response.model,
        "answer_backend": response.backend,
        "answer_usage": response.usage,
        "answer_latency_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def answer_known(state: RouterState, runtime: Runtime[RouterDeps]) -> RouterState:
    deps = runtime.context
    prompt = get_prompt(_PROMPT_NAME[state["intent"]], _PROMPT_VERSION)
    return await _answer(
        state, deps.small_client, prompt, f"router_answer_{state['intent']}", deps.max_tokens, deps
    )


async def answer_large(state: RouterState, runtime: Runtime[RouterDeps]) -> RouterState:
    deps = runtime.context
    prompt = get_prompt(_GENERAL_PROMPT_NAME, _GENERAL_PROMPT_VERSION)
    return await _answer(
        state, deps.large_client, prompt, "router_answer_unclear", deps.large_max_tokens, deps
    )


def after_rules(state: RouterState) -> str:
    """A rule already decided: go to the answer. Otherwise ask the small model."""
    return after_classify(state) if "intent" in state else "classify"


def after_classify(state: RouterState) -> str:
    return "answer_known" if state["intent"] in KNOWN_INTENTS else "answer_large"


def build_router_graph(*, checkpointer: Any = None, interrupt_before: list[str] | None = None):  # type: ignore[no-untyped-def]
    """Compile the graph once; pass `RouterDeps` as the run's context on every call."""
    # LangGraph accepts a state class that has __required_keys__ and __optional_keys__. Every
    # TypedDict has them when the code runs (and mypy accepts this line), but Pyrefly, the
    # editor's checker, does not match any TypedDict to that rule. So only this line is ignored.
    # pyrefly: ignore[bad-specialization]
    graph = StateGraph(RouterState, context_schema=RouterDeps)
    graph.add_node("rules", rules)
    graph.add_node("classify", classify)
    graph.add_node("answer_known", answer_known)
    graph.add_node("answer_large", answer_large)
    graph.add_edge(START, "rules")
    graph.add_conditional_edges("rules", after_rules, ["classify", "answer_known", "answer_large"])
    graph.add_conditional_edges("classify", after_classify, ["answer_known", "answer_large"])
    graph.add_edge("answer_known", END)
    graph.add_edge("answer_large", END)
    return graph.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)


ROUTER_GRAPH = build_router_graph()


def result_from_state(state: RouterState, total_latency_ms: float) -> RouterResult:
    return RouterResult(
        intent=state["intent"],
        rule_matched=state["rule_matched"],
        answer=state["answer"],
        answer_model=state["answer_model"],
        answer_backend=state["answer_backend"],
        classify_latency_ms=state.get("classify_latency_ms", 0.0),
        answer_latency_ms=state["answer_latency_ms"],
        total_latency_ms=total_latency_ms,
        classify_usage=state.get("classify_usage"),
        answer_usage=state.get("answer_usage"),
    )


async def route_with_graph(
    message: str,
    *,
    small_client: LLMClient,
    large_client: LLMClient,
    context: CallContext | None = None,
    max_tokens: int = 512,
    large_max_tokens: int = 2000,
) -> RouterResult:
    """Same signature and result as `router_service.route()`, run through the graph."""
    deps = RouterDeps(
        small_client,
        large_client,
        context or CallContext(feature="router"),
        max_tokens,
        large_max_tokens,
    )
    started = time.perf_counter()
    # ainvoke is typed as returning a plain dict; at the end of the graph it holds the state.
    state = cast(RouterState, await ROUTER_GRAPH.ainvoke({"message": message}, context=deps))
    return result_from_state(state, round((time.perf_counter() - started) * 1000, 1))
