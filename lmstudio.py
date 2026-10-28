"""Social build: no local model control or subprocess."""
from social_policy import disabled
class LMStudio:
    def __init__(self, *args, **kwargs): pass
    run = json = models = instances = estimate = ensure = disabled
