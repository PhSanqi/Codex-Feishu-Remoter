# CFR Model Registry

## Goal

CFR does not own a hardcoded list of Codex models. The installed Codex
`model/list` RPC is the availability authority for the Code Surface.

`src/cfr/control/model_registry.py` is the only normalization layer between
native Codex model metadata and CFR's Control/Feishu projections. Model slugs,
reasoning-effort values, service tiers, and multi-agent versions are discovered
at runtime.

## New-model flow

When a new model appears in the installed Codex runtime:

1. `codex_catalog.model_catalog()` reads `model/list`.
2. `project_runtime_model()` normalizes stable fields and preserves unknown
   native fields under `extensions`.
3. Control Center and Feishu `/models` consume the same normalized catalog.
4. Model, reasoning effort, and service tier writes are validated against that
   catalog before CFR sends them to Codex.

No CFR patch is required solely because a model slug changes or a new reasoning
effort is added.

## GPT-6 Astra boundary

As of 2026-09-04, the locally installed Codex runtime on this machine does not
yet advertise `gpt-6-astra` in `model/list`. CFR therefore does not invent an
Astra option. Once the installed Codex runtime exposes it, the existing registry
will make it selectable without an Astra-specific code branch.

Astra capabilities documented for the OpenAI Responses API, such as async tool
calling, are not reimplemented inside CFR's Codex Surface. CFR uses Codex
app-server as the execution authority, so tool scheduling remains owned by the
installed Codex runtime. Mid-turn steering already maps to Codex `turn/steer`.

## Compatibility contract

- `model`, `id`, and display name can resolve a model for user-facing commands.
- Programmatic/default writes resolve by native model/id, not presentation text.
- Supported reasoning efforts and service tiers remain ordered exactly as the
  runtime reports them.
- Known upgrade, availability, modality, personality, and multi-agent metadata
  are projected explicitly.
- Unknown future metadata is preserved in `extensions` for diagnostics, but CFR
  does not treat an unknown field as an implemented behavior until there is an
  execution-path contract for it.

This keeps future model rollout cheap without turning the registry into a plugin
framework that has no current second implementation.
