# Google Drive Integration with Grok

Connect your Google Drive to Grok for seamless document access.

## Availability

This feature is available in:
- Grok Business plan
- Grok Enterprise plan

## Privacy

**Important**: xAI doesn't use customer Google Drive data to train its models.

## Connecting Google Drive

1. Go to Grok settings
2. Navigate to Integrations
3. Click "Connect Google Drive"
4. Authorize access through Google's OAuth flow
5. Select which folders/files to make accessible

## How It Works

Once connected, Grok automatically searches relevant files—no extra steps needed.

### Automatic Search

When you ask a question, Grok can:
- Search content and metadata in your connected Drive
- Find relevant documents automatically
- No need to manually specify files

### Capabilities

Grok can:
- **Search content and metadata**: Find files by content or file properties
- **Provide answers with inline citations**: Responses link back to source files
- **Reason over multiple files**: Cross-reference information from different documents

## Using Google Drive in Chat

Simply ask questions that might be answered by your Drive documents:

```
"What were the Q3 sales figures from my reports?"
"Find the meeting notes from last week's team sync"
"Summarize the project proposal document"
```

Grok will automatically search your connected Drive and incorporate relevant information.

## Citation Format

Responses include inline citations linking back to source files:

```
According to the Q3 Report [1], sales increased by 15%...

[1] Q3 Sales Report.pdf - Google Drive
```

## Supported File Types

Google Drive integration supports:
- Google Docs
- Google Sheets
- Google Slides
- PDF files
- Word documents (.docx)
- Text files (.txt)
- And more

## Managing Access

### Viewing Connected Accounts

1. Go to Grok settings
2. Navigate to Integrations
3. View connected Google accounts

### Disconnecting

1. Go to Integrations settings
2. Click "Disconnect" next to Google Drive
3. Confirm disconnection

**Note**: Disconnecting removes Grok's access to your Drive files.

## Best Practices

1. **Organize your Drive**: Well-organized files are easier to find
2. **Use descriptive names**: Clear file names improve search accuracy
3. **Keep documents updated**: Grok accesses current versions
4. **Review permissions**: Ensure appropriate files are accessible

## Security

- OAuth 2.0 authentication
- Read-only access (Grok cannot modify your files)
- Access can be revoked at any time
- Data is not used for model training

## Troubleshooting

### Files Not Found

- Verify the file is in an accessible folder
- Check file permissions
- Ensure file format is supported

### Connection Issues

- Try disconnecting and reconnecting
- Clear browser cache
- Check Google account permissions

## Enterprise Features

For Grok Enterprise:
- Admin controls over Drive integration
- Audit logging of document access
- Organization-wide policies
- Compliance controls
