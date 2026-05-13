import asyncio
import os
from pathlib import Path

from core.webui import SynthWebUIInterface


def _set_default(name: str, value: str) -> None:
    if not os.getenv(name):
        os.environ[name] = value


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parents[1]
    cert_dir = repo_root / "tmp" / "webui_ssl"

    # Keep the helper aligned with the documented local URL: https://localhost:8000.
    # Callers can still override any of these with explicit environment variables.
    _set_default("SYNTH_WEBUI_HOST", "127.0.0.1")
    _set_default("SYNTH_WEBUI_TLS", "1")
    _set_default("SYNTH_WEBUI_HTTPS_PORT", "8000")
    _set_default("SYNTH_WEBUI_HTTP_PORT", "8080")
    _set_default("SYNTH_WEBUI_CERT_DIR", str(cert_dir))
    _set_default("SYNTH_WEBUI_CERTFILE", str(cert_dir / "synth_webui.crt"))
    _set_default("SYNTH_WEBUI_KEYFILE", str(cert_dir / "synth_webui.key"))

    ui = SynthWebUIInterface(autostart=False)
    asyncio.run(ui._run_server())
