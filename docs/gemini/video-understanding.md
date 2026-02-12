> To learn about video generation, see the [Veo](https://ai.google.dev/gemini-api/docs/video) guide.

Gemini models can process videos, enabling many frontier developer use cases
that would have historically required domain specific models.
Some of Gemini's vision capabilities include the ability to: describe, segment,
and extract information from videos, answer questions about video content, and
refer to specific timestamps within a video.

You can provide videos as input to Gemini in the following ways:

The following lists some of the ways you can provide videos as input to Gemini:

- [Upload a video file](https://ai.google.dev/gemini-api/docs/video-understanding#upload-video) using the File API before making a request. Use this approach for files larger than 100MB, videos longer than approximately 1 minute, or when you want to reuse the file across multiple requests.
- [Pass inline video data](https://ai.google.dev/gemini-api/docs/video-understanding#inline-video) in your request. Use this method for smaller files (\<100MB) and shorter durations.
- [Pass YouTube URLs](https://ai.google.dev/gemini-api/docs/video-understanding#youtube) as part of your request.

To learn about other file input methods, such as using external URLs or files
stored in Google Cloud, see the
[File input methods](https://ai.google.dev/gemini-api/docs/file-input-methods) guide.

### Upload a video file

The following code downloads a sample video, uploads it using the [Files API](https://ai.google.dev/gemini-api/docs/files),
waits for it to be processed, and then uses the uploaded file reference to
summarize the video.  

### Python

    from google import genai

    client = genai.Client()

    myfile = client.files.upload(file="path/to/sample.mp4")

    response = client.models.generate_content(
        model="gemini-3-flash-preview", contents=[myfile, "Summarize this video. Then create a quiz with an answer key based on the information in this video."]
    )

    print(response.text)

### JavaScript

    import {
      GoogleGenAI,
      createUserContent,
      createPartFromUri,
    } from "@google/genai";

    const ai = new GoogleGenAI({});

    async function main() {
      const myfile = await ai.files.upload({
        file: "path/to/sample.mp4",
        config: { mimeType: "video/mp4" },
      });

      const response = await ai.models.generateContent({
        model: "gemini-3-flash-preview",
        contents: createUserContent([
          createPartFromUri(myfile.uri, myfile.mimeType),
          "Summarize this video. Then create a quiz with an answer key based on the information in this video.",
        ]),
      });
      console.log(response.text);
    }

    await main();

### Go

    uploadedFile, _ := client.Files.UploadFromPath(ctx, "path/to/sample.mp4", nil)

    parts := []*genai.Part{
        genai.NewPartFromText("Summarize this video. Then create a quiz with an answer key based on the information in this video."),
        genai.NewPartFromURI(uploadedFile.URI, uploadedFile.MIMEType),
    }

    contents := []*genai.Content{
        genai.NewContentFromParts(parts, genai.RoleUser),
    }

    result, _ := client.Models.GenerateContent(
        ctx,
        "gemini-3-flash-preview",
        contents,
        nil,
    )

    fmt.Println(result.Text())

### REST

    VIDEO_PATH="path/to/sample.mp4"
    MIME_TYPE=$(file -b --mime-type "${VIDEO_PATH}")
    NUM_BYTES=$(wc -c < "${VIDEO_PATH}")
    DISPLAY_NAME=VIDEO

    tmp_header_file=upload-header.tmp

    echo "Starting file upload..."
    curl "https://generativelanguage.googleapis.com/upload/v1beta/files" \
      -H "x-goog-api-key: $GEMINI_API_KEY" \
      -D ${tmp_header_file} \
      -H "X-Goog-Upload-Protocol: resumable" \
      -H "X-Goog-Upload-Command: start" \
      -H "X-Goog-Upload-Header-Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Header-Content-Type: ${MIME_TYPE}" \
      -H "Content-Type: application/json" \
      -d "{'file': {'display_name': '${DISPLAY_NAME}'}}" 2> /dev/null

    upload_url=$(grep -i "x-goog-upload-url: " "${tmp_header_file}" | cut -d" " -f2 | tr -d "\r")
    rm "${tmp_header_file}"

    echo "Uploading video data..."
    curl "${upload_url}" \
      -H "Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Offset: 0" \
      -H "X-Goog-Upload-Command: upload, finalize" \
      --data-binary "@${VIDEO_PATH}" 2> /dev/null > file_info.json

    file_uri=$(jq -r ".file.uri" file_info.json)
    echo file_uri=$file_uri

    echo "File uploaded successfully. File URI: ${file_uri}"

    # --- 3. Generate content using the uploaded video file ---
    echo "Generating content from video..."
    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent" \
        -H "x-goog-api-key: $GEMINI_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
              {"file_data":{"mime_type": "'"${MIME_TYPE}"'", "file_uri": "'"${file_uri}"'"}},
              {"text": "Summarize this video. Then create a quiz with an answer key based on the information in this video."}]
            }]
          }' 2> /dev/null > response.json

    jq -r ".candidates[].content.parts[].text" response.json

Always use the Files API when the total request size (including the file, text
prompt, system instructions, etc.) is larger than 20 MB, the video duration is
significant, or if you intend to use the same video in multiple prompts.
The File API accepts video file formats directly.

To learn more about working with media files, see
[Files API](https://ai.google.dev/gemini-api/docs/files).

### Pass video data inline

Instead of uploading a video file using the File API, you can pass smaller
videos directly in the request to `generateContent`. This is suitable for
shorter videos under 20MB total request size.

Here's an example of providing inline video data:  

### Python

    from google import genai
    from google.genai import types

    # Only for videos of size <20Mb
    video_file_name = "/path/to/your/video.mp4"
    video_bytes = open(video_file_name, 'rb').read()

    client = genai.Client()
    response = client.models.generate_content(
        model='models/gemini-3-flash-preview',
        contents=types.Content(
            parts=[
                types.Part(
                    inline_data=types.Blob(data=video_bytes, mime_type='video/mp4')
                ),
                types.Part(text='Please summarize the video in 3 sentences.')
            ]
        )
    )
    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";
    import * as fs from "node:fs";

    const ai = new GoogleGenAI({});
    const base64VideoFile = fs.readFileSync("path/to/small-sample.mp4", {
      encoding: "base64",
    });

    const contents = [
      {
        inlineData: {
          mimeType: "video/mp4",
          data: base64VideoFile,
        },
      },
      { text: "Please summarize the video in 3 sentences." }
    ];

    const response = await ai.models.generateContent({
      model: "gemini-3-flash-preview",
      contents: contents,
    });
    console.log(response.text);

### REST

**Note:** If you get an `Argument list too long` error, the base64 encoding of your file might be too long for the curl command line. Use the File API method instead for larger files.  

    VIDEO_PATH=/path/to/your/video.mp4

    if [[ "$(base64 --version 2>&1)" = *"FreeBSD"* ]]; then
      B64FLAGS="--input"
    else
      B64FLAGS="-w0"
    fi

    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent" \
        -H "x-goog-api-key: $GEMINI_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
                {
                  "inline_data": {
                    "mime_type":"video/mp4",
                    "data": "'$(base64 $B64FLAGS $VIDEO_PATH)'"
                  }
                },
                {"text": "Please summarize the video in 3 sentences."}
            ]
          }]
        }' 2> /dev/null

### Pass YouTube URLs

| **Preview:** The YouTube URL feature is in preview and is available at no charge. Pricing and rate limits are likely to change.

You can pass YouTube URLs directly to Gemini API as part of your request as follows:  

### Python

    from google import genai
    from google.genai import types

    client = genai.Client()
    response = client.models.generate_content(
        model='models/gemini-3-flash-preview',
        contents=types.Content(
            parts=[
                types.Part(
                    file_data=types.FileData(file_uri='https://www.youtube.com/watch?v=9hE5-98ZeCg')
                ),
                types.Part(text='Please summarize the video in 3 sentences.')
            ]
        )
    )
    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({});

    const contents = [
      {
        fileData: {
          fileUri: "https://www.youtube.com/watch?v=9hE5-98ZeCg",
        },
      },
      { text: "Please summarize the video in 3 sentences." }
    ];

    const response = await ai.models.generateContent({
      model: "gemini-3-flash-preview",
      contents: contents,
    });
    console.log(response.text);

### Go

    package main

    import (
      "context"
      "fmt"
      "os"
      "google.golang.org/genai"
    )

    func main() {
      ctx := context.Background()
      client, err := genai.NewClient(ctx, nil)
      if err != nil {
          log.Fatal(err)
      }

      parts := []*genai.Part{
          genai.NewPartFromText("Please summarize the video in 3 sentences."),
          genai.NewPartFromURI("https://www.youtube.com/watch?v=9hE5-98ZeCg","video/mp4"),
      }

      contents := []*genai.Content{
          genai.NewContentFromParts(parts, genai.RoleUser),
      }

      result, _ := client.Models.GenerateContent(
          ctx,
          "gemini-3-flash-preview",
          contents,
          nil,
      )

      fmt.Println(result.Text())
    }

### REST

    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent" \
        -H "x-goog-api-key: $GEMINI_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
                {"text": "Please summarize the video in 3 sentences."},
                {
                  "file_data": {
                    "file_uri": "https://www.youtube.com/watch?v=9hE5-98ZeCg"
                  }
                }
            ]
          }]
        }' 2> /dev/null

**Limitations:**

- For the free tier, you can't upload more than 8 hours of YouTube video per day.
- For the paid tier, there is no limit based on video length.
- For models prior to Gemini 2.5, you can upload only 1 video per request. For Gemini 2.5 and later models, you can upload a maximum of 10 videos per request.
- You can only upload public videos (not private or unlisted videos).

## Refer to timestamps in the content

You can ask questions about specific points in time within the video using
timestamps of the form `MM:SS`.  

### Python

    prompt = "What are the examples given at 00:05 and 00:10 supposed to show us?" # Adjusted timestamps for the NASA video

### JavaScript

    const prompt = "What are the examples given at 00:05 and 00:10 supposed to show us?";

### Go

        prompt := []*genai.Part{
            genai.NewPartFromURI(currentVideoFile.URI, currentVideoFile.MIMEType),
             // Adjusted timestamps for the NASA video
            genai.NewPartFromText("What are the examples given at 00:05 and " +
                "00:10 supposed to show us?"),
        }

### REST

    PROMPT="What are the examples given at 00:05 and 00:10 supposed to show us?"

## Extract detailed insights from video

Gemini models offer powerful capabilities for understanding video content by
processing information from both the audio and visual streams. This lets you
extract a rich set of details, including generating descriptions of what is
happening in a video and answering questions about its content. For visual
descriptions, the model samples the video at a rate of **1 frame per second**.
This sampling rate may affect the level of detail in the descriptions,
particularly for videos with rapidly changing visuals.  

### Python

    prompt = "Describe the key events in this video, providing both audio and visual details. Include timestamps for salient moments."

### JavaScript

    const prompt = "Describe the key events in this video, providing both audio and visual details. Include timestamps for salient moments.";

### Go

        prompt := []*genai.Part{
            genai.NewPartFromURI(currentVideoFile.URI, currentVideoFile.MIMEType),
            genai.NewPartFromText("Describe the key events in this video, providing both audio and visual details. " +
          "Include timestamps for salient moments."),
        }

### REST

    PROMPT="Describe the key events in this video, providing both audio and visual details. Include timestamps for salient moments."

## Customize video processing

You can customize video processing in the Gemini API by setting clipping
intervals or providing custom frame rate sampling.
| **Tip:** Video clipping and frames per second (FPS) are supported by all models, but the quality is significantly higher from 2.5 series models.

### Set clipping intervals

You can clip video by specifying `videoMetadata` with start and end offsets.  

### Python

    from google import genai
    from google.genai import types

    client = genai.Client()
    response = client.models.generate_content(
        model='models/gemini-3-flash-preview',
        contents=types.Content(
            parts=[
                types.Part(
                    file_data=types.FileData(file_uri='https://www.youtube.com/watch?v=XEzRZ35urlk'),
                    video_metadata=types.VideoMetadata(
                        start_offset='1250s',
                        end_offset='1570s'
                    )
                ),
                types.Part(text='Please summarize the video in 3 sentences.')
            ]
        )
    )

### JavaScript

    import { GoogleGenAI } from '@google/genai';
    const ai = new GoogleGenAI({});
    const model = 'gemini-3-flash-preview';

    async function main() {
    const contents = [
      {
        role: 'user',
        parts: [
          {
            fileData: {
              fileUri: 'https://www.youtube.com/watch?v=9hE5-98ZeCg',
              mimeType: 'video/*',
            },
            videoMetadata: {
              startOffset: '40s',
              endOffset: '80s',
            }
          },
          {
            text: 'Please summarize the video in 3 sentences.',
          },
        ],
      },
    ];

    const response = await ai.models.generateContent({
      model,
      contents,
    });

    console.log(response.text)

    }

    await main();

### Set a custom frame rate

You can set custom frame rate sampling by passing an `fps` argument to
`videoMetadata`.
**Note:** Due to built-in per image based safety checks, the same video may get blocked at some fps and not at others due to different extracted frames.  

### Python

    from google import genai
    from google.genai import types

    # Only for videos of size <20Mb
    video_file_name = "/path/to/your/video.mp4"
    video_bytes = open(video_file_name, 'rb').read()

    client = genai.Client()
    response = client.models.generate_content(
        model='models/gemini-3-flash-preview',
        contents=types.Content(
            parts=[
                types.Part(
                    inline_data=types.Blob(
                        data=video_bytes,
                        mime_type='video/mp4'),
                    video_metadata=types.VideoMetadata(fps=5)
                ),
                types.Part(text='Please summarize the video in 3 sentences.')
            ]
        )
    )

By default 1 frame per second (FPS) is sampled from the video. You might want to
set low FPS (\< 1) for long videos. This is especially useful for mostly static
videos (e.g. lectures). Use a higher FPS for videos requiring granular temporal
analysis, such as fast-action understanding or high-speed motion tracking.

## Supported video formats

Gemini supports the following video format MIME types:

- `video/mp4`
- `video/mpeg`
- `video/mov`
- `video/avi`
- `video/x-flv`
- `video/mpg`
- `video/webm`
- `video/wmv`
- `video/3gpp`

## Technical details about videos

- **Supported models \& context** : All Gemini can process video data.
  - Models with a 1M context window can process videos up to 1 hour long at default media resolution or 3 hours long at low media resolution.
- **File API processing** : When using the File API, videos are stored at 1 frame per second (FPS) and audio is processed at 1Kbps (single channel). Timestamps are added every second.
  - These rates are subject to change in the future for improvements in inference.
  - You can override the 1 FPS sampling rate by [setting a custom frame rate](https://ai.google.dev/gemini-api/docs/video-understanding#custom-frame-rate).
- **Token calculation** : Each second of video is tokenized as follows:
  - Individual frames (sampled at 1 FPS):
    - If [`mediaResolution`](https://ai.google.dev/api/generate-content#MediaResolution) is set to low, frames are tokenized at 66 tokens per frame.
    - Otherwise, frames are tokenized at 258 tokens per frame.
  - Audio: 32 tokens per second.
  - Metadata is also included.
  - Total: Approximately 300 tokens per second of video at default media resolution, or 100 tokens per second of video at low media resolution.
- **Medial resolution** : Gemini 3 introduces granular control over multimodal
  vision processing with the `media_resolution` parameter. The
  `media_resolution` parameter determines the
  **maximum number of tokens allocated per input image or video frame.**
  Higher resolutions improve the model's ability to read fine text or identify
  small details, but increase token usage and latency.

  For more details about the parameter and how it can impact token
  calculations, see the [media resolution](https://ai.google.dev/gemini-api/docs/media-resolution) guide.
- **Timestamp format** : When referring to specific moments in a video within your prompt, use the `MM:SS` format (e.g., `01:15` for 1 minute and 15 seconds).

- **Best practices**:

  - Use only one video per prompt request for optimal results.
  - If combining text and a single video, place the text prompt *after* the video part in the `contents` array.
  - Be aware that fast action sequences might lose detail due to the 1 FPS sampling rate. Consider slowing down such clips if necessary.

## What's next

This guide shows how to upload video files and generate text outputs from video
inputs. To learn more, see the following resources:

- [System instructions](https://ai.google.dev/gemini-api/docs/text-generation#system-instructions): System instructions let you steer the behavior of the model based on your specific needs and use cases.
- [Files API](https://ai.google.dev/gemini-api/docs/files): Learn more about uploading and managing files for use with Gemini.
- [File prompting strategies](https://ai.google.dev/gemini-api/docs/files#prompt-guide): The Gemini API supports prompting with text, image, audio, and video data, also known as multimodal prompting.
- [Safety guidance](https://ai.google.dev/gemini-api/docs/safety-guidance): Sometimes generative AI models produce unexpected outputs, such as outputs that are inaccurate, biased, or offensive. Post-processing and human evaluation are essential to limit the risk of harm from such outputs.