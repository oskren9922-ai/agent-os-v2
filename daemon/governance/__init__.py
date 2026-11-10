"""Governance: what an agent may do, what needs a human, and the record of all of it.

    policy.py     risk tiers -> allow / wait for approval / deny
    approvals.py  the pending-action queue; approve -> execute exactly once
    limits.py     auto-approve cap and per-client rate limits
    audit.py      append-only audit log writer
"""
