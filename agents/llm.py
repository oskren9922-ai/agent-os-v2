"""The Claude client and the tool-use loop.

How one agent run works with Claude:

    1. We send the system prompt, the event, and a list of tools. The tools are
       this agent's READ functions from the registry plus one special tool,
       `submit_decision`, whose input schema is the decision model.
    2. Claude answers with tool calls. We run each one through the tool bridge
       (so governance and the audit log apply) and send the results back.
    3. This repeats until Claude calls `submit_decision`. Its input is validated
       against the Pydantic model. If it does not validate, Claude is told why
       and tries again.

Claude never gets the write functions as tools. It decides; plain code turns
the decision into the one action call, which governance may park for approval.

Without ANTHROPIC_API_KEY, `get_llm` returns None and the agents fall back to
their built-in rules.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from daemon.config import Settings

log = logging.getLogger("agentos.llm")

FINAL_TOOL = "submit_decision"
MAX_TOOL_RESULT_CHARS = 12000


class LLMError(RuntimeError):
    """The model run could not produce a valid decision."""


class LLMResult(BaseModel):
    decision: Any
    model: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0


def _block_to_dict(block: Any) -> dict[str, Any]:
    """Turn an SDK content block into the plain dict the API accepts back."""
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": block.text}
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


class ClaudeLLM:
    def __init__(self, api_key: str, model: str, max_tokens: int = 2000, client: Any = None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=60.0)
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def run(self, *, system: str, user: str, tools: list[dict], execute_tool: Callable[[str, dict], Any],
            decision_model: type[BaseModel], decision_schema: dict, max_turns: int = 8) -> LLMResult:
        all_tools = [*tools, {
            "name": FINAL_TOOL,
            "description": "Submit your final decision. Call this exactly once, when you have the facts you need.",
            "input_schema": decision_schema,
        }]
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        tokens_in = tokens_out = 0

        for turn in range(1, max_turns + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=all_tools,
                tool_choice={"type": "any"},  # every turn must be a tool call; the last one is submit_decision
                messages=messages,
            )
            usage = getattr(response, "usage", None)
            tokens_in += getattr(usage, "input_tokens", 0) or 0
            tokens_out += getattr(usage, "output_tokens", 0) or 0

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in response.content]})
            if not tool_uses:
                messages.append({"role": "user", "content": f"Call a tool. When you are done, call {FINAL_TOOL}."})
                continue

            results: list[dict[str, Any]] = []
            accepted: BaseModel | None = None
            for use in tool_uses:
                if use.name == FINAL_TOOL:
                    try:
                        accepted = decision_model.model_validate(use.input)
                        content, is_error = "Decision accepted.", False
                    except ValidationError as exc:
                        content, is_error = f"Decision rejected, fix and resubmit: {exc}", True
                else:
                    try:
                        output = execute_tool(use.name, use.input if isinstance(use.input, dict) else {})
                        content, is_error = json.dumps(output, default=str)[:MAX_TOOL_RESULT_CHARS], False
                    except Exception as exc:  # the model should see a tool failure, not crash the run
                        content, is_error = f"Tool failed: {exc}", True
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": content,
                                "is_error": is_error})
            if accepted is not None:
                return LLMResult(decision=accepted, model=self.model, turns=turn,
                                 input_tokens=tokens_in, output_tokens=tokens_out)
            messages.append({"role": "user", "content": results})

        raise LLMError(f"no valid decision after {max_turns} turns")


def get_llm(settings: Settings) -> ClaudeLLM | None:
    """The Claude client, or None when no API key is configured."""
    if not settings.llm_enabled:
        return None
    try:
        return ClaudeLLM(settings.anthropic_api_key, settings.anthropic_model, settings.anthropic_max_tokens)
    except Exception:
        log.exception("could not create the Anthropic client; agents will use the rules engine")
        return None
