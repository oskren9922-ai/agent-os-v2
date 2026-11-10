"""Sentinel: the agents that watch a merchant's PayPal account and act on it.

    llm.py                 Anthropic client and the tool-use loop
    toolbridge.py          registry functions -> Claude tools, filtered per agent
    orchestrator.py        event -> the right specialist
    dispute_resolver.py    gather proof -> fight / offer / accept
    collections.py         client profile -> gentle note / reminder / discount
    subscription_rescue.py failed subscription payment -> retry or hand to a human
    profiles.py            per-client payment profile
    schemas.py             the decision each agent must return
    prompts/               the system prompts

Agents never call PayPal. They call registry functions through toolbridge.py,
which goes through daemon/invocation.py, so governance applies to every call.
"""
