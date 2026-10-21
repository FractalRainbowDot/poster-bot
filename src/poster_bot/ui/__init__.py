"""Owner-facing Telegram UI (phase 2): concise Russian conversation in the
owner's private chat with durable state.

Layering: ``flow`` is the transport-free conversation engine (everything the
owner can do is decided and validated here against durable state); the aiogram
``handlers`` module is a thin adapter that normalizes Telegram updates into
flow events and renders flow replies. Storage-level rules (state machine,
minute uniqueness, limits) live in the domain/repo layers and are re-checked
on every execution — UI state never bypasses them.
"""