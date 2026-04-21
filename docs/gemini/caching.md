<!-- source: https://ai.google.dev/gemini-api/docs/caching -->

Try the new high-speed, cost-effective [Veo 3.1 Lite](/gemini-api/docs/models/veo-3.1-lite-generate-preview) model for video generation at scale. 

  * [ Home ](https://ai.google.dev/)
  * [ Gemini API ](https://ai.google.dev/gemini-api)
  * [ Docs ](https://ai.google.dev/gemini-api/docs)



Send feedback 

#  Context caching

In a typical AI workflow, you might pass the same input tokens over and over to a model. The Gemini API offers two different caching mechanisms:

  * Implicit caching (automatically enabled on Gemini 2.5 and newer models, no cost saving guarantee)
  * Explicit caching (can be manually enabled on most models, cost saving guarantee)



Explicit caching is useful in cases where you want to guarantee cost savings, but with some added developer work.

## Implicit caching

Implicit caching is enabled by default for all Gemini 2.5 and newer models. We automatically pass on cost savings if your request hits caches. There is nothing you need to do in order to enable this. The minimum input token count for context caching is listed in the following table for each model:

Model | Min token limit  
---|---  
Gemini 3 Flash Preview | 1024  
Gemini 3 Pro Preview | 4096  
Gemini 2.5 Flash | 1024  
Gemini 2.5 Pro | 4096  
  
To increase the chance of an implicit cache hit:

  * Try putting large and common contents at the beginning of your prompt
  * Try to send requests with similar prefix in a short amount of time



You can see the number of tokens which were cache hits in the response object's `usage_metadata` field.

## Explicit caching

Using the Gemini API explicit caching feature, you can pass some content to the model once, cache the input tokens, and then refer to the cached tokens for subsequent requests. At certain volumes, using cached tokens is lower cost than passing in the same corpus of tokens repeatedly.

When you cache a set of tokens, you can choose how long you want the cache to exist before the tokens are automatically deleted. This caching duration is called the _time to live_ (TTL). If not set, the TTL defaults to 1 hour. The cost for caching depends on the input token size and how long you want the tokens to persist.

This section assumes that you've installed a Gemini SDK (or have curl installed) and that you've configured an API key, as shown in the [quickstart](/gemini-api/docs/quickstart).

### Generate content using a cache

### Python

The following example shows how to generate content using a cached system instruction and video file.

### Videos
    
    
    import os
    import pathlib
    import requests
    import time
    
    from google import genai
    from google.genai import types
    
    client = genai.Client()
    
    # Download a test video file and save it locally
    url = 'https://storage.googleapis.com/generativeai-downloads/data/SherlockJr._10min.mp4'
    path_to_video_file = pathlib.Path('SherlockJr._10min.mp4')
    if not path_to_video_file.exists():
        path_to_video_file.write_bytes(requests.get(url).content)
    
    # Upload the video using the Files API
    video_file = client.files.upload(file=path_to_video_file)
    
    # Wait for the file to finish processing
    while video_file.state.name == 'PROCESSING':
        time.sleep(2.5)
        video_file = client.files.get(name=video_file.name)
    
    print(f'Video processing complete: {video_file.uri}')
    
    model='models/gemini-3-flash-preview'
    
    # Create a cache with a 5 minute TTL (300 seconds)
    cache = client.caches.create(
        model=model,
        config=types.CreateCachedContentConfig(
            display_name='sherlock jr movie', # used to identify the cache
            system_instruction=(
                'You are an expert video analyzer, and your job is to answer '
                'the user\'s query based on the video file you have access to.'
            ),
            contents=[video_file],
            ttl="300s",
        )
    )
    
    response = client.models.generate_content(
        model = model,
        contents= (
        'Introduce different characters in the movie by describing '
        'their personality, looks, and names. Also list the timestamps '
        'they were introduced for the first time.'),
        config=types.GenerateContentConfig(cached_content=cache.name)
    )
    
    print(response.usage_metadata)
    
    print(response.text)
    

### PDFs
    
    
    from google import genai
    from google.genai import types
    import io
    import httpx
    
    client = genai.Client()
    
    long_context_pdf_path = "https://sma.nasa.gov/SignificantIncidents/assets/a11_missionreport.pdf"
    
    # Retrieve and upload the PDF using the File API
    doc_io = io.BytesIO(httpx.get(long_context_pdf_path).content)
    
    document = client.files.upload(
      file=doc_io,
      config=dict(mime_type='application/pdf')
    )
    
    model_name = "gemini-3-flash-preview"
    system_instruction = "You are an expert analyzing transcripts."
    
    # Create a cached content object
    cache = client.caches.create(
        model=model_name,
        config=types.CreateCachedContentConfig(
          system_instruction=system_instruction,
          contents=[document],
        )
    )
    
    print(f'{cache=}')
    
    response = client.models.generate_content(
      model=model_name,
      contents="Please summarize this transcript",
      config=types.GenerateContentConfig(
        cached_content=cache.name
      ))
    
    print(f'{response.usage_metadata=}')
    
    print('\n\n', response.text)
    

### JavaScript

The following example shows how to generate content using a cached system instruction and a text file.
    
    
    import {
      GoogleGenAI,
      createUserContent,
      createPartFromUri,
    } from "@google/genai";
    
    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });
    
    async function main() {
      const doc = await ai.files.upload({
        file: "path/to/file.txt",
        config: { mimeType: "text/plain" },
      });
      console.log("Uploaded file name:", doc.name);
    
      const modelName = "gemini-3-flash-preview";
      const cache = await ai.caches.create({
        model: modelName,
        config: {
          contents: createUserContent(createPartFromUri(doc.uri, doc.mimeType)),
          systemInstruction: "You are an expert analyzing transcripts.",
        },
      });
      console.log("Cache created:", cache);
    
      const response = await ai.models.generateContent({
        model: modelName,
        contents: "Please summarize this transcript",
        config: { cachedContent: cache.name },
      });
      console.log("Response text:", response.text);
    }
    
    await main();
    

### Go

The following example shows how to generate content using a cache.
    
    
    package main
    
    import (
        "context"
        "fmt"
        "log"
    
        "google.golang.org/genai"
    )
    
    func main() {
        ctx := context.Background()
        client, err := genai.NewClient(ctx, &genai.ClientConfig{
            APIKey: "GOOGLE_API_KEY",
            Backend: genai.BackendGeminiAPI,
        })
        if err != nil {
            log.Fatal(err)
        }
    
        modelName := "gemini-3-flash-preview"
        document, err := client.Files.UploadFromPath(
            ctx,
            "media/a11.txt",
            &genai.UploadFileConfig{
              MIMEType: "text/plain",
            },
        )
        if err != nil {
            log.Fatal(err)
        }
        parts := []*genai.Part{
            genai.NewPartFromURI(document.URI, document.MIMEType),
        }
        contents := []*genai.Content{
            genai.NewContentFromParts(parts, genai.RoleUser),
        }
        cache, err := client.Caches.Create(ctx, modelName, &genai.CreateCachedContentConfig{
            Contents: contents,
            SystemInstruction: genai.NewContentFromText(
              "You are an expert analyzing transcripts.", genai.RoleUser,
            ),
        })
        if err != nil {
            log.Fatal(err)
        }
        fmt.Println("Cache created:")
        fmt.Println(cache)
    
        // Use the cache for generating content.
        response, err := client.Models.GenerateContent(
            ctx,
            modelName,
            genai.Text("Please summarize this transcript"),
            &genai.GenerateContentConfig{
              CachedContent: cache.Name,
            },
        )
        if err != nil {
            log.Fatal(err)
        }
        printResponse(response) // helper for printing response parts
    }
    

### REST

The following example shows how to create a cache and then use it to generate content.

### Videos
    
    
    wget https://storage.googleapis.com/generativeai-downloads/data/a11.txt
    echo '{
      "model": "models/gemini-3-flash-preview",
      "contents":[
        {
          "parts":[
            {
              "inline_data": {
                "mime_type":"text/plain",
                "data": "'$(base64 $B64FLAGS a11.txt)'"
              }
            }
          ],
        "role": "user"
        }
      ],
      "systemInstruction": {
        "parts": [
          {
            "text": "You are an expert at analyzing transcripts."
          }
        ]
      },
      "ttl": "300s"
    }' > request.json
    
    curl -X POST "https://generativelanguage.googleapis.com/v1beta/cachedContents?key=$GEMINI_API_KEY" \
    -H 'Content-Type: application/json' \
    -d @request.json \
    > cache.json
    
    CACHE_NAME=$(cat cache.json | grep '"name":' | cut -d '"' -f 4 | head -n 1)
    
    curl -X POST "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=$GEMINI_API_KEY" \
    -H 'Content-Type: application/json' \
    -d '{
          "contents": [
            {
              "parts":[{
                "text": "Please summarize this transcript"
              }],
              "role": "user"
            },
          ],
          "cachedContent": "'$CACHE_NAME'"
        }'
    

### PDFs
    
    
    DOC_URL="https://sma.nasa.gov/SignificantIncidents/assets/a11_missionreport.pdf"
    DISPLAY_NAME="A11_Mission_Report"
    SYSTEM_INSTRUCTION="You are an expert at analyzing transcripts."
    PROMPT="Please summarize this transcript"
    MODEL="models/gemini-3-flash-preview"
    TTL="300s"
    
    # Download the PDF
    wget -O "${DISPLAY_NAME}.pdf" "${DOC_URL}"
    
    MIME_TYPE=$(file -b --mime-type "${DISPLAY_NAME}.pdf")
    NUM_BYTES=$(wc -c < "${DISPLAY_NAME}.pdf")
    
    echo "MIME_TYPE: ${MIME_TYPE}"
    echo "NUM_BYTES: ${NUM_BYTES}"
    
    tmp_header_file=upload-header.tmp
    
    # Initial resumable request defining metadata.
    # The upload url is in the response headers dump them to a file.
    curl "${BASE_URL}/upload/v1beta/files?key=${GOOGLE_API_KEY}" \
      -D upload-header.tmp \
      -H "X-Goog-Upload-Protocol: resumable" \
      -H "X-Goog-Upload-Command: start" \
      -H "X-Goog-Upload-Header-Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Header-Content-Type: ${MIME_TYPE}" \
      -H "Content-Type: application/json" \
      -d "{'file': {'display_name': '${DISPLAY_NAME}'}}" 2> /dev/null
    
    upload_url=$(grep -i "x-goog-upload-url: " "${tmp_header_file}" | cut -d" " -f2 | tr -d "\r")
    rm "${tmp_header_file}"
    
    # Upload the actual bytes.
    curl "${upload_url}" \
      -H "Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Offset: 0" \
      -H "X-Goog-Upload-Command: upload, finalize" \
      --data-binary "@${DISPLAY_NAME}.pdf" 2> /dev/null > file_info.json
    
    file_uri=$(jq ".file.uri" file_info.json)
    echo "file_uri: ${file_uri}"
    
    # Clean up the downloaded PDF
    rm "${DISPLAY_NAME}.pdf"
    
    # Create the cached content request
    echo '{
      "model": "'$MODEL'",
      "contents":[
        {
          "parts":[
            {"file_data": {"mime_type": "'$MIME_TYPE'", "file_uri": '$file_uri'}}
          ],
        "role": "user"
        }
      ],
      "system_instruction": {
        "parts": [
          {
            "text": "'$SYSTEM_INSTRUCTION'"
          }
        ],
        "role": "system"
      },
      "ttl": "'$TTL'"
    }' > request.json
    
    # Send the cached content request
    curl -X POST "${BASE_URL}/v1beta/cachedContents?key=$GOOGLE_API_KEY" \
    -H 'Content-Type: application/json' \
    -d @request.json \
    > cache.json
    
    CACHE_NAME=$(cat cache.json | grep '"name":' | cut -d '"' -f 4 | head -n 1)
    echo "CACHE_NAME: ${CACHE_NAME}"
    # Send the generateContent request using the cached content
    curl -X POST "${BASE_URL}/${MODEL}:generateContent?key=$GOOGLE_API_KEY" \
    -H 'Content-Type: application/json' \
    -d '{
          "contents": [
            {
              "parts":[{
                "text": "'$PROMPT'"
              }],
              "role": "user"
            }
          ],
          "cachedContent": "'$CACHE_NAME'"
        }' > response.json
    
    cat response.json
    
    echo jq ".candidates[].content.parts[].text" response.json
    

### List caches

It's not possible to retrieve or view cached content, but you can retrieve cache metadata (`name`, `model`, `display_name`, `usage_metadata`, `create_time`, `update_time`, and `expire_time`).

### Python

To list metadata for all uploaded caches, use `CachedContent.list()`:
    
    
    for cache in client.caches.list():
      print(cache)
    

To fetch the metadata for one cache object, if you know its name, use `get`:
    
    
    client.caches.get(name=name)
    

### JavaScript

To list metadata for all uploaded caches, use `GoogleGenAI.caches.list()`:
    
    
    console.log("My caches:");
    const pager = await ai.caches.list({ config: { pageSize: 10 } });
    let page = pager.page;
    while (true) {
      for (const c of page) {
        console.log("    ", c.name);
      }
      if (!pager.hasNextPage()) break;
      page = await pager.nextPage();
    }
    

### Go

The following example lists all caches.
    
    
    caches, err := client.Caches.All(ctx)
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println("Listing all caches:")
    for _, item := range caches {
        fmt.Println("   ", item.Name)
    }
    

The following example lists caches using a page size of 2.
    
    
    page, err := client.Caches.List(ctx, &genai.ListCachedContentsConfig{PageSize: 2})
    if err != nil {
        log.Fatal(err)
    }
    
    pageIndex := 1
    for {
        fmt.Printf("Listing caches (page %d):\n", pageIndex)
        for _, item := range page.Items {
            fmt.Println("   ", item.Name)
        }
        if page.NextPageToken == "" {
            break
        }
        page, err = page.Next(ctx)
        if err == genai.ErrPageDone {
            break
        } else if err != nil {
            return err
        }
        pageIndex++
    }
    

### REST
    
    
    curl "https://generativelanguage.googleapis.com/v1beta/cachedContents?key=$GEMINI_API_KEY"
    

### Update a cache

You can set a new `ttl` or `expire_time` for a cache. Changing anything else about the cache isn't supported.

### Python

The following example shows how to update the `ttl` of a cache using `client.caches.update()`.
    
    
    from google import genai
    from google.genai import types
    
    client.caches.update(
      name = cache.name,
      config  = types.UpdateCachedContentConfig(
          ttl='300s'
      )
    )
    

To set the expiry time, it will accepts either a `datetime` object or an ISO-formatted datetime string (`dt.isoformat()`, like `2025-01-27T16:02:36.473528+00:00`). Your time must include a time zone (`datetime.utcnow()` doesn't attach a time zone, `datetime.now(datetime.timezone.utc)` does attach a time zone).
    
    
    from google import genai
    from google.genai import types
    import datetime
    
    # You must use a time zone-aware time.
    in10min = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
    
    client.caches.update(
      name = cache.name,
      config  = types.UpdateCachedContentConfig(
          expire_time=in10min
      )
    )
    

### JavaScript

The following example shows how to update the `ttl` of a cache using `GoogleGenAI.caches.update()`.
    
    
    const ttl = `${2 * 3600}s`; // 2 hours in seconds
    const updatedCache = await ai.caches.update({
      name: cache.name,
      config: { ttl },
    });
    console.log("After update (TTL):", updatedCache);
    

### Go

The following example shows how to update the `TTL` of a cache.
    
    
    // Update the TTL (2 hours).
    cache, err = client.Caches.Update(ctx, cache.Name, &genai.UpdateCachedContentConfig{
        TTL: 7200 * time.Second,
    })
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println("After update:")
    fmt.Println(cache)
    

### REST

The following example shows how to update the `ttl` of a cache.
    
    
    curl -X PATCH "https://generativelanguage.googleapis.com/v1beta/$CACHE_NAME?key=$GEMINI_API_KEY" \
    -H 'Content-Type: application/json' \
    -d '{"ttl": "600s"}'
    

### Delete a cache

The caching service provides a delete operation for manually removing content from the cache. The following example shows how to delete a cache:

### Python
    
    
    client.caches.delete(cache.name)
    

### JavaScript
    
    
    await ai.caches.delete({ name: cache.name });
    

### Go
    
    
    _, err = client.Caches.Delete(ctx, cache.Name, &genai.DeleteCachedContentConfig{})
    if err != nil {
        log.Fatal(err)
    }
    fmt.Println("Cache deleted:", cache.Name)
    

### REST
    
    
    curl -X DELETE "https://generativelanguage.googleapis.com/v1beta/$CACHE_NAME?key=$GEMINI_API_KEY"
    

### Explicit caching using the OpenAI library

If you're using an [OpenAI library](/gemini-api/docs/openai), you can enable explicit caching using the `cached_content` property on [`extra_body`](/gemini-api/docs/openai#extra-body).

## When to use explicit caching

Context caching is particularly well suited to scenarios where a substantial initial context is referenced repeatedly by shorter requests. Consider using context caching for use cases such as:

  * Chatbots with extensive [system instructions](/gemini-api/docs/system-instructions)
  * Repetitive analysis of lengthy video files
  * Recurring queries against large document sets
  * Frequent code repository analysis or bug fixing



### How explicit caching reduces costs

Context caching is a paid feature designed to reduce cost. Billing is based on the following factors:

  1. **Cache token count:** The number of input tokens cached, billed at a reduced rate when included in subsequent prompts.
  2. **Storage duration:** The amount of time cached tokens are stored (TTL), billed based on the TTL duration of cached token count. There are no minimum or maximum bounds on the TTL.
  3. **Other factors:** Other charges apply, such as for non-cached input tokens and output tokens.



For up-to-date pricing details, refer to the Gemini API [pricing page](/pricing). To learn how to count tokens, see the [Token guide](/gemini-api/docs/tokens).

### Additional considerations

Keep the following considerations in mind when using context caching:

  * The _minimum_ input token count for context caching varies by model. The _maximum_ is the same as the maximum for the given model. (For more on counting tokens, see the [Token guide](/gemini-api/docs/tokens)).
  * The model doesn't make any distinction between cached tokens and regular input tokens. Cached content is a prefix to the prompt.
  * There are no special rate or usage limits on context caching; the standard rate limits for `GenerateContent` apply, and token limits include cached tokens.
  * The number of cached tokens is returned in the `usage_metadata` from the create, get, and list operations of the cache service, and also in `GenerateContent` when using the cache.



Send feedback 
