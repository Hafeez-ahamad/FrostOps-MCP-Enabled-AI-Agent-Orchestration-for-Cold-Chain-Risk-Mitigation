"""LLM-backed roles. Each returns a validated answer or None; None means "use the deterministic
path" and the caller marks the step degraded. None of them can execute or approve anything."""
