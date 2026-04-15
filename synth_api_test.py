import json
import urllib.request
import urllib.error
import base64

img = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)
payload = {
    "model": "default",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {
            "role": "user",
            "content": "Describe this image.",
            "attachments": [
                {
                    "mime_type": "image/png",
                    "filename": "dot.png",
                    "data": base64.b64encode(img).decode("ascii"),
                }
            ],
        },
    ],
    "stream": False,
}
req = urllib.request.Request(
    "http://127.0.0.1:11435/api/chat",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(resp.status)
        print(resp.read().decode("utf-8", errors="replace"))
except urllib.error.HTTPError as e:
    print("HTTP_ERROR", e.code)
    print(e.read().decode("utf-8", errors="replace"))
except Exception as e:
    print("EXCEPTION", repr(e))
