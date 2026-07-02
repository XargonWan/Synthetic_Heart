# Collections API Reference

API reference for managing collections and documents.

## Base URL

```
https://management-api.x.ai/v1
```

## Authentication

Use your Management API key:

```
Authorization: Bearer <management-api-key>
```

**Note**: Management keys are different from inference API keys. Get them from xAI Console → Settings → Management Keys.

## Collection Endpoints

### Create Collection

```http
POST /v1/collections
```

**Request Body**:
```json
{
  "name": "my-collection",
  "description": "Optional description"
}
```

**Response**:
```json
{
  "id": "col_abc123",
  "name": "my-collection",
  "description": "Optional description",
  "created_at": "2024-01-15T10:30:00Z",
  "document_count": 0
}
```

### List Collections

```http
GET /v1/collections
```

**Query Parameters**:
- `limit` (optional): Number of results (default: 20, max: 100)
- `offset` (optional): Pagination offset

**Response**:
```json
{
  "data": [
    {
      "id": "col_abc123",
      "name": "my-collection",
      "document_count": 5
    }
  ],
  "has_more": false
}
```

### Get Collection

```http
GET /v1/collections/{collection_id}
```

**Response**:
```json
{
  "id": "col_abc123",
  "name": "my-collection",
  "description": "Collection description",
  "created_at": "2024-01-15T10:30:00Z",
  "document_count": 5,
  "total_size_bytes": 1048576
}
```

### Update Collection

```http
PATCH /v1/collections/{collection_id}
```

**Request Body**:
```json
{
  "name": "updated-name",
  "description": "Updated description"
}
```

### Delete Collection

```http
DELETE /v1/collections/{collection_id}
```

**Response**: 204 No Content

## Document Endpoints

### Upload Document

```http
POST /v1/collections/{collection_id}/documents
```

**Request**: multipart/form-data
- `file`: Document file

**Response**:
```json
{
  "id": "doc_xyz789",
  "filename": "document.pdf",
  "size_bytes": 102400,
  "status": "processing",
  "created_at": "2024-01-15T10:35:00Z"
}
```

### List Documents

```http
GET /v1/collections/{collection_id}/documents
```

**Query Parameters**:
- `limit` (optional): Number of results
- `offset` (optional): Pagination offset
- `status` (optional): Filter by status (processing, ready, failed)

**Response**:
```json
{
  "data": [
    {
      "id": "doc_xyz789",
      "filename": "document.pdf",
      "size_bytes": 102400,
      "status": "ready",
      "created_at": "2024-01-15T10:35:00Z"
    }
  ],
  "has_more": false
}
```

### Get Document

```http
GET /v1/collections/{collection_id}/documents/{document_id}
```

### Delete Document

```http
DELETE /v1/collections/{collection_id}/documents/{document_id}
```

**Response**: 204 No Content

## Search in Collection

### Via Inference API

Use the `collections_search` tool with the inference API:

```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Search query"}],
    tools=[{
        "type": "collections_search",
        "collections_search": {
            "collection_ids": ["col_abc123"]
        }
    }]
)
```

## Document Status

| Status | Description |
|--------|-------------|
| `processing` | Document is being indexed |
| `ready` | Document is ready for search |
| `failed` | Processing failed |

## Limits

| Resource | Limit |
|----------|-------|
| Max file size | 100 MB |
| Max files per collection | 100,000 |
| Max total storage | 100 GB |
| Max collections | Unlimited |

Contact xAI to increase limits.

## Supported MIME Types

While any `UTF-8` encoded text file is supported, special file conversion and chunking techniques are available for the following MIME types:

### Document Formats

| MIME Type | Description |
|-----------|-------------|
| `application/pdf` | PDF documents |
| `application/x-pdf` | PDF documents (alternate) |
| `application/vnd.adobe.pdf` | Adobe PDF |
| `application/msword` | Microsoft Word (legacy) |
| `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | Word (.docx) |
| `application/vnd.oasis.opendocument.text` | OpenDocument Text |
| `application/epub` | ePub books |
| `application/epub+zip` | ePub books (zipped) |
| `application/x-epub+zip` | ePub books (alternate) |
| `application/rtf` | Rich Text Format |
| `text/rtf` | Rich Text Format |

### Spreadsheet Formats

| MIME Type | Description |
|-----------|-------------|
| `application/vnd.ms-excel` | Microsoft Excel (legacy) |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | Excel (.xlsx) |
| `application/vnd.openxmlformats-officedocument.spreadsheetml.template` | Excel template |
| `application/csv` | CSV files |
| `text/csv` | CSV files |
| `text/tab-separated-values` | TSV files |
| `text/tsv` | TSV files |

### Presentation Formats

| MIME Type | Description |
|-----------|-------------|
| `application/vnd.openxmlformats-officedocument.presentationml.presentation` | PowerPoint (.pptx) |
| `application/vnd.openxmlformats-officedocument.presentationml.slide` | PowerPoint slide |
| `application/vnd.openxmlformats-officedocument.presentationml.slideshow` | PowerPoint slideshow |
| `application/vnd.openxmlformats-officedocument.presentationml.template` | PowerPoint template |

### Programming Languages

| MIME Type | Description |
|-----------|-------------|
| `text/x-python` | Python |
| `text/x-python-script` | Python script |
| `text/x-script.python` | Python script (alternate) |
| `text/javascript` | JavaScript |
| `text/jsx` | JSX (React) |
| `text/tsx` | TSX (TypeScript React) |
| `application/typescript` | TypeScript |
| `application/ecmascript` | ECMAScript |
| `text/x-java` | Java |
| `text/x-java-source` | Java source |
| `application/ms-java` | Java (alternate) |
| `text/x-c` | C |
| `text/x-chdr` | C header |
| `text/x-csrc` | C source |
| `text/x-c++hdr` | C++ header |
| `text/x-c++src` | C++ source |
| `text/x-csharp` | C# |
| `text/x-go` | Go |
| `text/x-rust` | Rust |
| `text/x-swift` | Swift |
| `text/x-kotlin` | Kotlin |
| `text/x-scala` | Scala |
| `text/x-ruby-script` | Ruby |
| `text/x-perl` | Perl |
| `text/x-perl-script` | Perl script |
| `text/php` | PHP |
| `text/x-php` | PHP (alternate) |
| `application/x-php` | PHP (alternate) |
| `application/dart` | Dart |
| `application/vnd.dart` | Dart (alternate) |
| `text/x-lua` | Lua |
| `text/x-tcl` | Tcl |
| `text/x-d` | D |
| `text/x-haskell` | Haskell |
| `text/x-erlang` | Erlang |
| `text/x-lisp` | Lisp |
| `text/x-emacs-lisp` | Emacs Lisp |
| `text/x-scheme` | Scheme |
| `text/x-objcsrc` | Objective-C |
| `text/x-pascal` | Pascal |
| `text/x-vbasic` | Visual Basic |

### Shell Scripts

| MIME Type | Description |
|-----------|-------------|
| `application/x-sh` | Shell script |
| `application/x-shellscript` | Shell script |
| `text/x-sh` | Shell script |
| `application/x-csh` | C shell |
| `text/x-csh` | C shell |
| `application/x-zsh` | Zsh |
| `application/x-powershell` | PowerShell |

### Markup & Data Formats

| MIME Type | Description |
|-----------|-------------|
| `text/plain` | Plain text |
| `text/markdown` | Markdown |
| `text/x-r-markdown` | R Markdown |
| `text/html` | HTML |
| `application/xhtml` | XHTML |
| `text/xml` | XML |
| `application/xml` | XML |
| `text/xml-dtd` | XML DTD |
| `application/json` | JSON |
| `text/yaml` | YAML |
| `text/css` | CSS |
| `text/x-sass` | Sass |
| `text/x-scss` | SCSS |

### Scientific & Technical

| MIME Type | Description |
|-----------|-------------|
| `application/x-latex` | LaTeX |
| `application/x-tex` | TeX |
| `text/x-tex` | TeX |
| `application/vnd.jupyter` | Jupyter notebooks |
| `text/x-bibtex` | BibTeX |
| `text/x-rst` | reStructuredText |
| `text/x-asm` | Assembly |
| `text/x-sql` | SQL |
| `text/x-diff` | Diff/Patch files |

### Other Formats

| MIME Type | Description |
|-----------|-------------|
| `text/calendar` | iCalendar |
| `text/x-vcalendar` | vCalendar |
| `text/vcard` | vCard |
| `text/vtt` | WebVTT subtitles |
| `text/uri-list` | URI list |
| `text/cache-manifest` | Cache manifest |
| `text/troff` | Troff |
| `text/n3` | N3/Notation3 |
| `text/turtle` | Turtle (RDF) |
| `text/x-coffeescript` | CoffeeScript |
| `text/x-java-properties` | Java properties |
| `application/vnd.ms-outlook` | Outlook messages |
| `application/x-hwp` | Hangul Word Processor |
| `application/x-hwp-v5` | Hangul Word Processor v5 |
| `application/vnd.curl` | Curl |
| `application/zip` | ZIP archives |

## Error Responses

```json
{
  "error": {
    "code": "invalid_request",
    "message": "File size exceeds maximum limit",
    "param": "file"
  }
}
```

### Error Codes

| Code | Description |
|------|-------------|
| `invalid_request` | Invalid request format |
| `not_found` | Collection/document not found |
| `quota_exceeded` | Storage quota exceeded |
| `file_too_large` | File exceeds size limit |
| `unsupported_format` | File format not supported |
