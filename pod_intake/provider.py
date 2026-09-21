"""Where the model calls go: Anthropic direct, AWS Bedrock, or an OpenRouter proxy.

One place decides this, because the choice touches three things that have to agree - which client
class is built, what the model is called on that platform, and what a token costs there - and the
service had them scattered across run.py, ingest.py and reader.py with the answer implied by which
environment variables happened to be set.

Selection, highest precedence first:

    INTAKE_MODEL_PROVIDER = bedrock | anthropic | openrouter    explicit, and what a server should set
    AWS_PROFILE or AWS_REGION set, and no ANTHROPIC_* key       bedrock
    ANTHROPIC_BASE_URL points at openrouter.ai                  openrouter
    otherwise                                                   anthropic

Bedrock notes that cost money to learn the hard way:

  * The client is AnthropicBedrockMantle, the Messages-API endpoint. The older AnthropicBedrock
    class is the bedrock-runtime InvokeModel path and is not what new code should use.
  * Model ids carry an "anthropic." prefix there: claude-opus-5 is anthropic.claude-opus-5. The
    bare id is rejected, so the mapping is applied here rather than asked of every caller.
  * Credentials come from the AWS chain, which means an SSO profile works - but only with botocore
    installed. The SDK does not depend on it; `pip install "anthropic[bedrock]"` adds it.
  * Bedrock is partner-operated and priced separately from the Anthropic API. The rates below are
    the first-party ones, kept as an ESTIMATE so spend reporting keeps working, and every cost the
    service records on Bedrock is an estimate until somebody puts AWS's own numbers in.
"""
from __future__ import annotations

import os

ANTHROPIC = "anthropic"
BEDROCK = "bedrock"
OPENROUTER = "openrouter"

# Bedrock takes the first-party id with a vendor prefix. Built rather than hard-coded per model so a
# model this project has never run still resolves correctly.
BEDROCK_PREFIX = "anthropic."


def provider() -> str:
    """Which platform this process should call."""
    explicit = (os.environ.get("INTAKE_MODEL_PROVIDER") or "").strip().lower()
    if explicit in (ANTHROPIC, BEDROCK, OPENROUTER):
        return explicit
    base = (os.environ.get("ANTHROPIC_BASE_URL") or "").lower()
    if "openrouter.ai" in base:
        return OPENROUTER
    has_anthropic_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if (os.environ.get("AWS_PROFILE") or os.environ.get("AWS_REGION")) and not has_anthropic_key:
        return BEDROCK
    return ANTHROPIC


def model_id(model: str, on: str | None = None) -> str:
    """The model's id on the target platform.

    Callers keep saying "claude-opus-5" everywhere - the CLI flags, the ledger's `model` column, the
    cost table - and only the id sent over the wire changes. Already-prefixed ids are left alone so
    an explicit `anthropic.claude-opus-5` in a config is not mangled into a double prefix.
    """
    on = on or provider()
    if on != BEDROCK:
        return model
    return model if model.startswith(BEDROCK_PREFIX) else BEDROCK_PREFIX + model


def base_model(model: str) -> str:
    """The first-party name, whatever platform id came in. What the cost table is keyed by."""
    return model[len(BEDROCK_PREFIX):] if model.startswith(BEDROCK_PREFIX) else model


def make_client(on: str | None = None):
    """Build the client for the chosen platform. Returns (client, provider_name).

    Nothing here reads a document or spends anything; constructing a client makes no API call.
    """
    on = on or provider()
    if on == BEDROCK:
        from anthropic import AnthropicBedrockMantle

        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        profile = os.environ.get("AWS_PROFILE")
        if not region:
            raise RuntimeError(
                "Bedrock needs a region: set AWS_REGION (the paybot-admin profile is us-east-1).")
        try:
            # aws_profile is passed explicitly rather than left to the environment so that the
            # failure when botocore is missing names the fix instead of surfacing as a credential
            # error somewhere deeper.
            return AnthropicBedrockMantle(aws_region=region, aws_profile=profile), BEDROCK
        except Exception as e:  # noqa: BLE001 - the useful part is telling the operator what to install
            raise RuntimeError(
                f"Could not build the Bedrock client ({type(e).__name__}: {e}). "
                "An SSO profile needs botocore: pip install 'anthropic[bedrock]'. "
                "Then `aws sso login --profile " + (profile or "<profile>") + "`.") from None
    if on == OPENROUTER:
        import anthropic

        return anthropic.Anthropic(), OPENROUTER      # ANTHROPIC_BASE_URL already points at it
    import anthropic

    return anthropic.Anthropic(), ANTHROPIC


def describe() -> str:
    """One line for a log or a --help, naming the platform without printing a secret."""
    on = provider()
    if on == BEDROCK:
        return (f"bedrock: region {os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')}, "
                f"profile {os.environ.get('AWS_PROFILE') or '(default credential chain)'}")
    if on == OPENROUTER:
        return "openrouter (ANTHROPIC_BASE_URL points at openrouter.ai)"
    return "anthropic api"
