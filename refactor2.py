import sys

with open('server.py', 'r') as f:
    code = f.read()

old_code = """            try:
                from myscoop.local_manifest import LocalManifestManager
                import subprocess

                # Ensure manifest exists and get the normalized app_name
                manager = LocalManifestManager(buckets_dir)
                app_name = manager.ensure_manifest(str(exe_path))

                cmd = [sys.executable, "myscoop.py", "install", app_name, "--file", str(exe_path)]
                log_status(f"Running cmd: {' '.join(cmd)}")
                
                # Execute CLI command as a subprocess
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=os.path.dirname(os.path.abspath(__file__))
                )
                
                # Stream logs live to connected clients
                for line in process.stdout:
                    if line:
                        log_status(line.strip())
                        
                process.wait()
                
                if process.returncode == 0:
                    log_status(f"Successfully installed: {exe_path}")
                else:
                    log_status(f"Failed to install '{exe_path}': process returned {process.returncode}")
                    
            except Exception as e:
                # Catch error so it continues to next app
                log_status(f"Failed to install '{exe_path}': {str(e)}")
                # traceback.print_exc()"""

new_code = """            try:
                import subprocess

                # Pass the exe path directly to the CLI.
                # The CLI's _resolve_install_target will find the best
                # matching manifest (e.g. 'abb' with gui installer type)
                # and handle the full install flow including GUI automation.
                cmd = [sys.executable, "myscoop.py", "install", str(exe_path)]
                log_status(f"Running cmd: {' '.join(cmd)}")
                
                # Execute CLI command as a subprocess
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    cwd=os.path.dirname(os.path.abspath(__file__))
                )
                
                # Stream logs live to connected clients
                for line in process.stdout:
                    if line:
                        log_status(line.strip())
                        
                process.wait()
                
                if process.returncode == 0:
                    log_status(f"Successfully installed: {exe_path}")
                else:
                    log_status(f"Failed to install '{exe_path}': process returned {process.returncode}")
                    
            except Exception as e:
                # Catch error so it continues to next app
                log_status(f"Failed to install '{exe_path}': {str(e)}")
                # traceback.print_exc()"""

found = old_code in code
print(f"Old code found: {found}")
if found:
    with open('server.py', 'w') as f:
        f.write(code.replace(old_code, new_code))
    print("Replacement done.")
else:
    print("NOT FOUND")
