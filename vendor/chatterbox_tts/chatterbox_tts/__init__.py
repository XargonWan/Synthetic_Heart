class ChatterboxUnavailable(Exception):
    pass


def speak(*args, **kwargs):
    raise ChatterboxUnavailable("Chatterbox TTS not installed or not available")
