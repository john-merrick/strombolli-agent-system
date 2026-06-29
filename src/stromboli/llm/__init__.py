"""The two model surfaces (PRD §4).

* ``gateway`` — the LiteLLM gateway for single structured reasoning calls
  (spec / router / memory) and the non-Claude verifier.
* ``coder`` — the Claude Agent SDK wrapper whose agent loop *is* the inner
  write→test→fix recursion (added in Phase 2).
"""

from stromboli.llm.gateway import (
    Gateway,
    GatewayError,
    LiteLLMGateway,
    build_gateway,
)

__all__ = ["Gateway", "GatewayError", "LiteLLMGateway", "build_gateway"]
