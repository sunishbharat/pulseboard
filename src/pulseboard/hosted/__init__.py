"""Hosted Profile A: the stateless pass-through HTTP server (ADDENDUM-01 §1A, ADR-022 to ADR-025).

Each request carries the caller's own Jira credentials as headers; nothing is stored beyond
size-capped, short-lived memory caches. Needs the ``hosted`` extra. Local stdio mode never
imports this package.
"""
