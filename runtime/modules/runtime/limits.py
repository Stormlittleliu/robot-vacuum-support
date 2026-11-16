"""Technical engine guard, separate from opt-in run budgets."""
import os


def engine_recursion_limit():
    value = int(os.environ.get("AH_ENGINE_RECURSION_LIMIT", "4096"))
    if value < 1:
        raise ValueError("AH_ENGINE_RECURSION_LIMIT must be a positive integer")
    return value


def token_limit(scenario):
    resolve = getattr(scenario, "resolve_execution", None)
    execution = resolve() if callable(resolve) else None
    for policy in getattr(execution, "policies", None) or []:
        name = policy.get("name") if isinstance(policy, dict) else policy.name
        enabled = policy.get("enabled", True) if isinstance(policy, dict) else policy.enabled
        if name == "budget" and not enabled:
            return None
    return scenario.token_budget
