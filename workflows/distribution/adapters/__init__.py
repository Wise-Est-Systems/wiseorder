"""Channel adapters for the distribution pipeline.

Each adapter implements ChannelAdapter (see base.py): draft(), submit(),
monitor(). v0.1 ships only adapters that fully function:

  - hacker_news     : Playwright-based; HN has no submit API
  - email_outreach  : SMTP send + IMAP reply-monitor

Additional channels (Reddit, Lobste.rs, etc.) ship as their own
ChannelAdapter subclasses when their auth / invite requirements are met.
There are no scaffold-only adapters in the registry by design.
"""
