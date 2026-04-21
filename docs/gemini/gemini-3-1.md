<!-- source: https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Gemini 3.1 Flash Live Preview

Gemini 3.1 Flash Live Preview is our low-latency, audio-to-audio model optimized for real-time dialogue and voice-first AI applications with acoustic nuance detection, numeric precision, and multimodal awareness.

[Try in Google AI Studio](https://aistudio.google.com/live?model=gemini-3.1-flash-live-preview)

## Documentation

Visit the [Live API](/gemini-api/docs/live-api) guide for full coverage of features and capabilities.

## gemini-3.1-flash-live-preview

Property | Description  
---|---  
id_cardModel code |  `gemini-3.1-flash-live-preview`  
saveSupported data types |  **Inputs** Text, images, audio, video **Output** Text and audio  
token_autoToken limits[[*]](/gemini-api/docs/tokens) |  **Input token limit** 131,072 **Output token limit** 65,536  
handymanCapabilities |  **Audio generation** Supported **Batch API** Not supported **Caching** Not supported **Code execution** Not supported **File search** Not Supported **Function calling** Supported **Grounding with Google Maps** Not supported **Image generation** Not supported **Live API** Supported **Search grounding** Supported **Structured outputs** Not supported **Thinking** Supported **URL context** Not supported  
123Versions |  Read the [model version patterns](/gemini-api/docs/models/gemini#model-versions) for more details.

  * Preview: `gemini-3.1-flash-live-preview`

  
calendar_monthLatest update | March 2026  
cognition_2Knowledge cutoff | January 2025  
  
## Migrating from Gemini 2.5 Flash Live

Gemini 3.1 Flash Live Preview is optimized for low-latency, real-time dialogue. When migrating from `gemini-2.5-flash-native-audio-preview-12-2025`, consider the following:

  * **Model string** : Update your model string from `gemini-2.5-flash-native-audio-preview-12-2025` to `gemini-3.1-flash-live-preview`.
  * **Thinking configuration** : Gemini 3.1 uses `thinkingLevel` (with settings like `minimal`, `low`, `medium`, and `high`) instead of `thinkingBudget`. The default is `minimal` to optimize for lowest latency. See [Thinking levels and budgets](/gemini-api/docs/thinking#levels-budgets).
  * **Server events** : A single [`BidiGenerateContentServerContent`](/api/live#bidigeneratecontentservercontent) event can now contain multiple content parts simultaneously (for example, audio chunks and transcript). Update your code to process all parts in each event to avoid missing content.
  * **Client content** : `send_client_content` is only supported for seeding initial context history (requires setting [`initial_history_in_client_content`](/api/live#HistoryConfig) in [`history_config`](/api/live#BidiGenerateContentSetup)). Use [`send_realtime_input`](/api/live#bidigeneratecontentrealtimeinput) to send text updates during the conversation. See [Incremental content updates](/gemini-api/docs/live-guide#incremental-updates).
  * **Turn coverage** : Defaults to [`TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO`](/api/live#turncoverage) instead of `TURN_INCLUDES_ONLY_ACTIVITY`. The model's turn now includes detected audio activity and all video frames. If your application currently sends a constant stream of video frames, you may want to update your application to only send video frames when there is audio activity to avoid incurring additional costs.
  * **Async function calling** : Not yet supported. Function calling is synchronous only. The model will not start responding until you've sent the tool response. See [Async function calling](/gemini-api/docs/live-tools#async-function-calling).
  * **Proactive audio and affective dialogue** : These features are not yet supported in Gemini 3.1 Flash Live. Remove any configuration for these features from your code. See [Proactive audio](/gemini-api/docs/live-guide#proactive-audio) and [Affective dialogue](/gemini-api/docs/live-guide#affective-dialog).



For a detailed feature comparison, see the [Model comparison](/gemini-api/docs/live-guide#model-comparison) table in the capabilities guide.

Send feedback 


---

<!-- source: https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Gemini 3.1 Pro Preview

Built to refine the performance and reliability of the Gemini 3 Pro series, Gemini 3.1 Pro Preview provides better thinking, improved token efficiency, and a more grounded, factually consistent experience. It's optimized for software engineering behavior and usability, as well as agentic workflows requiring precise tool usage and reliable multi-step execution across real-world domains.

[Try in Google AI Studio](https://aistudio.google.com/prompts/new_chat?model=gemini-3.1-pro-preview)

## Documentation

Visit the [Gemini 3 Developer Guide](/gemini-api/docs/gemini-3) page for full coverage of features and capabilities.

## gemini-3.1-pro-preview

Property | Description  
---|---  
id_cardModel code | `gemini-3.1-pro-preview`  
saveSupported data types |  **Inputs** Text, Image, Video, Audio, and PDF **Output** Text  
token_autoToken limits[[*]](/gemini-api/docs/tokens) |  **Input token limit** 1,048,576 **Output token limit** 65,536  
handymanCapabilities |  **Audio generation** Not supported **Batch API** Supported **Caching** Supported **Code execution** Supported **File search** Supported (AI Studio only) **Flex inference** Supported **Function calling** Supported **Grounding with Google Maps** Supported **Image generation** Not supported **Live API** Not supported **Priority inference** Supported **Search grounding** Supported **Structured outputs** Supported **Thinking** Supported **URL context** Supported  
123Versions |  Read the [model version patterns](/gemini-api/docs/models/gemini#model-versions) for more details.

  * Preview: `gemini-3.1-pro-preview`
  * Preview: `gemini-3.1-pro-preview-customtools` *

  
calendar_monthLatest update | February 2026  
cognition_2Knowledge cutoff | January 2025  
  
#### gemini-3.1-pro-preview-customtools

* _For those building with a mix of bash and custom tools, Gemini 3.1 Pro Preview comes with a separate endpoint available via the API called`gemini-3.1-pro-preview-customtools`. This endpoint is better at prioritizing your custom tools (for example `view_file` or `search_code`)._

_Note that while`gemini-3.1-pro-preview-customtools` is optimized for agentic workflows that use custom tools and bash, you may see quality fluctuations in some use cases which don't benefit from such tools._

Send feedback 


---

<!-- source: https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-preview -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Gemini 3.1 Flash-Lite Preview

Our most cost-efficient multimodal model, offering the fastest performance for high-frequency, lightweight tasks. Gemini 3.1 Flash-Lite is best for high-volume agentic tasks, simple data extraction, and extremely low-latency applications where budget and speed are the primary constraints.

[Try in Google AI Studio](https://aistudio.google.com/prompts/new_chat?model=gemini-3.1-flash-lite-preview)

## gemini-3.1-flash-lite-preview

Property | Description  
---|---  
id_cardModel code | `gemini-3.1-flash-lite-preview`  
saveSupported data types |  **Inputs** Text, Image, Video, Audio, and PDF **Output** Text  
token_autoToken limits[[*]](/gemini-api/docs/tokens) |  **Input token limit** 1,048,576 **Output token limit** 65,536  
handymanCapabilities |  **Audio generation** Not supported **Batch API** Supported **Caching** Supported **Code execution** Supported **Computer use** Not supported **File search** Supported **Flex inference** Supported **Function calling** Supported **Grounding with Google Maps** Supported **Image generation** Not supported **Live API** Not supported **Priority inference** Supported **Search grounding** Supported **Structured outputs** Supported **Thinking** Supported **URL context** Supported  
123Versions |  Read the [model version patterns](/gemini-api/docs/models/gemini#model-versions) for more details.

  * `Preview: gemini-3.1-flash-lite-preview`

  
calendar_monthLatest update | March 2026  
cognition_2Knowledge cutoff | January 2025  
  
## Developer guide

Gemini 3.1 Flash-Lite is best at handling straightforward tasks at significant scale. Here are some use cases best suited for Gemini 3.1 Flash-Lite:

  * **Translation** : Fast, cheap, high-volume translation, such as processing chat messages, reviews, and support tickets at scale. You can use system instructions to constrain output to only the translated text with no extra commentary:
        
        text = "Hey, are you down to grab some pizza later? I'm starving!"
        
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            config={
                "system_instruction": "Only output the translated text"
            },
            contents=f"Translate the following text to German: {text}"
        )
        
        print(response.text)
        

  * **Transcription** : Process recordings, voice notes, or any audio content where you need a text transcript without spinning up a separate speech-to-text pipeline. Supports multimodal inputs, so you can pass audio files directly for transcription:
        
        # URL = "https://storage.googleapis.com/generativeai-downloads/data/State_of_the_Union_Address_30_January_1961.mp3"
        
        # Upload the audio file to the GenAI File API
        uploaded_file = client.files.upload(file='sample.mp3')
        
        prompt = 'Generate a transcript of the audio.'
        
        response = client.models.generate_content(
          model="gemini-3.1-flash-lite-preview",
          contents=[prompt, uploaded_file]
        )
        
        print(response.text)
        

  * **Lightweight agentic tasks and data extraction** : Entity extraction, classification, and lightweight data processing pipelines supported with structured JSON output. For example, extracting structured data from an e-commerce customer review:
        
        from pydantic import BaseModel, Field
        
        prompt = "Analyze the user review and determine the aspect, sentiment score, summary quote, and return risk"
        input_text = "The boots look amazing and the leather is high quality, but they run way too small. I'm sending them back."
        
        class ReviewAnalysis(BaseModel):
            aspect: str = Field(description="The feature mentioned (e.g., Price, Comfort, Style, Shipping)")
            summary_quote: str = Field(description="The specific phrase from the review about this aspect")
            sentiment_score: int = Field(description="1 to 5 (1=worst, 5=best)")
            is_return_risk: bool = Field(description="True if the user mentions returning the item")
        
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=[prompt, input_text],
            config={
                "response_mime_type": "application/json",
                "response_json_schema": ReviewAnalysis.model_json_schema(),
            },
        )
        
        print(response.text)
        

  * **Document processing and summarization** : Parse PDFs and return concise summaries, like for building a document processing pipeline or quickly triaging incoming files:
        
        import httpx
        
        # Download a sample PDF document
        doc_url = "https://storage.googleapis.com/generativeai-downloads/data/med_gemini.pdf"
        doc_data = httpx.get(doc_url).content
        
        prompt = "Summarize this document"
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=[
                types.Part.from_bytes(
                    data=doc_data,
                    mime_type='application/pdf',
                ),
                prompt
            ]
        )
        
        print(response.text)
        

  * **Model routing** : Use a low-latency and low-cost model as a classifier that routes queries to the appropriate model based on task complexity. This is a real pattern in production — the open-source [Gemini CLI](https://geminicli.com/docs/core/#model-fallback) uses Flash-Lite to classify task complexity and route to Flash or Pro accordingly.
        
        FLASH_MODEL = 'flash'
        PRO_MODEL = 'pro'
        
        CLASSIFIER_SYSTEM_PROMPT = f"""
        You are a specialized Task Routing AI. Your sole function is to analyze the user's request and classify its complexity. Choose between `{FLASH_MODEL}` (SIMPLE) or `{PRO_MODEL}` (COMPLEX).
        1.  `{FLASH_MODEL}`: A fast, efficient model for simple, well-defined tasks.
        2.  `{PRO_MODEL}`: A powerful, advanced model for complex, open-ended, or multi-step tasks.
        
        A task is COMPLEX if it meets ONE OR MORE of the following criteria:
        1.  High Operational Complexity (Est. 4+ Steps/Tool Calls)
        2.  Strategic Planning and Conceptual Design
        3.  High Ambiguity or Large Scope
        4.  Deep Debugging and Root Cause Analysis
        
        A task is SIMPLE if it is highly specific, bounded, and has Low Operational Complexity (Est. 1-3 tool calls).
        """
        
        user_input = "I'm getting an error 'Cannot read property 'map' of undefined' when I click the save button. Can you fix it?"
        
        response_schema = {
          "type": "object",
          "properties": {
            "reasoning": {
              "type": "string",
              "description": "A brief, step-by-step explanation for the model choice, referencing the rubric."
            },
            "model_choice": {
              "type": "string",
              "enum": [FLASH_MODEL, PRO_MODEL]
            }
          },
          "required": ["reasoning", "model_choice"]
        }
        
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=user_input,
            config={
                "system_instruction": CLASSIFIER_SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "response_json_schema": response_schema
            },
        )
        
        print(response.text)
        

  * **Thinking** : For better accuracy for tasks that benefit from step-by-step reasoning, configure thinking so the model spends additional compute on internal reasoning before producing the final output:
        
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents="How does AI work?",
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="high")
            ),
        )
        
        print(response.text)
        




Send feedback 


---

<!-- source: https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image-preview -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Gemini 3.1 Flash Image Preview

**Nano Banana 2** provides high-quality image generation and conversational editing at a mainstream price point and low latency. It serves as the high-efficiency counterpart to [Gemini 3 Pro Image](/gemini-api/docs/models/gemini-3-pro-image-preview), optimized for speed and high-volume developer use cases.

**Key updates:**

  * New output resolution options: 
    * New support for 0.5K, 2K and 4K, default 1K
  * New Image Search Grounding: 
    * Integration of both text and image search results to inform generation with real-time web data
    * Supported with Thinking on or off
  * New 1:4, 4:1, 1:8 and 8:1 aspect ratios
  * Improved aspect ratio adherence
  * Improved image quality and consistency
  * Improved i18n text rendering



[Try in Google AI Studio](https://aistudio.google.com?model=gemini-3.1-flash-image-preview)

## Documentation

Visit the [Image generation](/gemini-api/docs/image-generation) page for full coverage of features and capabilities.

## gemini-3.1-flash-image-preview

Property | Description  
---|---  
id_cardModel code | `gemini-3.1-flash-image-preview`  
saveSupported data types |  **Inputs** Text and Image / PDF **Output** Image and Text  
token_autoToken limits[[*]](/gemini-api/docs/tokens) |  **Input token limit** 131,072 **Output token limit** 32,768  
handymanCapabilities |  **Audio generation** Not supported **Batch API** Supported **Caching** Not supported **Code execution** Not supported **File search** Not supported **Function calling** Not supported **Grounding with Google Maps** Not supported **Image generation** Supported **Live API** Not supported **Search grounding** Supported **Structured outputs** Not supported **Thinking** Supported **URL context** Not supported  
123Versions |  Read the [model version patterns](/gemini-api/docs/models/gemini#model-versions) for more details.

  * `Preview: gemini-3.1-flash-image-preview`

  
calendar_monthLatest update | February 2026  
cognition_2Knowledge cutoff | January 2025  
  
Send feedback 
