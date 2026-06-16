<!-- source: https://ai.google.dev/gemini-api/docs/live-api/capabilities -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Live API capabilities guide

This is a comprehensive guide that covers capabilities and configurations available with the Live API. See [Get started with Live API](/gemini-api/docs/live) page for an overview and sample code for common use cases.

## Before you begin

  * **Familiarize yourself with core concepts:** If you haven't already done so, read the [Get started with Live API ](/gemini-api/docs/live) page first. This will introduce you to the fundamental principles of the Live API, how it works, and the different [implementation approaches](/gemini-api/docs/live#implementation-approach).
  * **Try the Live API in AI Studio:** You may find it useful to try the Live API in [Google AI Studio](https://aistudio.google.com/app/live) before you start building. To use the Live API in Google AI Studio, select **Stream**.



## Model comparison

The following table summarizes the key differences between the [Gemini 3.1 Flash Live Preview](/gemini-api/docs/models/gemini-3.1-flash-live-preview) and the [Gemini 2.5 Flash Live Preview](/gemini-api/docs/models/gemini-2.5-flash-native-audio-preview-12-2025) models:

Feature | Gemini 3.1 Flash Live Preview | Gemini 2.5 Flash Live Preview  
---|---|---  
**Thinking** | Uses `thinkingLevel` to control thinking depth with settings like `minimal`, `low`, `medium`, and `high`. Defaults to `minimal` to optimize for lowest latency. See [Thinking levels and budgets](/gemini-api/docs/thinking#levels-budgets). | Uses `thinkingBudget` to set the number of thinking tokens. Dynamic thinking is enabled by default. Set `thinkingBudget` to `0` to disable. See [Thinking levels and budgets](/gemini-api/docs/thinking#levels-budgets).  
**[Receiving response](/api/live#bidigeneratecontentservercontent)** | A single server event can contain multiple content parts simultaneously (for example, `inlineData` and transcript). Ensure your code processes all parts in each event to avoid missing content. | Each server event contains only one content part. Parts are delivered in separate events.  
**Client content** | `send_client_content` is only supported for seeding initial context history (requires setting `initial_history_in_client_content` in session config). To send text updates during the conversation, use `send_realtime_input` instead. | `send_client_content` is supported throughout the conversation for sending incremental content updates and establishing context.  
**[Turn coverage](/api/live#turncoverage)** | Defaults to `TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO`. The model's turn includes detected audio activity and all video frames. | Defaults to `TURN_INCLUDES_ONLY_ACTIVITY`. The model's turn includes only the detected activity.  
**Custom VAD** (`activity_start`/`activity_end`) | Supported. Disable automatic VAD and send `activityStart` and `activityEnd` messages manually to control turn boundaries. | Supported. Disable automatic VAD and send `activityStart` and `activityEnd` messages manually to control turn boundaries.  
**Automatic VAD configuration** | Supported. Configure parameters such as `start_of_speech_sensitivity`, `end_of_speech_sensitivity`, `prefix_padding_ms`, and `silence_duration_ms`. | Supported. Configure parameters such as `start_of_speech_sensitivity`, `end_of_speech_sensitivity`, `prefix_padding_ms`, and `silence_duration_ms`.  
**[Asynchronous function calling](/gemini-api/docs/live-tools#async-function-calling)** (`behavior: NON_BLOCKING`) | Not supported. Function calling is sequential only. The model will not start responding until you've sent the tool response. | Supported. Set `behavior` to `NON_BLOCKING` on a function declaration to let the model continue interacting while the function runs. Control how the model handles responses with the `scheduling` parameter (`INTERRUPT`, `WHEN_IDLE`, or `SILENT`).  
**Proactive audio** | Not supported | Supported. When enabled, the model can proactively decide not to respond if the input content is not relevant. Set `proactive_audio` to `true` in the `proactivity` config (requires `v1alpha`).  
**Affective dialogue** | Not supported | Supported. The model adapts its response style to match the expression and tone of the input. Set `enable_affective_dialog` to `true` in session config (requires `v1alpha`).  
  
To migrate from Gemini 2.5 Flash Live to Gemini 3.1 Flash Live, see the [migration guide](/gemini-api/docs/models/gemini-3.1-flash-live-preview#migrating).

## Establishing a connection

The following example shows how to create a connection with an API key:

### Python
    
    
    import asyncio
    from google import genai
    
    client = genai.Client()
    
    model = "gemini-3.1-flash-live-preview"
    config = {"response_modalities": ["AUDIO"]}
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            print("Session started")
            # Send content...
    
    if __name__ == "__main__":
        asyncio.run(main())
    

### JavaScript
    
    
    import { GoogleGenAI, Modality } from '@google/genai';
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    const config = { responseModalities: [Modality.AUDIO] };
    
    async function main() {
    
      const session = await ai.live.connect({
        model: model,
        callbacks: {
          onopen: function () {
            console.debug('Opened');
          },
          onmessage: function (message) {
            console.debug(message);
          },
          onerror: function (e) {
            console.debug('Error:', e.message);
          },
          onclose: function (e) {
            console.debug('Close:', e.reason);
          },
        },
        config: config,
      });
    
      console.debug("Session started");
      // Send content...
    
      session.close();
    }
    
    main();
    

## Interaction modalities

The following sections provide examples and supporting context for the different input and output modalities available in Live API.

### Sending audio

Audio needs to be sent as raw PCM data (raw 16-bit PCM audio, 16kHz, little-endian).

### Python
    
    
    # Assuming 'chunk' is your raw PCM audio bytes
    await session.send_realtime_input(
        audio=types.Blob(
            data=chunk,
            mime_type="audio/pcm;rate=16000"
        )
    )
    

### JavaScript
    
    
    // Assuming 'chunk' is a Buffer of raw PCM audio
    session.sendRealtimeInput({
      audio: {
        data: chunk.toString('base64'),
        mimeType: 'audio/pcm;rate=16000'
      }
    });
    

### Audio formats

Audio data in the Live API is always raw, little-endian, 16-bit PCM. Audio output always uses a sample rate of 24kHz. Input audio is natively 16kHz, but the Live API will resample if needed so any sample rate can be sent. To convey the sample rate of input audio, set the MIME type of each audio-containing [Blob](/api/caching#Blob) to a value like `audio/pcm;rate=16000`.

### Receiving audio

The model's audio responses are received as chunks of data.

### Python
    
    
    async for response in session.receive():
        if response.server_content and response.server_content.model_turn:
            for part in response.server_content.model_turn.parts:
                if part.inline_data:
                    audio_data = part.inline_data.data
                    # Process or play the audio data
    

### JavaScript
    
    
    // Inside the onmessage callback
    const content = response.serverContent;
    if (content?.modelTurn?.parts) {
      for (const part of content.modelTurn.parts) {
        if (part.inlineData) {
          const audioData = part.inlineData.data;
          // Process or play audioData (base64 encoded string)
        }
      }
    }
    

### Sending text

Text can be sent using `send_realtime_input` (Python) or `sendRealtimeInput` (JavaScript).

### Python
    
    
    await session.send_realtime_input(text="Hello, how are you?")
    

### JavaScript
    
    
    session.sendRealtimeInput({
      text: 'Hello, how are you?'
    });
    

### Sending video

Video frames are sent as individual images (e.g., JPEG or PNG) at a specific frame rate (max 1 frame per second).

### Python
    
    
    # Assuming 'frame' is your JPEG-encoded image bytes
    await session.send_realtime_input(
        video=types.Blob(
            data=frame,
            mime_type="image/jpeg"
        )
    )
    

### JavaScript
    
    
    // Assuming 'frame' is a Buffer of JPEG-encoded image data
    session.sendRealtimeInput({
      video: {
        data: frame.toString('base64'),
        mimeType: 'image/jpeg'
      }
    });
    

#### Incremental content updates

Use incremental updates to send text input, establish session context, or restore session context. For short contexts you can send turn-by-turn interactions to represent the exact sequence of events:

### Python
    
    
    turns = [
        {"role": "user", "parts": [{"text": "What is the capital of France?"}]},
        {"role": "model", "parts": [{"text": "Paris"}]},
    ]
    
    await session.send_client_content(turns=turns, turn_complete=False)
    
    turns = [{"role": "user", "parts": [{"text": "What is the capital of Germany?"}]}]
    
    await session.send_client_content(turns=turns, turn_complete=True)
    

### JavaScript
    
    
    let inputTurns = [
      { "role": "user", "parts": [{ "text": "What is the capital of France?" }] },
      { "role": "model", "parts": [{ "text": "Paris" }] },
    ]
    
    session.sendClientContent({ turns: inputTurns, turnComplete: false })
    
    inputTurns = [{ "role": "user", "parts": [{ "text": "What is the capital of Germany?" }] }]
    
    session.sendClientContent({ turns: inputTurns, turnComplete: true })
    

For longer contexts it's recommended to provide a single message summary to free up the context window for subsequent interactions. See [Session Resumption](/gemini-api/docs/live-session#session-resumption) for another method for loading session context.

### Audio transcriptions

In addition to the model response, you can also receive transcriptions of both the audio output and the audio input.

To enable transcription of the model's audio output, send `output_audio_transcription` in the setup config. The transcription language is inferred from the model's response.

### Python
    
    
    import asyncio
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    model = "gemini-3.1-flash-live-preview"
    
    config = {
        "response_modalities": ["AUDIO"],
        "output_audio_transcription": {}
    }
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            message = "Hello? Gemini are you there?"
    
            await session.send_client_content(
                turns={"role": "user", "parts": [{"text": message}]}, turn_complete=True
            )
    
            async for response in session.receive():
                if response.server_content.model_turn:
                    print("Model turn:", response.server_content.model_turn)
                if response.server_content.output_transcription:
                    print("Transcript:", response.server_content.output_transcription.text)
    
    if __name__ == "__main__":
        asyncio.run(main())
    

### JavaScript
    
    
    import { GoogleGenAI, Modality } from '@google/genai';
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    
    const config = {
      responseModalities: [Modality.AUDIO],
      outputAudioTranscription: {}
    };
    
    async function live() {
      const responseQueue = [];
    
      async function waitMessage() {
        let done = false;
        let message = undefined;
        while (!done) {
          message = responseQueue.shift();
          if (message) {
            done = true;
          } else {
            await new Promise((resolve) => setTimeout(resolve, 100));
          }
        }
        return message;
      }
    
      async function handleTurn() {
        const turns = [];
        let done = false;
        while (!done) {
          const message = await waitMessage();
          turns.push(message);
          if (message.serverContent && message.serverContent.turnComplete) {
            done = true;
          }
        }
        return turns;
      }
    
      const session = await ai.live.connect({
        model: model,
        callbacks: {
          onopen: function () {
            console.debug('Opened');
          },
          onmessage: function (message) {
            responseQueue.push(message);
          },
          onerror: function (e) {
            console.debug('Error:', e.message);
          },
          onclose: function (e) {
            console.debug('Close:', e.reason);
          },
        },
        config: config,
      });
    
      const inputTurns = 'Hello how are you?';
      session.sendClientContent({ turns: inputTurns });
    
      const turns = await handleTurn();
    
      for (const turn of turns) {
        if (turn.serverContent && turn.serverContent.outputTranscription) {
          console.debug('Received output transcription: %s\n', turn.serverContent.outputTranscription.text);
        }
      }
    
      session.close();
    }
    
    async function main() {
      await live().catch((e) => console.error('got error', e));
    }
    
    main();
    

To enable transcription of the model's audio input, send `input_audio_transcription` in setup config.

### Python
    
    
    import asyncio
    from pathlib import Path
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    model = "gemini-3.1-flash-live-preview"
    
    config = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {},
    }
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            audio_data = Path("16000.pcm").read_bytes()
    
            await session.send_realtime_input(
                audio=types.Blob(data=audio_data, mime_type='audio/pcm;rate=16000')
            )
    
            async for msg in session.receive():
                if msg.server_content.input_transcription:
                    print('Transcript:', msg.server_content.input_transcription.text)
    
    if __name__ == "__main__":
        asyncio.run(main())
    

### JavaScript
    
    
    import { GoogleGenAI, Modality } from '@google/genai';
    import * as fs from "node:fs";
    import pkg from 'wavefile';
    const { WaveFile } = pkg;
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    
    const config = {
      responseModalities: [Modality.AUDIO],
      inputAudioTranscription: {}
    };
    
    async function live() {
      const responseQueue = [];
    
      async function waitMessage() {
        let done = false;
        let message = undefined;
        while (!done) {
          message = responseQueue.shift();
          if (message) {
            done = true;
          } else {
            await new Promise((resolve) => setTimeout(resolve, 100));
          }
        }
        return message;
      }
    
      async function handleTurn() {
        const turns = [];
        let done = false;
        while (!done) {
          const message = await waitMessage();
          turns.push(message);
          if (message.serverContent && message.serverContent.turnComplete) {
            done = true;
          }
        }
        return turns;
      }
    
      const session = await ai.live.connect({
        model: model,
        callbacks: {
          onopen: function () {
            console.debug('Opened');
          },
          onmessage: function (message) {
            responseQueue.push(message);
          },
          onerror: function (e) {
            console.debug('Error:', e.message);
          },
          onclose: function (e) {
            console.debug('Close:', e.reason);
          },
        },
        config: config,
      });
    
      // Send Audio Chunk
      const fileBuffer = fs.readFileSync("16000.wav");
    
      // Ensure audio conforms to API requirements (16-bit PCM, 16kHz, mono)
      const wav = new WaveFile();
      wav.fromBuffer(fileBuffer);
      wav.toSampleRate(16000);
      wav.toBitDepth("16");
      const base64Audio = wav.toBase64();
    
      // If already in correct format, you can use this:
      // const fileBuffer = fs.readFileSync("sample.pcm");
      // const base64Audio = Buffer.from(fileBuffer).toString('base64');
    
      session.sendRealtimeInput(
        {
          audio: {
            data: base64Audio,
            mimeType: "audio/pcm;rate=16000"
          }
        }
      );
    
      const turns = await handleTurn();
      for (const turn of turns) {
        if (turn.text) {
          console.debug('Received text: %s\n', turn.text);
        }
        else if (turn.data) {
          console.debug('Received inline data: %s\n', turn.data);
        }
        else if (turn.serverContent && turn.serverContent.inputTranscription) {
          console.debug('Received input transcription: %s\n', turn.serverContent.inputTranscription.text);
        }
      }
    
      session.close();
    }
    
    async function main() {
      await live().catch((e) => console.error('got error', e));
    }
    
    main();
    

### Change voice and language

Native audio output models support any of the voices available for our [Text-to-Speech (TTS)](/gemini-api/docs/speech-generation#voices) models. You can listen to all the voices in [AI Studio](https://aistudio.google.com/app/live).

To specify a voice, set the voice name within the `speechConfig` object as part of the session configuration:

### Python
    
    
    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": "Kore"}}
        },
    }
    

### JavaScript
    
    
    const config = {
      responseModalities: [Modality.AUDIO],
      speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: "Kore" } } }
    };
    

The Live API supports multiple languages. Native audio output models automatically choose the appropriate language and don't support explicitly setting the language code.

## Native audio capabilities

Our latest models feature [native audio output](/gemini-api/docs/models/gemini-3.1-flash-live-preview), which provides natural, realistic-sounding speech and improved multilingual performance. 

### Thinking

Gemini 3.1 models use `thinkingLevel` to control thinking depth, with settings like `minimal`, `low`, `medium`, and `high`. The default is `minimal` to optimize for lowest latency. Gemini 2.5 models use `thinkingBudget` to set the number of thinking tokens instead. For more details on levels vs budgets, see [Thinking levels and budgets](/gemini-api/docs/thinking#levels-budgets).

###  Python 
    
    
    model = "gemini-3.1-flash-live-preview"
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"]
        thinking_config=types.ThinkingConfig(
            thinking_level="low",
        )
    )
    
    async with client.aio.live.connect(model=model, config=config) as session:
        # Send audio input and receive audio
    

### JavaScript
    
    
    const model = 'gemini-3.1-flash-live-preview';
    const config = {
      responseModalities: [Modality.AUDIO],
      thinkingConfig: {
        thinkingLevel: 'low',
      },
    };
    
    async function main() {
    
      const session = await ai.live.connect({
        model: model,
        config: config,
        callbacks: ...,
      });
    
      // Send audio input and receive audio
    
      session.close();
    }
    
    main();
    

Additionally, you can enable thought summaries by setting `includeThoughts` to `true` in your configuration. See [thought summaries](/gemini-api/docs/thinking#summaries) for more info:

###  Python 
    
    
    model = "gemini-3.1-flash-live-preview"
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"]
        thinking_config=types.ThinkingConfig(
            thinking_level="low",
            include_thoughts=True
        )
    )
    

### JavaScript
    
    
    const model = 'gemini-3.1-flash-live-preview';
    const config = {
      responseModalities: [Modality.AUDIO],
      thinkingConfig: {
        thinkingLevel: 'low',
        includeThoughts: true,
      },
    };
    

### Affective dialog

This feature lets Gemini adapt its response style to the input expression and tone.

To use affective dialog, set the api version to `v1alpha` and set `enable_affective_dialog` to `true`in the setup message:

### Python
    
    
    client = genai.Client(http_options={"api_version": "v1alpha"})
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        enable_affective_dialog=True
    )
    

### JavaScript
    
    
    const ai = new GoogleGenAI({ httpOptions: {"apiVersion": "v1alpha"} });
    
    const config = {
      responseModalities: [Modality.AUDIO],
      enableAffectiveDialog: true
    };
    

### Proactive audio

When this feature is enabled, Gemini can proactively decide not to respond if the content is not relevant.

To use it, set the api version to `v1alpha` and configure the `proactivity` field in the setup message and set `proactive_audio` to `true`:

### Python
    
    
    client = genai.Client(http_options={"api_version": "v1alpha"})
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        proactivity={'proactive_audio': True}
    )
    

### JavaScript
    
    
    const ai = new GoogleGenAI({ httpOptions: {"apiVersion": "v1alpha"} });
    
    const config = {
      responseModalities: [Modality.AUDIO],
      proactivity: { proactiveAudio: true }
    }
    

## Voice Activity Detection (VAD)

Voice Activity Detection (VAD) allows the model to recognize when a person is speaking. This is essential for creating natural conversations, as it allows a user to interrupt the model at any time.

When VAD detects an interruption, the ongoing generation is canceled and discarded. Only the information already sent to the client is retained in the session history. The server then sends a [`BidiGenerateContentServerContent`](/api/live#bidigeneratecontentservercontent) message to report the interruption.

The Gemini server then discards any pending function calls and sends a `BidiGenerateContentServerContent` message with the IDs of the canceled calls.

### Python
    
    
    async for response in session.receive():
        if response.server_content.interrupted is True:
            # The generation was interrupted
    
            # If realtime playback is implemented in your application,
            # you should stop playing audio and clear queued playback here.
    

### JavaScript
    
    
    const turns = await handleTurn();
    
    for (const turn of turns) {
      if (turn.serverContent && turn.serverContent.interrupted) {
        // The generation was interrupted
    
        // If realtime playback is implemented in your application,
        // you should stop playing audio and clear queued playback here.
      }
    }
    

### Automatic VAD

By default, the model automatically performs VAD on a continuous audio input stream. VAD can be configured with the [`realtimeInputConfig.automaticActivityDetection`](/api/live#RealtimeInputConfig.AutomaticActivityDetection) field of the [setup configuration](/api/live#BidiGenerateContentSetup).

When the audio stream is paused for more than a second (for example, because the user switched off the microphone), an [`audioStreamEnd`](/api/live#BidiGenerateContentRealtimeInput.FIELDS.bool.BidiGenerateContentRealtimeInput.audio_stream_end) event should be sent to flush any cached audio. The client can resume sending audio data at any time.

### Python
    
    
    # example audio file to try:
    # URL = "https://storage.googleapis.com/generativeai-downloads/data/hello_are_you_there.pcm"
    # !wget -q $URL -O sample.pcm
    import asyncio
    from pathlib import Path
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    model = "gemini-3.1-flash-live-preview"
    
    config = {"response_modalities": ["AUDIO"]}
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            audio_bytes = Path("sample.pcm").read_bytes()
    
            await session.send_realtime_input(
                audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
            )
    
            # if stream gets paused, send:
            # await session.send_realtime_input(audio_stream_end=True)
    
            async for response in session.receive():
                if response.text is not None:
                    print(response.text)
    
    if __name__ == "__main__":
        asyncio.run(main())
    

### JavaScript
    
    
    // example audio file to try:
    // URL = "https://storage.googleapis.com/generativeai-downloads/data/hello_are_you_there.pcm"
    // !wget -q $URL -O sample.pcm
    import { GoogleGenAI, Modality } from '@google/genai';
    import * as fs from "node:fs";
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    const config = { responseModalities: [Modality.AUDIO] };
    
    async function live() {
      const responseQueue = [];
    
      async function waitMessage() {
        let done = false;
        let message = undefined;
        while (!done) {
          message = responseQueue.shift();
          if (message) {
            done = true;
          } else {
            await new Promise((resolve) => setTimeout(resolve, 100));
          }
        }
        return message;
      }
    
      async function handleTurn() {
        const turns = [];
        let done = false;
        while (!done) {
          const message = await waitMessage();
          turns.push(message);
          if (message.serverContent && message.serverContent.turnComplete) {
            done = true;
          }
        }
        return turns;
      }
    
      const session = await ai.live.connect({
        model: model,
        callbacks: {
          onopen: function () {
            console.debug('Opened');
          },
          onmessage: function (message) {
            responseQueue.push(message);
          },
          onerror: function (e) {
            console.debug('Error:', e.message);
          },
          onclose: function (e) {
            console.debug('Close:', e.reason);
          },
        },
        config: config,
      });
    
      // Send Audio Chunk
      const fileBuffer = fs.readFileSync("sample.pcm");
      const base64Audio = Buffer.from(fileBuffer).toString('base64');
    
      session.sendRealtimeInput(
        {
          audio: {
            data: base64Audio,
            mimeType: "audio/pcm;rate=16000"
          }
        }
    
      );
    
      // if stream gets paused, send:
      // session.sendRealtimeInput({ audioStreamEnd: true })
    
      const turns = await handleTurn();
      for (const turn of turns) {
        if (turn.text) {
          console.debug('Received text: %s\n', turn.text);
        }
        else if (turn.data) {
          console.debug('Received inline data: %s\n', turn.data);
        }
      }
    
      session.close();
    }
    
    async function main() {
      await live().catch((e) => console.error('got error', e));
    }
    
    main();
    

With `send_realtime_input`, the API will respond to audio automatically based on VAD. While `send_client_content` adds messages to the model context in order, `send_realtime_input` is optimized for responsiveness at the expense of deterministic ordering.

### Automatic VAD configuration

For more control over the VAD activity, you can configure the following parameters. See [API reference](/api/live#automaticactivitydetection) for more info.

### Python
    
    
    from google.genai import types
    
    config = {
        "response_modalities": ["AUDIO"],
        "realtime_input_config": {
            "automatic_activity_detection": {
                "disabled": False, # default
                "start_of_speech_sensitivity": types.StartSensitivity.START_SENSITIVITY_LOW,
                "end_of_speech_sensitivity": types.EndSensitivity.END_SENSITIVITY_LOW,
                "prefix_padding_ms": 20,
                "silence_duration_ms": 100,
            }
        }
    }
    

### JavaScript
    
    
    import { GoogleGenAI, Modality, StartSensitivity, EndSensitivity } from '@google/genai';
    
    const config = {
      responseModalities: [Modality.AUDIO],
      realtimeInputConfig: {
        automaticActivityDetection: {
          disabled: false, // default
          startOfSpeechSensitivity: StartSensitivity.START_SENSITIVITY_LOW,
          endOfSpeechSensitivity: EndSensitivity.END_SENSITIVITY_LOW,
          prefixPaddingMs: 20,
          silenceDurationMs: 100,
        }
      }
    };
    

### Disable automatic VAD

Alternatively, the automatic VAD can be disabled by setting `realtimeInputConfig.automaticActivityDetection.disabled` to `true` in the setup message. In this configuration the client is responsible for detecting user speech and sending [`activityStart`](/api/live#BidiGenerateContentRealtimeInput.FIELDS.BidiGenerateContentRealtimeInput.ActivityStart.BidiGenerateContentRealtimeInput.activity_start) and [`activityEnd`](/api/live#BidiGenerateContentRealtimeInput.FIELDS.BidiGenerateContentRealtimeInput.ActivityEnd.BidiGenerateContentRealtimeInput.activity_end) messages at the appropriate times. An `audioStreamEnd` isn't sent in this configuration. Instead, any interruption of the stream is marked by an `activityEnd` message.

### Python
    
    
    config = {
        "response_modalities": ["AUDIO"],
        "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
    }
    
    async with client.aio.live.connect(model=model, config=config) as session:
        # ...
        await session.send_realtime_input(activity_start=types.ActivityStart())
        await session.send_realtime_input(
            audio=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=16000")
        )
        await session.send_realtime_input(activity_end=types.ActivityEnd())
        # ...
    

### JavaScript
    
    
    const config = {
      responseModalities: [Modality.AUDIO],
      realtimeInputConfig: {
        automaticActivityDetection: {
          disabled: true,
        }
      }
    };
    
    session.sendRealtimeInput({ activityStart: {} })
    
    session.sendRealtimeInput(
      {
        audio: {
          data: base64Audio,
          mimeType: "audio/pcm;rate=16000"
        }
      }
    
    );
    
    session.sendRealtimeInput({ activityEnd: {} })
    

## Token count

You can find the total number of consumed tokens in the [usageMetadata](/api/live#usagemetadata) field of the returned server message.

### Python
    
    
    async for message in session.receive():
        # The server will periodically send messages that include UsageMetadata.
        if message.usage_metadata:
            usage = message.usage_metadata
            print(
                f"Used {usage.total_token_count} tokens in total. Response token breakdown:"
            )
            for detail in usage.response_tokens_details:
                match detail:
                    case types.ModalityTokenCount(modality=modality, token_count=count):
                        print(f"{modality}: {count}")
    

### JavaScript
    
    
    const turns = await handleTurn();
    
    for (const turn of turns) {
      if (turn.usageMetadata) {
        console.debug('Used %s tokens in total. Response token breakdown:\n', turn.usageMetadata.totalTokenCount);
    
        for (const detail of turn.usageMetadata.responseTokensDetails) {
          console.debug('%s\n', detail);
        }
      }
    }
    

## Media resolution

You can specify the media resolution for the input media by setting the `mediaResolution` field as part of the session configuration:

### Python
    
    
    from google.genai import types
    
    config = {
        "response_modalities": ["AUDIO"],
        "media_resolution": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    }
    

### JavaScript
    
    
    import { GoogleGenAI, Modality, MediaResolution } from '@google/genai';
    
    const config = {
        responseModalities: [Modality.AUDIO],
        mediaResolution: MediaResolution.MEDIA_RESOLUTION_LOW,
    };
    

## Limitations

Consider the following limitations of the Live API when you plan your project.

### Response modalities

The native audio models only support `AUDIO response modality. If you need the model response as text, use the output audio transcription feature. 

### Client authentication

The Live API only provides server-to-server authentication by default. If you're implementing your Live API application using a [client-to-server approach](/gemini-api/docs/live#implementation-approach), you need to use [ephemeral tokens](/gemini-api/docs/ephemeral-tokens) to mitigate security risks.

### Session duration

Audio-only sessions are limited to 15 minutes, and audio plus video sessions are limited to 2 minutes. However, you can configure different [session management techniques](/gemini-api/docs/live-session) for unlimited extensions on session duration.

### Context window

A session has a context window limit of:

  * 128k tokens for native audio output models
  * 32k tokens for other Live API models



## Supported languages

Live API supports the following 97 languages.

Language | BCP-47 Code | Language | BCP-47 Code  
---|---|---|---  
Afrikaans | `af` | Latvian | `lv`  
Akan | `ak` | Lithuanian | `lt`  
Albanian | `sq` | Macedonian | `mk`  
Amharic | `am` | Malay | `ms`  
Arabic | `ar` | Malayalam | `ml`  
Armenian | `hy` | Maltese | `mt`  
Assamese | `as` | Maori | `mi`  
Azerbaijani | `az` | Marathi | `mr`  
Basque | `eu` | Mongolian | `mn`  
Belarusian | `be` | Nepali | `ne`  
Bengali | `bn` | Norwegian | `no`  
Bosnian | `bs` | Odia | `or`  
Bulgarian | `bg` | Oromo | `om`  
Burmese | `my` | Pashto | `ps`  
Catalan | `ca` | Persian | `fa`  
Cebuano | `ceb` | Polish | `pl`  
Chinese | `zh` | Portuguese | `pt`  
Croatian | `hr` | Punjabi | `pa`  
Czech | `cs` | Quechua | `qu`  
Danish | `da` | Romanian | `ro`  
Dutch | `nl` | Romansh | `rm`  
English | `en` | Russian | `ru`  
Estonian | `et` | Serbian | `sr`  
Faroese | `fo` | Sindhi | `sd`  
Filipino | `fil` | Sinhala | `si`  
Finnish | `fi` | Slovak | `sk`  
French | `fr` | Slovenian | `sl`  
Galician | `gl` | Somali | `so`  
Georgian | `ka` | Southern Sotho | `st`  
German | `de` | Spanish | `es`  
Greek | `el` | Swahili | `sw`  
Gujarati | `gu` | Swedish | `sv`  
Hausa | `ha` | Tajik | `tg`  
Hebrew | `iw` | Tamil | `ta`  
Hindi | `hi` | Telugu | `te`  
Hungarian | `hu` | Thai | `th`  
Icelandic | `is` | Tswana | `tn`  
Indonesian | `id` | Turkish | `tr`  
Irish | `ga` | Turkmen | `tk`  
Italian | `it` | Ukrainian | `uk`  
Japanese | `ja` | Urdu | `ur`  
Kannada | `kn` | Uzbek | `uz`  
Kazakh | `kk` | Vietnamese | `vi`  
Khmer | `km` | Welsh | `cy`  
Kinyarwanda | `rw` | Western Frisian | `fy`  
Korean | `ko` | Wolof | `wo`  
Kurdish | `ku` | Yoruba | `yo`  
Kyrgyz | `ky` | Zulu | `zu`  
Lao | `lo` |  |   
  
## What's next

  * Read the [Tool Use](/gemini-api/docs/live-tools) and [Session Management](/gemini-api/docs/live-session) guides for essential information on using the Live API effectively.
  * Try the Live API in [Google AI Studio](https://aistudio.google.com/app/live).
  * For more info about the Live API models, see [Gemini 2.5 Flash Native Audio](/gemini-api/docs/models#gemini-2.5-flash-native-audio) on the Models page.
  * Try more examples in the [Live API cookbook](https://colab.research.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI.ipynb), the [Live API Tools cookbook](https://colab.research.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI_tools.ipynb), and the [Live API Get Started script](https://github.com/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI.py).



Send feedback 
