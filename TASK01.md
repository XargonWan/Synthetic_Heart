"SYSTEM ARCHITECT TASK: Build a Generalized Multimodal Game-Agent Bridge"

1. Core Architecture:

Implement a GameBridge Class using an Abstract Base Class (ABC) pattern.

Define methods: get_vision(), get_audio(), and handle_action().

Create a MinecraftAdapter as the first plugin implementing these methods.

2. Live API Implementation (WebSocket):

Use the bidiGenerateContent endpoint on v1alpha.

Sensory Loop: Capture screen frames (2 FPS) and system audio (16kHz PCM) into a non-blocking queue.

Continuous Streaming: Do not wait for a response to send the next frame. Use asyncio for a true bi-directional stream.

3. Critical State Handling (Gemini 3 Specific):

Thought Signatures: Capture the thought_signature from every model response. Save this to the MariaDB synth.chat_history_cache alongside the text.

Persistence: Every new turn MUST include the thought_signature from the previous turn. For the very first turn, set it to "INCLUDE_THOUGHTS_NEW_CONVERSATION".

4. Action Execution:

The model will output game_interaction tool calls.

Execute these via pydirectinput asynchronously so the character can move while the model continues to process the live video feed.