"""Distribution pipeline — human-gated multi-channel outreach.

Takes a DistributionEvent (e.g., new release, scheduled outreach tick),
drafts channel-shaped content for each registered ChannelAdapter, queues
the drafts via the shared core.approvals.gateway, and on operator
approval submits to the channel and monitors for replies.

Same trust boundary as commit_pipeline: nothing leaves the host without
an approved card.
"""
