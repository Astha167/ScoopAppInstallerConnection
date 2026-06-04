import sys
import os

with open('server.py', 'r') as f:
    code = f.read()

old_code = '''            try:
                # Same resolution logic as cli.py install command
                app_name, local_file = _resolve_install_target(str(exe_path), str(exe_path), buckets_dir)
                
                resolver = DependencyResolver(APPS_DIR, buckets_dir)
                install_order = resolver.resolve(app_name)

                for i, dep_name in enumerate(install_order):
                    is_dep = (dep_name.lower() != app_name.lower())
                    if is_dep and resolver.is_installed(dep_name):
                        log_status(f"Dependency '{dep_name}' already installed.")
                        continue
                    
                    file_arg = local_file if not is_dep else None
                    install_single_app(dep_name, buckets_dir, is_dependency=is_dep, local_file=file_arg)
                    
                log_status(f"Successfully installed: {exe_path}")
            except Exception as e:
                # Catch error so it continues to next app
                log_status(f"Failed to install '{exe_path}': {str(e)}")
                # traceback.print_exc()'''

new_code = '''            try:
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
                # traceback.print_exc()'''

print(f"Old code found: {old_code in code}")
with open('server.py', 'w') as f:
    f.write(code.replace(old_code, new_code))

