"""An autonomous synthetic buyer for live production acceptance of a fresh order.

A product-only customer orders a public-channel bot through the actual Codegen
Telegram bot, waits for native work, deploy and QA, checks the live product,
writes redacted evidence and tears down its own project. The operator runbook is
docs/runbooks/synthetic-buyer.md; the entrypoint is `python -m src.synthetic_buyer`.
Importing this package does nothing.
"""
