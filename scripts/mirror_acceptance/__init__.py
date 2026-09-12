"""M5 acceptance harness (read-only probes + matrix runner).

Every module here is *observation only*: it must never write to the production
database, never mutate account rows, and never emit a raw credential. Anything
derived from an account row is reduced to (anonymous ordinal, tier, boolean)
before it leaves this package -- see ``redact.py``.
"""
