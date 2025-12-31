#!/usr/bin/env python3

import zipfile
import shutil
from pathlib import Path

def extract_projectm_presets():
    """Extract all zip files in .projectM directory to presets folder"""
    
    projectm_dir = Path.home() / ".projectM"
    presets_dir = projectm_dir / "presets"
    
    print(f"projectM directory: {projectm_dir}")
    print(f"Presets directory: {presets_dir}")
    
    # Ensure presets directory exists
    presets_dir.mkdir(parents=True, exist_ok=True)
    
    # Find and extract all zip files
    zip_files = list(projectm_dir.glob("*.zip"))
    print(f"Found {len(zip_files)} zip files")
    
    total_extracted = 0
    
    for zip_file in zip_files:
        print(f"Extracting: {zip_file.name}")
        try:
            with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                # Extract all .milk and .prjm files
                for file_info in zip_ref.filelist:
                    if file_info.filename.lower().endswith(('.milk', '.prjm')):
                        # Extract to temp location first
                        extracted_path = zip_ref.extract(file_info, projectm_dir / "temp_extract")
                        
                        # Copy to presets directory with just the filename
                        final_path = presets_dir / Path(file_info.filename).name
                        shutil.move(str(extracted_path), str(final_path))
                        total_extracted += 1
                
                # Clean up temp directory
                temp_dir = projectm_dir / "temp_extract"
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)
                    
            print(f"  ✓ Extracted {zip_file.name}")
            
            # Optionally move the zip file to processed folder
            processed_dir = projectm_dir / "processed"
            processed_dir.mkdir(exist_ok=True)
            shutil.move(str(zip_file), str(processed_dir / zip_file.name))
            
        except Exception as e:
            print(f"  ✗ Error extracting {zip_file.name}: {e}")
    
    # Count final presets
    final_presets = list(presets_dir.glob("*.milk")) + list(presets_dir.glob("*.prjm"))
    print("Final count: {} preset files".format(len(final_presets)))
    
    if final_presets:
        print("Sample presets:")
        for preset in final_presets[:10]:
            print(f"  - {preset.name}")
        print("\n✓ Presets ready! Restart Gamp.py to use them.")
    else:
        print("⚠ No preset files found")
    
    return len(final_presets)

if __name__ == "__main__":
    extract_projectm_presets()
