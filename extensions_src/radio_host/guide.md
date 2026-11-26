# AzuraCast Radio DJ

Turns the synth into the live DJ of an [AzuraCast](https://www.azuracast.com/) station.
It watches the station, and around each song change it asks the model for a short spoken
remark, has the synth's voice say it, and streams it into the station through the WebDJ
connection, timed to land in the gap between songs. The same clip plays on the avatar's
stage, so anyone watching sees the synth speak. Every remark goes through the synth's full
context (personality, mood, diary, memories, recent listener chat): it is the synth's
voice, not a generic DJ.

## Setup

You need an AzuraCast instance with an API key that has **Manage Station Broadcasting**
(Administration > Users > Edit User > API Keys) and a WebDJ streamer account on the
station.

1. Switch the extension on and grant what it asks for (Plugins > Store).
2. In its settings fill in **AzuraCast Base URL**, **API Key** and **Station ID** (the
   station shortcode from the station's admin URL), and the **WebDJ Streamer** username and
   password (they must match a streamer account on the station).
3. It starts at once. The **Radio** tab of Activity shows the status and every remark.

## Settings

| Setting | Meaning |
| --- | --- |
| AzuraCast Base URL, API Key, Station ID | The station. All three are needed. |
| WebDJ Streamer Username / Password | The streamer account the remarks are broadcast with (default `SyntH` / `synthradio`). |
| Radio Language | The language of the remarks. |
| Announce Only With Listeners | Stay silent while nobody is listening. |
| Songs Between Comments | Speak every N songs (1 = every song). |
| Track Poll Interval | How often the station is asked what is playing. |
| Listener History Count | How many recent listener messages the model sees. |
| Radio TTS Engine Override | Use a different speech engine for the radio. |
| Audio Gain | Volume boost for the remarks, in dB. |
| Next Song Announcement | Announce the next song too (experimental). Off: it only says goodbye to the song that just ended. |
| On-the-fly Banter Timeout | How long to wait for a slow model before skipping a remark. |

Changes apply within about twenty seconds.

## What the model can do

- **`radio_speak`** (`text`, optional `style`): say something on air. This is the only
  action the model is offered in the turns the radio starts.
- **`radio_update_metadata`** (`artist`, `title`, optional `album`): change the station's
  now-playing metadata.

When someone asks about the radio ("what's playing?", "who's next?"), a Recon question lets
the radio add the live status (station, now playing, next, listeners, schedule) to the
prompt.

## How it fits the platform

- The extension registers one interface, `radio_host`, which is not a chat: its turns are
  background work (`turns.request`, priority `elevated`, scoped to `radio_speak`) that never
  enter the chat history.
- The voice is the Core's (`voice.synthesize`), and the stage mirror is `voice.play`.
- The remarks are logged in the extension's own storage (the last 40) with a small replay
  clip each (the last 30); nothing lives in the Core's database.
- It needs `ffmpeg` on the machine to convert the clips for the WebDJ.
