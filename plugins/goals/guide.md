# Goals

Synth's **self-directed goal store**. It keeps the free-text objectives Synth
authors for itself — inside a game world, or for its own personal life — and
persists them across sessions.

This plugin is generic: it does **not** ship a catalogue of objectives and it
judges nothing. It only *persists* and *recalls* what Synth decides for itself,
optionally with an ordered plan of sub-steps that **auto-completes** when the
last step is passed.

## Scope

Every goal is filed under a three-level scope tuple — all plain strings that
default to `none`:

| Level | Meaning | Example |
|-------|---------|---------|
| `scope` | The broad domain | `vessel`, `none` (personal) |
| `game` | The game/app inside that domain | `minecraft`, `none` |
| `world` | The specific world/instance | a server name, `none` |

`none / none / none` is a **personal life goal** (e.g. *"write a poem about the
sea"*). A Minecraft embodiment goal is filed under `vessel / minecraft / none`.

When Synth is embodied in a world the scope is derived automatically from the
turn's routing metadata (`interface_path` → `vessel/<game>/<world>`). Synth may
also set `scope` / `game` / `world` **explicitly** in the action payload — an
explicit value always wins over the derived one.

## Actions

| Action | Purpose |
|--------|---------|
| `goal_list` | Recall the current active goal and recent goals for a scope. |
| `goal_set` | Declare a new free-text active goal (abandons the previous active goal in the same scope). |
| `goal_update` | Add a note, change status (`done` / `abandoned`), rewrite or advance the ordered plan. |

All three are ordinary **Fast-Lane** actions (`security_level: "low"`, no
`external_effects`) — they never spawn an agentic task.

### Ordered plans and auto-completion

A goal may carry an ordered list of `steps`. When Synth finishes the current
sub-step it calls `goal_update` with `advance: true`; advancing **past the last
step** marks the whole goal `done` automatically. Synth can also rewrite the
whole plan with a new `steps` list or jump to a specific `current_step`
(0-based).

## Storage

Goals live in the `goals` table (`scope`, `game`, `world`, `description`,
`note`, `status`, `steps`, `current_step`, `target_kind`, `target_name`,
`destination`, `created_at`, `updated_at`). At most one goal is `active` per
scope tuple.
