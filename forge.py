"""Social build: no Forge implementation or transport."""
from social_policy import disabled
DEFAULT_PARAMS = {}
start_server = disabled
class Forge:
    def __init__(self, *args, **kwargs): pass
    request = models = idle = load = unload = generate = disabled
