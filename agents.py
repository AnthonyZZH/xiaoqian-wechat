"""Social build: agent and handoff execution unavailable."""
from social_policy import disabled
DEFAULT_ROUTES = {}
def default_configs(): return {}
def parse_command(*args, **kwargs): return None
class AgentJobs:
    def __init__(self, *args, **kwargs): pass
    def jobs(self): return []
    def stop(self, *args): return False
    start = disabled
