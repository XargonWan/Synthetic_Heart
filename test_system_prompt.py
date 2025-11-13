#!/usr/bin/env python3
"""
Test system_prompt fix: verifica che il LLM riceva il system prompt
corretto quando in modalità correzione (correction scenario).
"""
import asyncio
import json
import sys
sys.path.insert(0, '/videodrome/videodrome-deployment/Synthetic_Heart')

async def main():
    from core.logging_utils import log_info, log_debug
    from llm_engines.selenium_chatgpt import SeleniumChatGPTPlugin
    from types import SimpleNamespace
    
    log_info("[test] Starting system prompt test...")
    
    # Crea plugin
    plugin = SeleniumChatGPTPlugin()
    log_info("[test] Plugin created")
    
    # Simula un correction_payload (come lo manda transport_layer.py)
    correction_payload = {
        "system_message": {
            "type": "error",
            "message": "CRITICAL ERROR: Your previous response was not valid JSON.",
            "your_reply": "Some bad response",
            "required_format": {
                "actions": [
                    {
                        "type": "message_telegram_bot",
                        "payload": {
                            "text": "Your message content here",
                            "target": "-1003098886330",
                            "thread_id": 0
                        }
                    }
                ]
            },
            "strict_requirements": [
                "MUST start with { and end with }",
                "MUST contain 'actions' array",
                "NO text outside JSON structure",
            ]
        }
    }
    
    correction_prompt = json.dumps(correction_payload, ensure_ascii=False)
    
    # Simula il messaggio
    message = SimpleNamespace()
    message.chat_id = -1003098886330
    message.text = correction_prompt
    message.thread_id = 0
    message.date = None
    message.from_user = None
    
    # Bot placeholder
    bot = None
    
    log_info("[test] Calling handle_incoming_message with correction_prompt...")
    try:
        # Questo dovrebbe ora riconoscere il system_message e costruire
        # un system prompt che forzi il LLM a rispondere SOLO con JSON
        response = await plugin.handle_incoming_message(bot, message, correction_prompt)
        log_info(f"[test] Response received: {response[:200]}")
        log_info("[test] ✅ Test completed")
    except Exception as e:
        log_info(f"[test] ❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
