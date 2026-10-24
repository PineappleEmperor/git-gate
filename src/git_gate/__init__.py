"""git-gate: GitHub access for sandboxed agents, without the token entering the sandbox."""


class Refused(Exception):
    """A request the rules do not allow. The message is shown to the agent."""
