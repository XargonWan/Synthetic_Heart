# gRPC Reference

Reference of all available xAI gRPC API services. The xAI Enterprise gRPC API is a robust, high-performance gRPC interface designed for seamless integration into existing systems.

## Base URL

```
api.x.ai
```

## Authentication

For all services, authenticate with the header:

```
Authorization: Bearer <your xAI API key>
```

## Protobuf Definitions

Visit xAI API Protobuf Definitions to view and download protobuf definitions.

## Available Services

### xai_api.Chat

Chat completion service for text generation.

```protobuf
service Chat {
  rpc CreateChatCompletion(ChatCompletionRequest) returns (ChatCompletionResponse);
  rpc CreateChatCompletionStream(ChatCompletionRequest) returns (stream ChatCompletionChunk);
}
```

#### Example (Python)

```python
import grpc
from xai_api import chat_pb2, chat_pb2_grpc

channel = grpc.secure_channel('api.x.ai:443', grpc.ssl_channel_credentials())
stub = chat_pb2_grpc.ChatStub(channel)

metadata = [('authorization', f'Bearer {api_key}')]

request = chat_pb2.ChatCompletionRequest(
    model="grok-4",
    messages=[
        chat_pb2.Message(role="user", content="Hello!")
    ]
)

response = stub.CreateChatCompletion(request, metadata=metadata)
print(response.choices[0].message.content)
```

### xai_api.Image

Image generation and editing service.

```protobuf
service Image {
  rpc CreateImage(ImageRequest) returns (ImageResponse);
  rpc EditImage(ImageEditRequest) returns (ImageResponse);
}
```

### xai_api.BatchMgmt

Batch management service for handling batch operations.

```protobuf
service BatchMgmt {
  rpc CreateBatch(CreateBatchRequest) returns (Batch);
  rpc GetBatch(GetBatchRequest) returns (Batch);
  rpc ListBatches(ListBatchesRequest) returns (ListBatchesResponse);
  rpc CancelBatch(CancelBatchRequest) returns (Batch);
}
```

### xai_api.Models

Model listing and information service.

```protobuf
service Models {
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc GetModel(GetModelRequest) returns (Model);
  rpc ListLanguageModels(ListLanguageModelsRequest) returns (ListLanguageModelsResponse);
  rpc GetLanguageModel(GetLanguageModelRequest) returns (LanguageModel);
  rpc ListImageGenerationModels(ListImageGenerationModelsRequest) returns (ListImageGenerationModelsResponse);
  rpc GetImageGenerationModel(GetImageGenerationModelRequest) returns (ImageGenerationModel);
}
```

### xai_api.Auth

Authentication and API key management service.

```protobuf
service Auth {
  rpc GetApiKey(GetApiKeyRequest) returns (ApiKey);
  rpc ListApiKeys(ListApiKeysRequest) returns (ListApiKeysResponse);
  rpc CreateApiKey(CreateApiKeyRequest) returns (ApiKey);
  rpc DeleteApiKey(DeleteApiKeyRequest) returns (Empty);
}
```

### xai_api.Sample

Raw sampling service for advanced use cases.

```protobuf
service Sample {
  rpc RawSample(RawSampleRequest) returns (RawSampleResponse);
  rpc RawSampleStream(RawSampleRequest) returns (stream RawSampleChunk);
}
```

## Streaming

gRPC supports bidirectional streaming for real-time responses:

```python
# Streaming example
stream = stub.CreateChatCompletionStream(request, metadata=metadata)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

## xAI Python SDK

For Python, the xAI SDK covers all features and uses gRPC for optimal performance:

```python
from xai_sdk import Client

client = Client(api_key="your-api-key")

# SDK handles gRPC internally
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello!"}]
)
```

The xAI SDK allows interaction with all products:
- Collections
- Voice API
- API key management
- And more

## Deprecated Services

### Messages (Anthropic Compatible)

**Status**: Deprecated

The Anthropic SDK compatibility is fully deprecated. Migrate to the Responses API or gRPC.

## Error Handling

gRPC uses standard status codes:

| Code | Description |
|------|-------------|
| OK (0) | Success |
| INVALID_ARGUMENT (3) | Invalid request |
| UNAUTHENTICATED (16) | Auth failed |
| PERMISSION_DENIED (7) | Access denied |
| NOT_FOUND (5) | Resource not found |
| RESOURCE_EXHAUSTED (8) | Rate limited |
| INTERNAL (13) | Server error |
| UNAVAILABLE (14) | Service unavailable |

## Best Practices

1. **Use connection pooling**: Reuse gRPC channels
2. **Handle streaming properly**: Process chunks as they arrive
3. **Implement retries**: Use exponential backoff for transient errors
4. **Set deadlines**: Prevent hanging requests
5. **Use the SDK**: The xAI SDK handles gRPC complexity for you
