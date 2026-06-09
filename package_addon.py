import os
import zipfile

def package_addon():
    addon_name = "upframeg"
    output_filename = f"{addon_name}.zip"
    
    # Excluded items
    ignored_extensions = {".pyc", ".pyo", ".pyd"}
    ignored_folders = {"__pycache__", ".git", ".github", ".vscode", "ai_binaries"}
    
    print(f"Packaging {addon_name} addon into {output_filename}...")
    
    if os.path.exists(output_filename):
        try:
            os.remove(output_filename)
        except Exception as e:
            print(f"Warning: Could not remove existing zip file: {e}")
        
    with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zip_ref:
        count = 0
        for root, dirs, files in os.walk(addon_name):
            # Prune ignored folders in-place
            dirs[:] = [d for d in dirs if d not in ignored_folders]
            
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in ignored_extensions:
                    continue
                    
                file_path = os.path.join(root, file)
                # Store paths starting with the addon directory name
                arcname = file_path
                zip_ref.write(file_path, arcname)
                print(f"  Added: {arcname}")
                count += 1
                
    print(f"Success! Packaged {count} files into {output_filename}.")

if __name__ == "__main__":
    package_addon()
