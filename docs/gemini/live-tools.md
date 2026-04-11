<!-- source: https://ai.google.dev/gemini-api/docs/live-api/tools -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Tool use with Live API

Tool use allows Live API to go beyond just conversation by enabling it to perform actions in the real-world and pull in external context while maintaining a real time connection. You can define tools such as [Function calling](/gemini-api/docs/function-calling) and [Google Search](/gemini-api/docs/grounding) with the Live API.

## Overview of supported tools

Here's a brief overview of the available tools for Live API models:

Tool | Gemini 3.1 Flash Live Preview | Gemini 2.5 Flash Live Preview  
---|---|---  
**Search** | Supported | Supported  
**Function calling** | Supported (synchronous only) | Supported (synchronous and asynchronous)  
**Google Maps** | Not supported | Not supported  
**Code execution** | Not supported | Not supported  
**URL context** | Not supported | Not supported  
  
## Function calling

Live API supports function calling, just like regular content generation requests. Function calling lets the Live API interact with external data and programs, greatly increasing what your applications can accomplish.

You can define function declarations as part of the session configuration. After receiving tool calls, the client should respond with a list of `FunctionResponse` objects using the `session.send_tool_response` method.

See the [Function calling tutorial](/gemini-api/docs/function-calling) to learn more.

###  Python 
    
    
    import asyncio
    import wave
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    
    model = "gemini-3.1-flash-live-preview"
    
    # Simple function definitions
    turn_on_the_lights = {"name": "turn_on_the_lights"}
    turn_off_the_lights = {"name": "turn_off_the_lights"}
    
    tools = [{"function_declarations": [turn_on_the_lights, turn_off_the_lights]}]
    config = {"response_modalities": ["AUDIO"], "tools": tools}
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            prompt = "Turn on the lights please"
            await session.send_client_content(turns={"parts": [{"text": prompt}]})
    
            wf = wave.open("audio.wav", "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)  # Output is 24kHz
    
            async for response in session.receive():
                if response.data is not None:
                    wf.writeframes(response.data)
                elif response.tool_call:
                    print("The tool was called")
                    function_responses = []
                    for fc in response.tool_call.function_calls:
                        function_response = types.FunctionResponse(
                            id=fc.id,
                            name=fc.name,
                            response={ "result": "ok" } # simple, hard-coded function response
                        )
                        function_responses.append(function_response)
    
                    await session.send_tool_response(function_responses=function_responses)
    
            wf.close()
    
    if __name__ == "__main__":
        asyncio.run(main())
    

###  JavaScript 
    
    
    import { GoogleGenAI, Modality } from '@google/genai';
    import * as fs from "node:fs";
    import pkg from 'wavefile';  // npm install wavefile
    const { WaveFile } = pkg;
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    
    // Simple function definitions
    const turn_on_the_lights = { name: "turn_on_the_lights" } // , description: '...', parameters: { ... }
    const turn_off_the_lights = { name: "turn_off_the_lights" }
    
    const tools = [{ functionDeclarations: [turn_on_the_lights, turn_off_the_lights] }]
    
    const config = {
      responseModalities: [Modality.AUDIO],
      tools: tools
    }
    
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
          } else if (message.toolCall) {
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
    
      const inputTurns = 'Turn on the lights please';
      session.sendClientContent({ turns: inputTurns });
    
      let turns = await handleTurn();
    
      for (const turn of turns) {
        if (turn.toolCall) {
          console.debug('A tool was called');
          const functionResponses = [];
          for (const fc of turn.toolCall.functionCalls) {
            functionResponses.push({
              id: fc.id,
              name: fc.name,
              response: { result: "ok" } // simple, hard-coded function response
            });
          }
    
          console.debug('Sending tool response...\n');
          session.sendToolResponse({ functionResponses: functionResponses });
        }
      }
    
      // Check again for new messages
      turns = await handleTurn();
    
      // Combine audio data strings and save as wave file
      const combinedAudio = turns.reduce((acc, turn) => {
          if (turn.data) {
              const buffer = Buffer.from(turn.data, 'base64');
              const intArray = new Int16Array(buffer.buffer, buffer.byteOffset, buffer.byteLength / Int16Array.BYTES_PER_ELEMENT);
              return acc.concat(Array.from(intArray));
          }
          return acc;
      }, []);
    
      const audioBuffer = new Int16Array(combinedAudio);
    
      const wf = new WaveFile();
      wf.fromScratch(1, 24000, '16', audioBuffer);  // output is 24kHz
      fs.writeFileSync('audio.wav', wf.toBuffer());
    
      session.close();
    }
    
    async function main() {
      await live().catch((e) => console.error('got error', e));
    }
    
    main();
    

From a single prompt, the model can generate multiple function calls and the code necessary to chain their outputs. This code executes in a sandbox environment, generating subsequent [BidiGenerateContentToolCall](/api/live#bidigeneratecontenttoolcall) messages.

## Asynchronous function calling

Function calling executes sequentially by default, meaning execution pauses until the results of each function call are available. This ensures sequential processing, which means you won't be able to continue interacting with the model while the functions are being run.

If you don't want to block the conversation, you can tell the model to run the functions asynchronously. To do so, you first need to add a `behavior` to the function definitions:

###  Python 
    
    
    # Non-blocking function definitions
    turn_on_the_lights = {"name": "turn_on_the_lights", "behavior": "NON_BLOCKING"} # turn_on_the_lights will run asynchronously
    turn_off_the_lights = {"name": "turn_off_the_lights"} # turn_off_the_lights will still pause all interactions with the model
    

###  JavaScript 
    
    
    import { GoogleGenAI, Modality, Behavior } from '@google/genai';
    
    // Non-blocking function definitions
    const turn_on_the_lights = {name: "turn_on_the_lights", behavior: Behavior.NON_BLOCKING}
    
    // Blocking function definitions
    const turn_off_the_lights = {name: "turn_off_the_lights"}
    
    const tools = [{ functionDeclarations: [turn_on_the_lights, turn_off_the_lights] }]
    

`NON-BLOCKING` ensures the function runs asynchronously while you can continue interacting with the model.

Then you need to tell the model how to behave when it receives the `FunctionResponse` using the `scheduling` parameter. It can either:

  * Interrupt what it's doing and tell you about the response it got right away (`scheduling="INTERRUPT"`),
  * Wait until it's finished with what it's currently doing (`scheduling="WHEN_IDLE"`),
  * Or do nothing and use that knowledge later on in the discussion (`scheduling="SILENT"`)




###  Python 
    
    
    # for a non-blocking function definition, apply scheduling in the function response:
      function_response = types.FunctionResponse(
          id=fc.id,
          name=fc.name,
          response={
              "result": "ok",
              "scheduling": "INTERRUPT" # Can also be WHEN_IDLE or SILENT
          }
      )
    

###  JavaScript 
    
    
    import { GoogleGenAI, Modality, Behavior, FunctionResponseScheduling } from '@google/genai';
    
    // for a non-blocking function definition, apply scheduling in the function response:
    const functionResponse = {
      id: fc.id,
      name: fc.name,
      response: {
        result: "ok",
        scheduling: FunctionResponseScheduling.INTERRUPT  // Can also be WHEN_IDLE or SILENT
      }
    }
    

## Grounding with Google Search

You can enable Grounding with Google Search as part of the session configuration. This increases the Live API's accuracy and prevents hallucinations. See the [Grounding tutorial](/gemini-api/docs/grounding) to learn more.

### Python 
    
    
    import asyncio
    import wave
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    
    model = "gemini-3.1-flash-live-preview"
    
    tools = [{'google_search': {}}]
    config = {"response_modalities": ["AUDIO"], "tools": tools}
    
    async def main():
        async with client.aio.live.connect(model=model, config=config) as session:
            prompt = "When did the last Brazil vs. Argentina soccer match happen?"
            await session.send_client_content(turns={"parts": [{"text": prompt}]})
    
            wf = wave.open("audio.wav", "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)  # Output is 24kHz
    
            async for chunk in session.receive():
                if chunk.server_content:
                    if chunk.data is not None:
                        wf.writeframes(chunk.data)
    
                    # The model might generate and execute Python code to use Search
                    model_turn = chunk.server_content.model_turn
                    if model_turn:
                        for part in model_turn.parts:
                            if part.executable_code is not None:
                                print(part.executable_code.code)
    
                            if part.code_execution_result is not None:
                                print(part.code_execution_result.output)
    
            wf.close()
    
    if __name__ == "__main__":
        asyncio.run(main())
    

###  JavaScript 
    
    
    import { GoogleGenAI, Modality } from '@google/genai';
    import * as fs from "node:fs";
    import pkg from 'wavefile';  // npm install wavefile
    const { WaveFile } = pkg;
    
    const ai = new GoogleGenAI({});
    const model = 'gemini-3.1-flash-live-preview';
    
    const tools = [{ googleSearch: {} }]
    const config = {
      responseModalities: [Modality.AUDIO],
      tools: tools
    }
    
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
          } else if (message.toolCall) {
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
    
      const inputTurns = 'When did the last Brazil vs. Argentina soccer match happen?';
      session.sendClientContent({ turns: inputTurns });
    
      let turns = await handleTurn();
    
      let combinedData = '';
      for (const turn of turns) {
        if (turn.serverContent && turn.serverContent.modelTurn && turn.serverContent.modelTurn.parts) {
          for (const part of turn.serverContent.modelTurn.parts) {
            if (part.executableCode) {
              console.debug('executableCode: %s\n', part.executableCode.code);
            }
            else if (part.codeExecutionResult) {
              console.debug('codeExecutionResult: %s\n', part.codeExecutionResult.output);
            }
            else if (part.inlineData && typeof part.inlineData.data === 'string') {
              combinedData += atob(part.inlineData.data);
            }
          }
        }
      }
    
      // Convert the base64-encoded string of bytes into a Buffer.
      const buffer = Buffer.from(combinedData, 'binary');
    
      // The buffer contains raw bytes. For 16-bit audio, we need to interpret every 2 bytes as a single sample.
      const intArray = new Int16Array(buffer.buffer, buffer.byteOffset, buffer.byteLength / Int16Array.BYTES_PER_ELEMENT);
    
      const wf = new WaveFile();
      // The API returns 16-bit PCM audio at a 24kHz sample rate.
      wf.fromScratch(1, 24000, '16', intArray);
      fs.writeFileSync('audio.wav', wf.toBuffer());
    
      session.close();
    }
    
    async function main() {
      await live().catch((e) => console.error('got error', e));
    }
    
    main();
    

## Combining multiple tools

You can combine multiple tools within the Live API, increasing your application's capabilities even more:

###  Python 
    
    
    prompt = """
    Hey, I need you to do two things for me.
    
    1. Use Google Search to look up information about the largest earthquake in California the week of Dec 5 2024?
    2. Then turn on the lights
    
    Thanks!
    """
    
    tools = [
        {"google_search": {}},
        {"function_declarations": [turn_on_the_lights, turn_off_the_lights]},
    ]
    
    config = {"response_modalities": ["AUDIO"], "tools": tools}
    
    # ... remaining model call
    

###  JavaScript 
    
    
    const prompt = `Hey, I need you to do two things for me.
    
    1. Use Google Search to look up information about the largest earthquake in California the week of Dec 5 2024?
    2. Then turn on the lights
    
    Thanks!
    `
    
    const tools = [
      { googleSearch: {} },
      { functionDeclarations: [turn_on_the_lights, turn_off_the_lights] }
    ]
    
    const config = {
      responseModalities: [Modality.AUDIO],
      tools: tools
    }
    
    // ... remaining model call
    

## What's next

  * Check out more examples of using tools with the Live API in the [Tool use cookbook](https://colab.research.google.com/github/google-gemini/cookbook/blob/main/quickstarts/Get_started_LiveAPI_tools.ipynb).
  * Get the full story on features and configurations from the [Live API Capabilities guide](/gemini-api/docs/live-guide).



Send feedback 
