
class HarmonySpeechUnavailable(Exception):
    pass


def generate_tts(*args, **kwargs):
    raise HarmonySpeechUnavailable("Harmony engine not installed or not available")
