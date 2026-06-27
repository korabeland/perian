"""Memory governor — the spine's residency-policy authority (Perian x ATB merge, U4).

Policy over mechanism: `residency_policy` is the single source of truth for the
residency FACTS (the always-resident keep-set, model identities, the shared
model-slot lock path, and the budget math + proof). The U2 mechanism
(`tools/swap_orchestrator` + `tools/ollama_client`) and the aux watchdog CONSULT
it; they own the load/pin/evict actions, this owns the decisions those actions
must honor.
"""
