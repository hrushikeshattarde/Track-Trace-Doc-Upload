"""OpenAI-compatible reader/adjudicator for non-Claude models (GPT, Gemini, Llama, ...) via OpenRouter.

Same prompts, same schemas, same matcher. Used only when run.py decides the model slug is not an Anthropic
model; Claude always goes through the Anthropic SDK in reader.py.

Endpoint: OpenRouter's standard OpenAI-compatible API at https://openrouter.ai/api/v1 (note the /v1 here,
unlike the Anthropic-format endpoint). Key: OPENROUTER_API_KEY, or the ANTHROPIC_AUTH_TOKEN already set for
the Anthropic path. Cost comes from OpenRouter's own usage accounting when it returns it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from openai import OpenAI

from .normalize import Document
from .reader import ADJUDICATOR_SYSTEM, READER_SYSTEM, _extract_json
from .schema import Adjudication, Extraction

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


@dataclass
class OpenAIUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost_from_provider: float | None

    @property
    def cost_usd(self) -> float:
        return self.cost_from_provider if self.cost_from_provider is not None else 0.0

    def as_dict(self) -> dict:
        return {"model": self.model, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read, "cache_write_tokens": self.cache_write,
                "estimated_cost_usd": round(self.cost_usd, 5), "cost_source": "provider" if self.cost_from_provider is not None else "unknown"}


def make_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        raise RuntimeError("Set OPENROUTER_API_KEY (or ANTHROPIC_AUTH_TOKEN) to use a non-Claude model through OpenRouter.")
    return OpenAI(api_key=key, base_url=os.environ.get("OPENAI_COMPAT_BASE_URL", OPENROUTER_BASE))


def _page_parts(doc: Document) -> list[dict]:
    return [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{p.b64}", "detail": "high"}} for p in doc.pages]


def _usage(model: str, completion) -> OpenAIUsage:
    u = completion.usage
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
    cost = getattr(u, "cost", None)
    if cost is None and hasattr(u, "model_extra"):
        cost = (u.model_extra or {}).get("cost")
    return OpenAIUsage(model=model, input_tokens=u.prompt_tokens or 0, output_tokens=u.completion_tokens or 0,
                       cache_read=cached, cache_write=0, cost_from_provider=float(cost) if cost is not None else None)


def _structured_call(client: OpenAI, model: str, system: str, parts: list[dict], schema_model, effort: str | None = None):
    """JSON-mode call with the schema in the prompt, validated with Pydantic. Works across OpenRouter models."""
    schema_text = json.dumps(schema_model.model_json_schema())
    messages = [
        {"role": "system", "content": system + "\n\nRespond with ONLY a JSON object that validates against this JSON schema. No prose, no code fences.\n" + schema_text},
        {"role": "user", "content": parts},
    ]
    kwargs = {"model": model, "messages": messages, "max_tokens": 8000,
              "extra_body": {"usage": {"include": True}}}          # ask OpenRouter to return the cost
    if effort:
        kwargs["extra_body"]["reasoning"] = {"effort": effort}     # OpenRouter's unified reasoning control; ignored by models without it
    try:
        completion = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception as e:                                        # some models reject response_format; retry without it
        if "response_format" not in str(e).lower() and "json" not in str(e).lower():
            raise
        completion = client.chat.completions.create(**kwargs)
    text = completion.choices[0].message.content or ""
    try:
        parsed = schema_model.model_validate_json(_extract_json(text))
    except Exception as e:
        raise RuntimeError(f"Model response did not validate against {schema_model.__name__}: {e}\n--- response text (first 600 chars) ---\n{text[:600]}") from e
    return parsed, _usage(model, completion)


def read_document(client: OpenAI, doc: Document, model: str, effort: str | None = None) -> tuple[Extraction, OpenAIUsage]:
    parts = _page_parts(doc)
    if doc.text_layer:
        parts.append({"type": "text", "text": f"The file also carries this typed text layer (likely an app stamp, not part of the printed form):\n{doc.text_layer}"})
    parts.append({"type": "text", "text": f"File name: {doc.path.name}. {len(doc.pages)} page(s). Extract the document per the schema."})
    return _structured_call(client, model, READER_SYSTEM, parts, Extraction, effort)


def adjudicate(client: OpenAI, doc: Document, extraction: Extraction, candidates: list[dict], model: str, effort: str | None = None) -> tuple[Adjudication, OpenAIUsage]:
    parts = _page_parts(doc)
    parts.append({"type": "text", "text": "Structured extraction of this document:\n" + extraction.model_dump_json(indent=1)})
    parts.append({"type": "text", "text": "Candidate loads from the TMS (JSON):\n" + json.dumps(candidates, indent=1)})
    parts.append({"type": "text", "text": "Which candidate does this document belong to, if any? Answer per the schema."})
    return _structured_call(client, model, ADJUDICATOR_SYSTEM, parts, Adjudication, effort)
