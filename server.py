import os
import sys
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


def _configure_text_stream(stream) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass


_configure_text_stream(sys.stdout)
_configure_text_stream(sys.stderr)

# Add the current directory so myscoop can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="MakingScoop Manager API")

# Ensure static directory exists
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

import re
import queue
import time
import threading

# Active client queues for broadcasting
active_clients_lock = threading.Lock()
active_clients = set()

# Ensure standard output also goes to our status_queue so the user sees it
class StreamLogger:
    def __init__(self, original):
        self.original = original
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    def write(self, message):
        try:
            self.original.write(message)
        except UnicodeEncodeError:
            encoding = self.encoding or "utf-8"
            safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
            self.original.write(safe_message)
        if message.strip():
            clean_msg = self.ansi_escape.sub('', message.strip()).replace('\n', ' ').replace('\r', '')
            if clean_msg:
                # Thread-safe broadcast to all connected clients
                with active_clients_lock:
                    for q in active_clients:
                        try:
                            # non-blocking put to prevent a hung client from holding up the logger
                            q.put_nowait(clean_msg)
                        except queue.Full:
                            pass

    def flush(self):
        self.original.flush()

    def isatty(self):
        return getattr(self.original, "isatty", lambda: False)()

    def fileno(self):
        return self.original.fileno()

    @property
    def encoding(self):
        return getattr(self.original, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self.original, name)

# Replace stdout to capture click.echo prints used by myscoop/cli.py
sys.stdout = StreamLogger(sys.stdout)

def log_status(msg: str):
    print(msg)

class InstallRequest(BaseModel):
    path: str

@app.get("/")
def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")

@app.get("/")
def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")

@app.post("/api/install")
async def start_install(req: InstallRequest):
    """Background task to install one or many apps without failing the whole process."""
    try:
        target_path = target_path.strip().strip('"').strip("'")
        path_obj = Path(target_path)
        if not path_obj.exists():
            log_status(f"Error: Path does not exist -> {target_path}")
            return

        supported_suffixes = {".exe", ".msi", ".zip", ".7z"}
        targets = []
        if path_obj.is_dir():
            targets = [
                str(p) for p in sorted(path_obj.iterdir())
                if p.is_file() and p.suffix.lower() in supported_suffixes
            ]
            log_status(f"Found {len(targets)} installer/archive file(s) in folder.")
        elif path_obj.suffix.lower() in supported_suffixes:
            targets = [str(path_obj)]
        else:
            log_status(
                f"Target '{target_path}' is not a directory or a supported "
                f"installer/archive file (.exe, .msi, .zip, .7z)."
            )
            return

        for installer_path in targets:
            log_status(f"\\n--- Starting install for: {installer_path} ---")
            try:
                import subprocess

                # Pass the installer/archive path directly to the CLI.
                # The CLI's _resolve_install_target will find the best
                # matching manifest (e.g. 'abb' with gui installer type)
                # and handle the full install flow including GUI automation.
                cmd = [sys.executable, "myscoop.py", "install", str(installer_path)]
                log_status(f"Running cmd: {' '.join(cmd)}")
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8:replace"
                env.setdefault("PYTHONUTF8", "1")
                
                # Execute CLI command as a subprocess
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    env=env,
                )
                
                # Stream logs live to connected clients
                for line in process.stdout:
                    if line:
                        log_status(line.strip())
                        
                process.wait()
                
                if process.returncode == 0:
                    log_status(f"Installation successful: {installer_path}")
                else:
                    log_status(
                        f"Installation unsuccessful for '{installer_path}': "
                        f"process returned {process.returncode}"
                    )
                    
            except Exception as e:
                # Catch error so it continues to next app
                log_status(f"Failed to install '{installer_path}': {str(e)}")

        log_status("\\n--- All installations complete! ---")
        
    except Exception as e:
        log_status(f"Critical error during bulk install: {e}")

from fastapi.concurrency import run_in_threadpool

@app.post("/api/install")
async def start_install(req: InstallRequest):
    """Enqueue installation of a file or folder of executables."""
    target_path = req.path
    # Run the installation wrapper in background so we don't block the API event loop
    asyncio.create_task(run_in_threadpool(process_installation_queue_sync, target_path))
    return {"message": "Installation started in background.", "path": target_path}

@app.get("/api/install/status")
async def stream_status(request: Request):
    """Server-sent events for installation logs."""
    client_queue = queue.Queue(maxsize=1000)
    with active_clients_lock:
        active_clients.add(client_queue)

    async def event_generator():
        last_ping = time.time()
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    # Polling approach
                    msg = client_queue.get_nowait()
                    # Ensure no control chars break SSE format
                    msg_clean = msg.replace('\\x1b', '').replace('\\n', ' ')
                    yield f"data: {msg_clean}\\n\\n"
                    last_ping = time.time()
                except queue.Empty:
                    # Periodic keep-alive ping
                    if time.time() - last_ping > 1.0:
                        yield "data: ping\\n\\n"
                        last_ping = time.time()
                    await asyncio.sleep(0.1)
        finally:
            with active_clients_lock:
                active_clients.discard(client_queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
