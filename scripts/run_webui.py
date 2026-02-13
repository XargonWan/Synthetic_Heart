import uvicorn
from core.webui import SynthWebUIInterface

if __name__ == "__main__":
    ui = SynthWebUIInterface(autostart=False)
    app = ui.app
    # Bind to 0.0.0.0:8000 for test purposes
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
