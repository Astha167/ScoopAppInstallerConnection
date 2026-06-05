import os
import sys
import asyncio
import subprocess
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

from myscoop.cli import APPS_DIR
from myscoop.install_state import get_valid_installed_versions

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

# Graceful shutdown state. Install work runs in a background thread and may
# block on child processes, so Ctrl+C needs an explicit signal and process
# cleanup path.
shutdown_event = threading.Event()
active_processes_lock = threading.Lock()
active_processes = set()
active_install_tasks = set()


def _register_process(process):
    with active_processes_lock:
        active_processes.add(process)


def _unregister_process(process):
    with active_processes_lock:
        active_processes.discard(process)


def _terminate_process(process, timeout: float = 5.0) -> None:
    try:
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except Exception:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        process.kill()
                    except Exception:
                        pass
    finally:
        stdout = getattr(process, "stdout", None)
        if stdout is not None:
            try:
                stdout.close()
            except Exception:
                pass


def _terminate_active_processes() -> None:
    with active_processes_lock:
        processes = list(active_processes)

    for process in processes:
        _terminate_process(process)

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


@app.on_event("shutdown")
async def shutdown_install_workers():
    shutdown_event.set()
    log_status("Server shutdown requested; stopping active installations...")
    _terminate_active_processes()
    for task in list(active_install_tasks):
        task.cancel()

# ---------------------------------------------------------------------------
# Fallback architecture: MakingScoop (AI/UI-TARS driven automation)
#
# This server (ScoopApp-Installer) is the *primary* installation engine and
# runs on port 8000. When an installation fails (non-zero return code) we hand
# the same installer path off to MakingScoop, which drives the setup wizard
# visually using the UI-TARS vision model.
#
# Port map (UI-TARS model port 8001 is fixed and cannot be changed):
#   8000 -> ScoopApp-Installer API (this server, primary engine)
#   8001 -> UI-TARS vision model endpoint (used internally by MakingScoop)
#   8002 -> MakingScoop API (AI fallback engine)
# ---------------------------------------------------------------------------
MAKINGSCOOP_BASE_URL = os.environ.get("MAKINGSCOOP_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
# Whether to subscribe to MakingScoop's SSE stream and relay terminal
# (success/failure/error) messages back to our own frontend.
RELAY_FALLBACK_LOGS = os.environ.get("SCOOP_RELAY_FALLBACK_LOGS", "1").lower() not in ("0", "false", "no")
# Overall safety cap (seconds) for how long we follow the fallback log stream.
FALLBACK_STREAM_TIMEOUT = int(os.environ.get("SCOOP_FALLBACK_STREAM_TIMEOUT", "1800"))


def _is_terminal_fallback_message(message: str) -> bool:
    """Only success / unsuccessful / error / completion messages are relayed.

    Streaming every AI log line back would be a huge overhead, so we filter the
    MakingScoop SSE feed down to the messages that actually tell the user the
    outcome of the AI-driven installation.
    """
    lowered = message.lower()
    keywords = (
        "successfully installed",
        "installation successful",
        "failed to install",
        "unsuccessful",
        "all installations complete",
        "error",
        "critical error",
        "path does not exist",
    )
    return any(k in lowered for k in keywords)


def forward_to_makingscoop_fallback(installer_path: str) -> None:
    """Delegate a failed installation to MakingScoop's AI automation.

    Sends a single HTTP POST carrying the installer path, then (optionally)
    subscribes to MakingScoop's SSE stream and relays only the terminal
    success/failure/error messages back to this server's own clients.
    """
    if shutdown_event.is_set():
        log_status("AI fallback skipped because server shutdown is in progress.")
        return

    try:
        import requests
    except ImportError:
        log_status("AI fallback unavailable: the 'requests' package is not installed.")
        return

    install_url = f"{MAKINGSCOOP_BASE_URL}/api/install"
    log_status(f"Delegating to AI fallback (MakingScoop) at {install_url}")

    try:
        resp = requests.post(install_url, json={"path": installer_path}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_status(f"AI fallback request failed: could not reach MakingScoop ({exc}).")
        return

    log_status(f"AI fallback accepted the installer: {installer_path}")

    if shutdown_event.is_set() or not RELAY_FALLBACK_LOGS:
        return

    # Follow MakingScoop's log stream and relay only meaningful outcome messages.
    status_url = f"{MAKINGSCOOP_BASE_URL}/api/install/status"
    try:
        with requests.get(
            status_url,
            stream=True,
            timeout=(15, 5),
            headers={"Accept": "text/event-stream"},
        ) as stream:
            stream.raise_for_status()
            deadline = time.time() + FALLBACK_STREAM_TIMEOUT
            for raw_line in stream.iter_lines(decode_unicode=True):
                if shutdown_event.is_set():
                    log_status("AI fallback log relay stopped due to server shutdown.")
                    break
                if time.time() > deadline:
                    log_status("AI fallback log relay timed out; stopping stream.")
                    break
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                payload = raw_line[len("data:"):].strip()
                if not payload or payload == "ping":
                    continue
                if _is_terminal_fallback_message(payload):
                    log_status(f"[AI] {payload}")
                    # The completion sentinel marks the end of the AI run.
                    if "all installations complete" in payload.lower():
                        break
    except requests.RequestException as exc:
        log_status(f"AI fallback log relay ended: {exc}")


class InstallRequest(BaseModel):
    path: str

@app.get("/")
def serve_index():
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")

@app.get("/api/apps")
def list_apps():
    """List all installed applications."""
    if not os.path.exists(APPS_DIR):
        return {"apps": []}
    
    apps = []
    # Traverse APPS_DIR to find installed applications and their versions
    for app_name in sorted(os.listdir(APPS_DIR)):
        app_path = os.path.join(APPS_DIR, app_name)
        if not os.path.isdir(app_path):
            continue
        versions = get_valid_installed_versions(
            APPS_DIR,
            app_name,
            cleanup_invalid=True,
        )
        if versions:
            version = versions[-1]
            apps.append({"name": app_name, "version": version})

    return {"apps": apps}

def process_installation_queue_sync(target_path: str):
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
            if shutdown_event.is_set():
                log_status("Installation queue stopped because server shutdown is in progress.")
                break

            log_status(f"\\n--- Starting install for: {installer_path} ---")
            try:
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
                _register_process(process)
                
                try:
                    # Stream logs live to connected clients. On shutdown the
                    # shutdown hook terminates the child process, closing this
                    # pipe and allowing the thread to exit.
                    if process.stdout is None:
                        raise RuntimeError("Failed to capture installer output.")
                    for line in process.stdout:
                        if line:
                            log_status(line.strip())
                    process.wait()
                finally:
                    _unregister_process(process)
                
                if process.returncode == 0:
                    log_status(f"Installation successful: {installer_path}")
                elif shutdown_event.is_set():
                    log_status(f"Installation stopped during server shutdown: {installer_path}")
                else:
                    log_status(
                        f"Installation unsuccessful for '{installer_path}': "
                        f"process returned {process.returncode}"
                    )
                    # Primary engine failed -> hand off to MakingScoop's AI
                    # (UI-TARS) automation as a fallback.
                    if not shutdown_event.is_set():
                        forward_to_makingscoop_fallback(installer_path)
                    
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
    if shutdown_event.is_set():
        return {"message": "Server is shutting down; installation was not started.", "path": req.path}

    target_path = req.path
    # Run the installation wrapper in background so we don't block the API event loop
    task = asyncio.create_task(run_in_threadpool(process_installation_queue_sync, target_path))
    active_install_tasks.add(task)
    task.add_done_callback(active_install_tasks.discard)
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
                if shutdown_event.is_set():
                    yield "data: Server shutting down\\n\\n"
                    break
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

    reload_enabled = os.environ.get("SCOOP_RELOAD", "0").lower() in ("1", "true", "yes")
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=reload_enabled)
