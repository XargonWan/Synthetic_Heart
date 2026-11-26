# Fish Audio protocol

Adds the **Fish Audio** protocol to SyntH's external endpoints: cloud text-to-speech with
the voices of your Fish Audio account and the public library.

## What it does

- **Speech**: sends the text to `https://api.fish.audio/v1/tts` with your key, the model tier
  (`tts_model`, default `s2.1-pro-free`), the format (`tts_output_format`: wav, mp3 or pcm) and
  the chosen voice (`tts_reference_id`). Long replies get up to 180 seconds.
- **Voices**: the picker lists, in this order, the voices you added by URL, your own cloned
  voices, and the popular public ones. "Add a voice by URL" reads a fish.audio share link.
- It reports a fixed *voice* capability and does not spend credits to probe.

## Permission

It asks for **endpoints.protocol**: it receives the API key of the endpoints that use the
`fish` protocol, and of no others, because it needs it to call Fish Audio.

Without it, a Fish endpoint stays in the list but cannot speak; install and switch it on and
its voice engine appears again at once.
