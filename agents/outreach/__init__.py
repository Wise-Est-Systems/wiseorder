"""Outreach agents — LLM-backed drafters for the distribution pipeline.

OutreachDrafter generates channel-shaped drafts (HN title + first comment,
cold-reach email subject + body) from a DistributionEvent.

Mirrors the LLM-call pattern in agents/engineering/summarizer (litellm
acompletion, bounded retries, JSON-parsed output).
"""
