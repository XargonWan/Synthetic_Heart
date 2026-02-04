Gemini models can process documents in PDF format, using native
vision to understand entire document contexts. This goes beyond
just text extraction, allowing Gemini to:

- Analyze and interpret content, including text, images, diagrams, charts, and tables, even in long documents up to 1000 pages.
- Extract information into [structured output](https://ai.google.dev/gemini-api/docs/structured-output) formats.
- Summarize and answer questions based on both the visual and textual elements in a document.
- Transcribe document content (e.g. to HTML), preserving layouts and formatting, for use in downstream applications.

You can also pass non-PDF documents in the same way but Gemini will see them
as normal text which will eliminate context like charts or formatting.

## Passing PDF data inline

You can pass PDF data inline in the request to `generateContent`. This is best
suited for smaller documents or temporary processing where you don't need to
reference the file in subsequent requests. We recommend using the [Files API](https://ai.google.dev/gemini-api/docs/document-processing#large-pdfs)
for larger documents that you need to refer to in multi-turn interactions to
improve request latency and reduce bandwidth usage.

The following example shows you how to fetch a PDF from a URL and convert it to
bytes for processing:  

### Python

    from google import genai
    from google.genai import types
    import httpx

    client = genai.Client()

    doc_url = "https://discovery.ucl.ac.uk/id/eprint/10089234/1/343019_3_art_0_py4t4l_convrt.pdf"

    # Retrieve and encode the PDF byte
    doc_data = httpx.get(doc_url).content

    prompt = "Summarize this document"
    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=[
            types.Part.from_bytes(
                data=doc_data,
                mime_type='application/pdf',
            ),
            prompt
        ]
    )

    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

    async function main() {
        const pdfResp = await fetch('https://discovery.ucl.ac.uk/id/eprint/10089234/1/343019_3_art_0_py4t4l_convrt.pdf')
            .then((response) => response.arrayBuffer());

        const contents = [
            { text: "Summarize this document" },
            {
                inlineData: {
                    mimeType: 'application/pdf',
                    data: Buffer.from(pdfResp).toString("base64")
                }
            }
        ];

        const response = await ai.models.generateContent({
            model: "gemini-3-flash-preview",
            contents: contents
        });
        console.log(response.text);
    }

    main();

### Go

    package main

    import (
        "context"
        "fmt"
        "io"
        "net/http"
        "os"
        "google.golang.org/genai"
    )

    func main() {

        ctx := context.Background()
        client, _ := genai.NewClient(ctx, &genai.ClientConfig{
            APIKey:  os.Getenv("GEMINI_API_KEY"),
            Backend: genai.BackendGeminiAPI,
        })

        pdfResp, _ := http.Get("https://discovery.ucl.ac.uk/id/eprint/10089234/1/343019_3_art_0_py4t4l_convrt.pdf")
        var pdfBytes []byte
        if pdfResp != nil && pdfResp.Body != nil {
            pdfBytes, _ = io.ReadAll(pdfResp.Body)
            pdfResp.Body.Close()
        }

        parts := []*genai.Part{
            &genai.Part{
                InlineData: &genai.Blob{
                    MIMEType: "application/pdf",
                    Data:     pdfBytes,
                },
            },
            genai.NewPartFromText("Summarize this document"),
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

    DOC_URL="https://discovery.ucl.ac.uk/id/eprint/10089234/1/343019_3_art_0_py4t4l_convrt.pdf"
    PROMPT="Summarize this document"
    DISPLAY_NAME="base64_pdf"

    # Download the PDF
    wget -O "${DISPLAY_NAME}.pdf" "${DOC_URL}"

    # Check for FreeBSD base64 and set flags accordingly
    if [[ "$(base64 --version 2>&1)" = *"FreeBSD"* ]]; then
      B64FLAGS="--input"
    else
      B64FLAGS="-w0"
    fi

    # Base64 encode the PDF
    ENCODED_PDF=$(base64 $B64FLAGS "${DISPLAY_NAME}.pdf")

    # Generate content using the base64 encoded PDF
    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=$GOOGLE_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
              {"inline_data": {"mime_type": "application/pdf", "data": "'"$ENCODED_PDF"'"}},
              {"text": "'$PROMPT'"}
            ]
          }]
        }' 2> /dev/null > response.json

    cat response.json
    echo

    jq ".candidates[].content.parts[].text" response.json

    # Clean up the downloaded PDF
    rm "${DISPLAY_NAME}.pdf"

You can also read a PDF from a local file for processing:  

### Python

    from google import genai
    from google.genai import types
    import pathlib

    client = genai.Client()

    # Retrieve and encode the PDF byte
    filepath = pathlib.Path('file.pdf')

    prompt = "Summarize this document"
    response = client.models.generate_content(
      model="gemini-3-flash-preview",
      contents=[
          types.Part.from_bytes(
            data=filepath.read_bytes(),
            mime_type='application/pdf',
          ),
          prompt])
    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";
    import * as fs from 'fs';

    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

    async function main() {
        const contents = [
            { text: "Summarize this document" },
            {
                inlineData: {
                    mimeType: 'application/pdf',
                    data: Buffer.from(fs.readFileSync("content/343019_3_art_0_py4t4l_convrt.pdf")).toString("base64")
                }
            }
        ];

        const response = await ai.models.generateContent({
            model: "gemini-3-flash-preview",
            contents: contents
        });
        console.log(response.text);
    }

    main();

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
        client, _ := genai.NewClient(ctx, &genai.ClientConfig{
            APIKey:  os.Getenv("GEMINI_API_KEY"),
            Backend: genai.BackendGeminiAPI,
        })

        pdfBytes, _ := os.ReadFile("path/to/your/file.pdf")

        parts := []*genai.Part{
            &genai.Part{
                InlineData: &genai.Blob{
                    MIMEType: "application/pdf",
                    Data:     pdfBytes,
                },
            },
            genai.NewPartFromText("Summarize this document"),
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

## Uploading PDFs using the Files API

We recommend you use Files API for larger files or when you intend to reuse a
document across multiple requests. This improves request latency and reduces
bandwidth usage by decoupling the file upload from the model requests.
| **Note:** The Files API is available at no cost in all regions where the Gemini API is available. Uploaded files are stored for 48 hours.

### Large PDFs from URLs

Use the File API to simplify uploading and processing large PDF files from URLs:  

### Python

    from google import genai
    from google.genai import types
    import io
    import httpx

    client = genai.Client()

    long_context_pdf_path = "https://www.nasa.gov/wp-content/uploads/static/history/alsj/a17/A17_FlightPlan.pdf"

    # Retrieve and upload the PDF using the File API
    doc_io = io.BytesIO(httpx.get(long_context_pdf_path).content)

    sample_doc = client.files.upload(
      # You can pass a path or a file-like object here
      file=doc_io,
      config=dict(
        mime_type='application/pdf')
    )

    prompt = "Summarize this document"

    response = client.models.generate_content(
      model="gemini-3-flash-preview",
      contents=[sample_doc, prompt])
    print(response.text)

### JavaScript

    import { createPartFromUri, GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

    async function main() {

        const pdfBuffer = await fetch("https://www.nasa.gov/wp-content/uploads/static/history/alsj/a17/A17_FlightPlan.pdf")
            .then((response) => response.arrayBuffer());

        const fileBlob = new Blob([pdfBuffer], { type: 'application/pdf' });

        const file = await ai.files.upload({
            file: fileBlob,
            config: {
                displayName: 'A17_FlightPlan.pdf',
            },
        });

        // Wait for the file to be processed.
        let getFile = await ai.files.get({ name: file.name });
        while (getFile.state === 'PROCESSING') {
            getFile = await ai.files.get({ name: file.name });
            console.log(`current file status: ${getFile.state}`);
            console.log('File is still processing, retrying in 5 seconds');

            await new Promise((resolve) => {
                setTimeout(resolve, 5000);
            });
        }
        if (file.state === 'FAILED') {
            throw new Error('File processing failed.');
        }

        // Add the file to the contents.
        const content = [
            'Summarize this document',
        ];

        if (file.uri && file.mimeType) {
            const fileContent = createPartFromUri(file.uri, file.mimeType);
            content.push(fileContent);
        }

        const response = await ai.models.generateContent({
            model: 'gemini-3-flash-preview',
            contents: content,
        });

        console.log(response.text);

    }

    main();

### Go

    package main

    import (
      "context"
      "fmt"
      "io"
      "net/http"
      "os"
      "google.golang.org/genai"
    )

    func main() {

      ctx := context.Background()
      client, _ := genai.NewClient(ctx, &genai.ClientConfig{
        APIKey:  os.Getenv("GEMINI_API_KEY"),
        Backend: genai.BackendGeminiAPI,
      })

      pdfURL := "https://www.nasa.gov/wp-content/uploads/static/history/alsj/a17/A17_FlightPlan.pdf"
      localPdfPath := "A17_FlightPlan_downloaded.pdf"

      respHttp, _ := http.Get(pdfURL)
      defer respHttp.Body.Close()

      outFile, _ := os.Create(localPdfPath)
      defer outFile.Close()

      _, _ = io.Copy(outFile, respHttp.Body)

      uploadConfig := &genai.UploadFileConfig{MIMEType: "application/pdf"}
      uploadedFile, _ := client.Files.UploadFromPath(ctx, localPdfPath, uploadConfig)

      promptParts := []*genai.Part{
        genai.NewPartFromURI(uploadedFile.URI, uploadedFile.MIMEType),
        genai.NewPartFromText("Summarize this document"),
      }
      contents := []*genai.Content{
        genai.NewContentFromParts(promptParts, genai.RoleUser), // Specify role
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

    PDF_PATH="https://www.nasa.gov/wp-content/uploads/static/history/alsj/a17/A17_FlightPlan.pdf"
    DISPLAY_NAME="A17_FlightPlan"
    PROMPT="Summarize this document"

    # Download the PDF from the provided URL
    wget -O "${DISPLAY_NAME}.pdf" "${PDF_PATH}"

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

    # Now generate content using that file
    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=$GOOGLE_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
              {"text": "'$PROMPT'"},
              {"file_data":{"mime_type": "application/pdf", "file_uri": '$file_uri'}}]
            }]
          }' 2> /dev/null > response.json

    cat response.json
    echo

    jq ".candidates[].content.parts[].text" response.json

    # Clean up the downloaded PDF
    rm "${DISPLAY_NAME}.pdf"

### Large PDFs stored locally

### Python

    from google import genai
    from google.genai import types
    import pathlib
    import httpx

    client = genai.Client()

    # Retrieve and encode the PDF byte
    file_path = pathlib.Path('large_file.pdf')

    # Upload the PDF using the File API
    sample_file = client.files.upload(
        file=file_path,
    )

    prompt="Summarize this document"

    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=[sample_file, "Summarize this document"])
    print(response.text)

### JavaScript

    import { createPartFromUri, GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

    async function main() {
        const file = await ai.files.upload({
            file: 'path-to-localfile.pdf'
            config: {
                displayName: 'A17_FlightPlan.pdf',
            },
        });

        // Wait for the file to be processed.
        let getFile = await ai.files.get({ name: file.name });
        while (getFile.state === 'PROCESSING') {
            getFile = await ai.files.get({ name: file.name });
            console.log(`current file status: ${getFile.state}`);
            console.log('File is still processing, retrying in 5 seconds');

            await new Promise((resolve) => {
                setTimeout(resolve, 5000);
            });
        }
        if (file.state === 'FAILED') {
            throw new Error('File processing failed.');
        }

        // Add the file to the contents.
        const content = [
            'Summarize this document',
        ];

        if (file.uri && file.mimeType) {
            const fileContent = createPartFromUri(file.uri, file.mimeType);
            content.push(fileContent);
        }

        const response = await ai.models.generateContent({
            model: 'gemini-3-flash-preview',
            contents: content,
        });

        console.log(response.text);

    }

    main();

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
        client, _ := genai.NewClient(ctx, &genai.ClientConfig{
            APIKey:  os.Getenv("GEMINI_API_KEY"),
            Backend: genai.BackendGeminiAPI,
        })
        localPdfPath := "/path/to/file.pdf"

        uploadConfig := &genai.UploadFileConfig{MIMEType: "application/pdf"}
        uploadedFile, _ := client.Files.UploadFromPath(ctx, localPdfPath, uploadConfig)

        promptParts := []*genai.Part{
            genai.NewPartFromURI(uploadedFile.URI, uploadedFile.MIMEType),
            genai.NewPartFromText("Give me a summary of this pdf file."),
        }
        contents := []*genai.Content{
            genai.NewContentFromParts(promptParts, genai.RoleUser),
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

    NUM_BYTES=$(wc -c < "${PDF_PATH}")
    DISPLAY_NAME=TEXT
    tmp_header_file=upload-header.tmp

    # Initial resumable request defining metadata.
    # The upload url is in the response headers dump them to a file.
    curl "${BASE_URL}/upload/v1beta/files?key=${GEMINI_API_KEY}" \
      -D upload-header.tmp \
      -H "X-Goog-Upload-Protocol: resumable" \
      -H "X-Goog-Upload-Command: start" \
      -H "X-Goog-Upload-Header-Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Header-Content-Type: application/pdf" \
      -H "Content-Type: application/json" \
      -d "{'file': {'display_name': '${DISPLAY_NAME}'}}" 2> /dev/null

    upload_url=$(grep -i "x-goog-upload-url: " "${tmp_header_file}" | cut -d" " -f2 | tr -d "\r")
    rm "${tmp_header_file}"

    # Upload the actual bytes.
    curl "${upload_url}" \
      -H "Content-Length: ${NUM_BYTES}" \
      -H "X-Goog-Upload-Offset: 0" \
      -H "X-Goog-Upload-Command: upload, finalize" \
      --data-binary "@${PDF_PATH}" 2> /dev/null > file_info.json

    file_uri=$(jq ".file.uri" file_info.json)
    echo file_uri=$file_uri

    # Now generate content using that file
    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=$GOOGLE_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
              {"text": "Can you add a few more lines to this poem?"},
              {"file_data":{"mime_type": "application/pdf", "file_uri": '$file_uri'}}]
            }]
          }' 2> /dev/null > response.json

    cat response.json
    echo

    jq ".candidates[].content.parts[].text" response.json

You can verify the API successfully stored the uploaded file and get its
metadata by calling [`files.get`](https://ai.google.dev/api/rest/v1beta/files/get). Only the `name`
(and by extension, the `uri`) are unique.  

### Python

    from google import genai
    import pathlib

    client = genai.Client()

    fpath = pathlib.Path('example.txt')
    fpath.write_text('hello')

    file = client.files.upload(file='example.txt')

    file_info = client.files.get(name=file.name)
    print(file_info.model_dump_json(indent=4))

### REST

    name=$(jq ".file.name" file_info.json)
    # Get the file of interest to check state
    curl https://generativelanguage.googleapis.com/v1beta/files/$name > file_info.json
    # Print some information about the file you got
    name=$(jq ".file.name" file_info.json)
    echo name=$name
    file_uri=$(jq ".file.uri" file_info.json)
    echo file_uri=$file_uri

## Passing multiple PDFs

The Gemini API is capable of processing multiple PDF documents (up to 1000 pages)
in a single request, as long as the combined size of the documents and the text
prompt stays within the model's context window.  

### Python

    from google import genai
    import io
    import httpx

    client = genai.Client()

    doc_url_1 = "https://arxiv.org/pdf/2312.11805"
    doc_url_2 = "https://arxiv.org/pdf/2403.05530"

    # Retrieve and upload both PDFs using the File API
    doc_data_1 = io.BytesIO(httpx.get(doc_url_1).content)
    doc_data_2 = io.BytesIO(httpx.get(doc_url_2).content)

    sample_pdf_1 = client.files.upload(
      file=doc_data_1,
      config=dict(mime_type='application/pdf')
    )
    sample_pdf_2 = client.files.upload(
      file=doc_data_2,
      config=dict(mime_type='application/pdf')
    )

    prompt = "What is the difference between each of the main benchmarks between these two papers? Output these in a table."

    response = client.models.generate_content(
        model="gemini-3-flash-preview",
        contents=[sample_pdf_1, sample_pdf_2, prompt]
    )

    print(response.text)

### JavaScript

    import { createPartFromUri, GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({ apiKey: "GEMINI_API_KEY" });

    async function uploadRemotePDF(url, displayName) {
        const pdfBuffer = await fetch(url)
            .then((response) => response.arrayBuffer());

        const fileBlob = new Blob([pdfBuffer], { type: 'application/pdf' });

        const file = await ai.files.upload({
            file: fileBlob,
            config: {
                displayName: displayName,
            },
        });

        // Wait for the file to be processed.
        let getFile = await ai.files.get({ name: file.name });
        while (getFile.state === 'PROCESSING') {
            getFile = await ai.files.get({ name: file.name });
            console.log(`current file status: ${getFile.state}`);
            console.log('File is still processing, retrying in 5 seconds');

            await new Promise((resolve) => {
                setTimeout(resolve, 5000);
            });
        }
        if (file.state === 'FAILED') {
            throw new Error('File processing failed.');
        }

        return file;
    }

    async function main() {
        const content = [
            'What is the difference between each of the main benchmarks between these two papers? Output these in a table.',
        ];

        let file1 = await uploadRemotePDF("https://arxiv.org/pdf/2312.11805", "PDF 1")
        if (file1.uri && file1.mimeType) {
            const fileContent = createPartFromUri(file1.uri, file1.mimeType);
            content.push(fileContent);
        }
        let file2 = await uploadRemotePDF("https://arxiv.org/pdf/2403.05530", "PDF 2")
        if (file2.uri && file2.mimeType) {
            const fileContent = createPartFromUri(file2.uri, file2.mimeType);
            content.push(fileContent);
        }

        const response = await ai.models.generateContent({
            model: 'gemini-3-flash-preview',
            contents: content,
        });

        console.log(response.text);
    }

    main();

### Go

    package main

    import (
        "context"
        "fmt"
        "io"
        "net/http"
        "os"
        "google.golang.org/genai"
    )

    func main() {

        ctx := context.Background()
        client, _ := genai.NewClient(ctx, &genai.ClientConfig{
            APIKey:  os.Getenv("GEMINI_API_KEY"),
            Backend: genai.BackendGeminiAPI,
        })

        docUrl1 := "https://arxiv.org/pdf/2312.11805"
        docUrl2 := "https://arxiv.org/pdf/2403.05530"
        localPath1 := "doc1_downloaded.pdf"
        localPath2 := "doc2_downloaded.pdf"

        respHttp1, _ := http.Get(docUrl1)
        defer respHttp1.Body.Close()

        outFile1, _ := os.Create(localPath1)
        _, _ = io.Copy(outFile1, respHttp1.Body)
        outFile1.Close()

        respHttp2, _ := http.Get(docUrl2)
        defer respHttp2.Body.Close()

        outFile2, _ := os.Create(localPath2)
        _, _ = io.Copy(outFile2, respHttp2.Body)
        outFile2.Close()

        uploadConfig1 := &genai.UploadFileConfig{MIMEType: "application/pdf"}
        uploadedFile1, _ := client.Files.UploadFromPath(ctx, localPath1, uploadConfig1)

        uploadConfig2 := &genai.UploadFileConfig{MIMEType: "application/pdf"}
        uploadedFile2, _ := client.Files.UploadFromPath(ctx, localPath2, uploadConfig2)

        promptParts := []*genai.Part{
            genai.NewPartFromURI(uploadedFile1.URI, uploadedFile1.MIMEType),
            genai.NewPartFromURI(uploadedFile2.URI, uploadedFile2.MIMEType),
            genai.NewPartFromText("What is the difference between each of the " +
                                  "main benchmarks between these two papers? " +
                                  "Output these in a table."),
        }
        contents := []*genai.Content{
            genai.NewContentFromParts(promptParts, genai.RoleUser),
        }

        modelName := "gemini-3-flash-preview"
        result, _ := client.Models.GenerateContent(
            ctx,
            modelName,
            contents,
            nil,
        )

        fmt.Println(result.Text())
    }

### REST

    DOC_URL_1="https://arxiv.org/pdf/2312.11805"
    DOC_URL_2="https://arxiv.org/pdf/2403.05530"
    DISPLAY_NAME_1="Gemini_paper"
    DISPLAY_NAME_2="Gemini_1.5_paper"
    PROMPT="What is the difference between each of the main benchmarks between these two papers? Output these in a table."

    # Function to download and upload a PDF
    upload_pdf() {
      local doc_url="$1"
      local display_name="$2"

      # Download the PDF
      wget -O "${display_name}.pdf" "${doc_url}"

      local MIME_TYPE=$(file -b --mime-type "${display_name}.pdf")
      local NUM_BYTES=$(wc -c < "${display_name}.pdf")

      echo "MIME_TYPE: ${MIME_TYPE}"
      echo "NUM_BYTES: ${NUM_BYTES}"

      local tmp_header_file=upload-header.tmp

      # Initial resumable request
      curl "${BASE_URL}/upload/v1beta/files?key=${GOOGLE_API_KEY}" \
        -D "${tmp_header_file}" \
        -H "X-Goog-Upload-Protocol: resumable" \
        -H "X-Goog-Upload-Command: start" \
        -H "X-Goog-Upload-Header-Content-Length: ${NUM_BYTES}" \
        -H "X-Goog-Upload-Header-Content-Type: ${MIME_TYPE}" \
        -H "Content-Type: application/json" \
        -d "{'file': {'display_name': '${display_name}'}}" 2> /dev/null

      local upload_url=$(grep -i "x-goog-upload-url: " "${tmp_header_file}" | cut -d" " -f2 | tr -d "\r")
      rm "${tmp_header_file}"

      # Upload the PDF
      curl "${upload_url}" \
        -H "Content-Length: ${NUM_BYTES}" \
        -H "X-Goog-Upload-Offset: 0" \
        -H "X-Goog-Upload-Command: upload, finalize" \
        --data-binary "@${display_name}.pdf" 2> /dev/null > "file_info_${display_name}.json"

      local file_uri=$(jq ".file.uri" "file_info_${display_name}.json")
      echo "file_uri for ${display_name}: ${file_uri}"

      # Clean up the downloaded PDF
      rm "${display_name}.pdf"

      echo "${file_uri}"
    }

    # Upload the first PDF
    file_uri_1=$(upload_pdf "${DOC_URL_1}" "${DISPLAY_NAME_1}")

    # Upload the second PDF
    file_uri_2=$(upload_pdf "${DOC_URL_2}" "${DISPLAY_NAME_2}")

    # Now generate content using both files
    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent?key=$GOOGLE_API_KEY" \
        -H 'Content-Type: application/json' \
        -X POST \
        -d '{
          "contents": [{
            "parts":[
              {"file_data": {"mime_type": "application/pdf", "file_uri": '$file_uri_1'}},
              {"file_data": {"mime_type": "application/pdf", "file_uri": '$file_uri_2'}},
              {"text": "'$PROMPT'"}
            ]
          }]
        }' 2> /dev/null > response.json

    cat response.json
    echo

    jq ".candidates[].content.parts[].text" response.json

## Technical details

Gemini supports PDF files up to 50MB or 1000 pages. This limit applies
to both inline data and Files API uploads. Each document page is equivalent to 258
tokens.

While there are no specific limits to the number of pixels in a document besides
the model's [context window](https://ai.google.dev/gemini-api/docs/long-context), larger pages are
scaled down to a maximum resolution of 3072 x 3072 while preserving their original
aspect ratio, while smaller pages are scaled up to 768 x 768 pixels. There is no
cost reduction for pages at lower sizes, other than bandwidth, or performance
improvement for pages at higher resolution.

### Gemini 3 models

Gemini 3 introduces granular control over multimodal vision processing with the
`media_resolution` parameter. You can now set the resolution to low, medium, or
high per individual media part. With this addition, the processing of PDF
documents has been updated:

1. **Native text inclusion:** Text natively embedded in the PDF is extracted and provided to the model.
2. **Billing \& token reporting:**
   - You are **not charged** for tokens originating from the extracted **native text** in PDFs.
   - In the `usage_metadata` section of the API response, tokens generated from processing PDF pages (as images) are now counted under the `IMAGE` modality, not a separate `DOCUMENT` modality as in some earlier versions.

For more details about the media resolution parameter, see the
[Media resolution](https://ai.google.dev/gemini-api/docs/media-resolution) guide.

### Document types

Technically, you can pass other MIME types for document understanding, like
TXT, Markdown, HTML, XML, etc. However, document vision ***only meaningfully
understands PDFs***. Other types will be extracted as pure text, and the model
won't be able to interpret what we see in the rendering of those files. Any
file-type specifics like charts, diagrams, HTML tags, Markdown formatting, etc.,
will be lost.

To learn about other file input methods, see the
[File input methods](https://ai.google.dev/gemini-api/docs/file-input-methods) guide.

### Best practices

For best results:

- Rotate pages to the correct orientation before uploading.
- Avoid blurry pages.
- If using a single page, place the text prompt after the page.

## What's next

To learn more, see the following resources:

- [File prompting strategies](https://ai.google.dev/gemini-api/docs/files#prompt-guide): The Gemini API supports prompting with text, image, audio, and video data, also known as multimodal prompting.
- [System instructions](https://ai.google.dev/gemini-api/docs/text-generation#system-instructions): System instructions let you steer the behavior of the model based on your specific needs and use cases.