
class KittenUnavailable(Exception):
    pass


def tts(*args, **kwargs):
    raise KittenUnavailable("KittenTTS not installed or not available")
