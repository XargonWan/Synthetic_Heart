TTS Lip Sync Plugin
===================

.. versionadded:: 1.1.0
   Introduced custom TTS and LipSync integration with support for multiple backend servers.

The ``tts_lipsync`` plugin enables Synthetic Heart to generate spoken audio from text using external TTS servers. It integrates with the Web UI to play audio and drive lip-sync animations (via the ``synth:tts-play`` event).

Architecture
------------

The plugin is designed for reliability and quality:

1.  **Dual-Server Fallback**: Configured with a primary and a secondary/fallback TTS server. If the primary fails or times out, the system automatically tries the fallback server.
2.  **Audio Sanitization**: Automatically strips emojis and incompatible characters to ensure smooth TTS generation.
3.  **Emotion Mapping**: Maps standard emotions (happy, sad, etc.) to 8-dimensional emotion vectors supported by the IndexTTS2 backend.
4.  **Web UI Integration**: Generated audio is broadcast to the Web UI via WebSocket, triggering immediate playback and lip movement.

Configuration
-------------

Currently, server configuration is defined in ``plugins/tts_lipsync.py``.

- **Primary Server**: ``http://192.168.1.6:8001/tts_stream`` (Remote)
- **Fallback Server**: ``http://192.168.1.69:8001/tts_stream`` (Local)

Each server configuration includes a ``ref_path`` pointing to the reference audio file used for voice cloning on that specific machine.

.. code-block:: python

    self.tts_servers = [
        {
            "url": "http://192.168.1.6:8001/tts_stream",
            "ref_path": r"C:\Users\EVO\Documents\ai2\index-tts\index-tts-training_v2\audio\reference\2b_ref.wav"
        },
        {
            "url": "http://192.168.1.69:8001/tts_stream",
            "ref_path": r"F:\0synth\0synth\reference\2b_ref.wav"
        }
    ]

Supported Actions
-----------------

**tts_speak**
    Generate speech from text. This action is REQUIRED for the user to hear the response.

    .. code-block:: json

       {
         "type": "tts_speak",
         "payload": {
           "text": "Hello, I am ready to help.",
           "emotion": "happy"
         }
       }

    **Parameters:**
    
    *   ``text`` (string): The text to speak.
    *   ``emotion`` (string, optional): One of ``happy``, ``sad``, ``angry``, ``curious``, ``neutral``. Defaults to ``neutral``.

Emotion Support
---------------

The plugin maps high-level emotion keywords to specific vectors:

*   **happy**: ``[1.0, 0.0, ...]``
*   **sad**: ``[0.0, 0.0, 1.0, ...]``
*   **angry**: ``[0.0, 1.0, ...]``
*   **afraid**: ``[0.0, 0.0, 0.0, 1.0, ...]``
*   **disgusted**: ``[0.0, ... 1.0, ...]``
*   **melancholic**: ``[0.0, ... 1.0, 0.0, 0.0]``
*   **calm/neutral**: ``[0.0, ... 1.0]``
*   **curious**: Maps to "surprised" vector.

Usage
-----

To make the persona speak, simply include the ``tts_speak`` action in the LLM's response. The system handles generation, saving the WAV file to ``res/synth_webui/static/audio/tts/``, and broadcasting the playback event.
