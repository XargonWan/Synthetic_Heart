# Chat with Files

Once you've uploaded files, you can reference them in conversations using the `file()` helper function in the xAI Python SDK. When files are attached, the system automatically enables document search capabilities, transforming your request into an agentic workflow.

## Prerequisites

> **xAI Python SDK Users**: Version 1.4.0 of the `xai-sdk` package is required to use the Files API.

- Files must be uploaded first (see [Managing Files](./managing-files.md))

## Basic Chat with a Single File

Reference an uploaded file in a conversation to let the model search through it for relevant information.

### Using xAI SDK

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a document
document_content = b"""Quarterly Sales Report - Q4 2024

Revenue Summary:
- Total Revenue: $5.2M
- Year-over-Year Growth: +18%
- Quarter-over-Quarter Growth: +7%

Top Performing Products:
- Product A: $2.1M revenue (+25% YoY)
- Product B: $1.8M revenue (+12% YoY)
- Product C: $1.3M revenue (+15% YoY)
"""

uploaded_file = client.files.upload(document_content, filename="sales_report.txt")

# Create a chat with the file attached
chat = client.chat.create(model="grok-4-fast")
chat.append(user("What was the total revenue in this report?", file(uploaded_file.id)))

# Get the response
response = chat.sample()

print(f"Answer: {response.content}")
print(f"\nUsage: {response.usage}")

# Clean up
client.files.delete(uploaded_file.id)
```

### Using OpenAI SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)

# Upload a file
document_content = b"""Quarterly Sales Report - Q4 2024

Revenue Summary:
- Total Revenue: $5.2M
- Year-over-Year Growth: +18%
"""

with open("temp_sales.txt", "wb") as f:
    f.write(document_content)

with open("temp_sales.txt", "rb") as f:
    uploaded_file = client.files.create(file=f, purpose="assistants")

# Create a chat with the file using Responses API
response = client.responses.create(
    model="grok-4-fast",
    input=[
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What was the total revenue in this report?"},
                {"type": "input_file", "file_id": uploaded_file.id}
            ]
        }
    ]
)

final_answer = response.output[-1].content[0].text

print(f"Answer: {final_answer}")

# Clean up
client.files.delete(uploaded_file.id)
```

### Using REST API

```bash
# First upload the file
FILE_ID=$(curl https://api.x.ai/v1/files \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -F file=@sales_report.txt \
  -F purpose=assistants | jq -r '.id')

# Then use it in chat
curl -X POST "https://api.x.ai/v1/responses" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"grok-4-fast\",
    \"input\": [
      {
        \"role\": \"user\",
        \"content\": [
          {\"type\": \"input_text\", \"text\": \"What was the total revenue in this report?\"},
          {\"type\": \"input_file\", \"file_id\": \"$FILE_ID\"}
        ]
      }
    ]
  }"
```

### Using Python Requests

```python
import os
import requests

api_key = os.getenv("XAI_API_KEY")
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_key}"
}

# Upload file first
upload_url = "https://api.x.ai/v1/files"
files = {"file": ("sales_report.txt", b"Total Revenue: $5.2M")}
data = {"purpose": "assistants"}
upload_response = requests.post(
    upload_url,
    headers={"Authorization": f"Bearer {api_key}"},
    files=files,
    data=data
)
file_id = upload_response.json()["id"]

# Create chat with file using attachments
chat_url = "https://api.x.ai/v1/responses"
payload = {
    "model": "grok-4-fast",
    "input": [
        {
            "role": "user",
            "content": "What was the total revenue in this report?",
            "attachments": [
                {
                    "file_id": file_id,
                    "tools": [{"type": "file_search"}]
                }
            ]
        }
    ]
}
response = requests.post(chat_url, headers=headers, json=payload)
print(response.json())
```

## Streaming Chat with Files

Get real-time responses while the model searches through your documents.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a document
document_content = b"""Product Specifications:
- Model: XR-2000
- Weight: 2.5 kg
- Dimensions: 30cm x 20cm x 10cm
- Power: 100W
- Features: Wireless connectivity, LCD display, Energy efficient
"""

uploaded_file = client.files.upload(document_content, filename="specs.txt")

# Create chat with streaming
chat = client.chat.create(model="grok-4-fast")
chat.append(user("What is the weight of the XR-2000?", file(uploaded_file.id)))

# Stream the response
is_thinking = True
for response, chunk in chat.stream():
    # Show tool calls as they happen
    for tool_call in chunk.tool_calls:
        print(f"\nSearching: {tool_call.function.name}")

    if response.usage.reasoning_tokens and is_thinking:
        print(f"\rThinking... ({response.usage.reasoning_tokens} tokens)", end="", flush=True)

    if chunk.content and is_thinking:
        print("\n\nAnswer:")
        is_thinking = False

    if chunk.content:
        print(chunk.content, end="", flush=True)

print(f"\n\nUsage: {response.usage}")

# Clean up
client.files.delete(uploaded_file.id)
```

## Multiple File Attachments

Query across multiple documents simultaneously.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload multiple documents
file1_content = b"Document 1: The project started in January 2024."
file2_content = b"Document 2: The project budget is $500,000."
file3_content = b"Document 3: The team consists of 5 engineers and 2 designers."

file1 = client.files.upload(file1_content, filename="timeline.txt")
file2 = client.files.upload(file2_content, filename="budget.txt")
file3 = client.files.upload(file3_content, filename="team.txt")

# Create chat with multiple files
chat = client.chat.create(model="grok-4-fast")
chat.append(
    user(
        "Based on these documents, when did the project start, what is the budget, and how many people are on the team?",
        file(file1.id),
        file(file2.id),
        file(file3.id),
    )
)

response = chat.sample()

print(f"Answer: {response.content}")
print("\nDocuments searched: 3")
print(f"Usage: {response.usage}")

# Clean up
client.files.delete(file1.id)
client.files.delete(file2.id)
client.files.delete(file3.id)
```

## Multi-Turn Conversations with Files

Maintain context across multiple questions about the same documents. Use encrypted content to preserve file context efficiently across multiple turns.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload an employee record
document_content = b"""Employee Information:
Name: Alice Johnson
Department: Engineering
Years of Service: 5
Performance Rating: Excellent
Skills: Python, Machine Learning, Cloud Architecture
Current Project: AI Platform Redesign
"""

uploaded_file = client.files.upload(document_content, filename="employee.txt")

# Create a multi-turn conversation with encrypted content
chat = client.chat.create(
    model="grok-4-fast",
    use_encrypted_content=True,  # Enable encrypted content for efficient multi-turn
)

# First turn: Ask about the employee name
chat.append(user("What is the employee's name?", file(uploaded_file.id)))
response1 = chat.sample()
print("Q1: What is the employee's name?")
print(f"A1: {response1.content}\n")

# Add the response to conversation history
chat.append(response1)

# Second turn: Ask about department (agentic context is retained via encrypted content)
chat.append(user("What department does this employee work in?"))
response2 = chat.sample()
print("Q2: What department does this employee work in?")
print(f"A2: {response2.content}\n")

# Add the response to conversation history
chat.append(response2)

# Third turn: Ask about skills
chat.append(user("What skills does this employee have?"))
response3 = chat.sample()
print("Q3: What skills does this employee have?")
print(f"A3: {response3.content}\n")

# Clean up
client.files.delete(uploaded_file.id)
```

### Why use `use_encrypted_content=True`?

When enabled, encrypted content preserves the agentic context (including document search results) across multiple turns without needing to re-attach files or re-search documents. This is more token-efficient for longer conversations.

## Combining Files with Other Modalities

You can combine file attachments with images and other content types in a single message.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file, image

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a text document with cat care information
text_content = b"Cat Care Guide: Cats require daily grooming, especially long-haired breeds. Regular brushing helps prevent matting and reduces shedding."
text_file = client.files.upload(text_content, filename="cat-care.txt")

# Use both file and image in the same message
chat = client.chat.create(model="grok-4-fast")
chat.append(
    user(
        "Based on the attached care guide, do you have any advice about the pictured cat?",
        file(text_file.id),
        image("https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/1200px-Cat03.jpg"),
    )
)

response = chat.sample()

print(f"Analysis: {response.content}")
print(f"\nUsage: {response.usage}")

# Clean up
client.files.delete(text_file.id)
```

## Combining Files with Code Execution

For data analysis tasks, you can attach data files and enable the code execution tool. This allows Grok to write and run Python code to analyze and process your data.

```python
import os
from xai_sdk import Client
from xai_sdk.chat import user, file
from xai_sdk.tools import code_execution

client = Client(api_key=os.getenv("XAI_API_KEY"))

# Upload a CSV data file
csv_content = b"""product,region,revenue,units_sold
Product A,North,245000,1200
Product A,South,189000,950
Product A,East,312000,1500
Product A,West,278000,1350
Product B,North,198000,800
Product B,South,156000,650
Product B,East,234000,950
Product B,West,201000,850
Product C,North,167000,700
Product C,South,134000,550
Product C,East,198000,800
Product C,West,176000,725
"""

data_file = client.files.upload(csv_content, filename="sales_data.csv")

# Create chat with both file attachment and code execution
chat = client.chat.create(
    model="grok-4-fast",
    tools=[code_execution()],  # Enable code execution
)

chat.append(
    user(
        "Analyze this sales data and calculate: 1) Total revenue by product, 2) Average units sold by region, 3) Which product-region combination has the highest revenue",
        file(data_file.id)
    )
)

# Stream the response to see code execution in real-time
is_thinking = True
for response, chunk in chat.stream():
    for tool_call in chunk.tool_calls:
        if tool_call.function.name == "code_execution":
            print("\n[Executing Code]")

    if response.usage.reasoning_tokens and is_thinking:
        print(f"\rThinking... ({response.usage.reasoning_tokens} tokens)", end="", flush=True)

    if chunk.content and is_thinking:
        print("\n\nAnalysis Results:")
        is_thinking = False

    if chunk.content:
        print(chunk.content, end="", flush=True)

print(f"\n\nUsage: {response.usage}")

# Clean up
client.files.delete(data_file.id)
```

The model will:
- Access the attached data file
- Write Python code to load and analyze the data
- Execute the code in a sandboxed environment
- Perform calculations and statistical analysis
- Return the results and insights in the response

## Automatic Document Search

When files are attached, the system automatically activates the `attachment_search` tool:

1. The request becomes an agentic workflow
2. Grok intelligently searches through your documents
3. Grok reasons over the content to answer questions

### Tool Call Visibility

In streaming mode, you can see when the model is searching your documents via tool call notifications.

## Recommended Models

For best document understanding:
- **grok-4-fast** - Good balance of speed and understanding (recommended)
- **grok-4** - Best understanding for complex documents

## Pricing

Document search is billed at **$10 per 1,000 tool invocations**, in addition to standard token costs.

Each time the model searches your documents counts as one tool invocation.

## Limitations and Considerations

### Request Constraints

- **No batch requests**: File attachments with document search are agentic requests and do not support batch mode (`n > 1`)
- **Streaming recommended**: Use streaming mode for better observability of document search process

### Document Complexity

- Highly unstructured or very long documents may require more processing
- Well-organized documents with clear structure are easier to search
- Large documents with many searches can result in higher token usage

### Model Compatibility

- **Recommended models**: `grok-4-fast`, `grok-4` for best document understanding
- **Agentic requirement**: File attachments require agentic-capable models that support server-side tools

## Best Practices

1. **Use descriptive filenames**: Helps the model understand document purpose
2. **Ask specific questions**: More focused queries yield better results
3. **Use streaming**: See real-time progress during document analysis
4. **Choose appropriate models**: grok-4 for complex documents
5. **Combine modalities**: Mix files with images when helpful
6. **Use encrypted content**: Enable for efficient multi-turn conversations
7. **Clean up files**: Delete files when no longer needed

## Error Handling

```python
try:
    response = client.chat.completions.create(
        model="grok-4",
        messages=[
            {
                "role": "user",
                "content": "Summarize this",
                "attachments": [{"file_id": "file-invalid"}]
            }
        ]
    )
except Exception as e:
    if "file_not_found" in str(e):
        print("File does not exist or has been deleted")
    elif "file_not_processed" in str(e):
        print("File is still processing, try again later")
    else:
        raise
```

## Next Steps

Learn more about managing your files:

- [Managing Files](./managing-files.md) - Learn how to upload, list, retrieve, and delete files using the Files API
