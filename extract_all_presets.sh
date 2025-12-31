#!/bin/bash

echo "=== projectM Preset Extraction Tool ==="
echo "Working directory: ~/.projectM"

# Navigate to projectM directory
cd ~/.projectM 2>/dev/null || cd "$HOME/.projectM" 2>/dev/null || {
    echo "Error: Cannot access ~/.projectM directory"
    exit 1
}

echo "Current directory contents:"
ls -la

# Ensure presets directory exists
mkdir -p presets

# Extract all zip files
echo "Looking for zip files..."
zip_count=0
for zipfile in *.zip; do
    if [ -f "$zipfile" ]; then
        echo "Processing: $zipfile"
        
        # Create extraction directory
        extract_dir="${zipfile%.zip}_extracted"
        mkdir -p "$extract_dir"
        
        # Extract zip file
        if unzip -q "$zipfile" -d "$extract_dir"; then
            echo "  ✓ Extracted successfully"
            
            # Copy all preset files
            milk_files=$(find "$extract_dir" -name "*.milk" | wc -l)
            prjm_files=$(find "$extract_dir" -name "*.prjm" | wc -l)
            
            if [ $milk_files -gt 0 ] || [ $prjm_files -gt 0 ]; then
                find "$extract_dir" -name "*.milk" -exec cp {} presets/ \;
                find "$extract_dir" -name "*.prjm" -exec cp {} presets/ \;
                echo "  ✓ Found $milk_files .milk files and $prjm_files .prjm files"
            else
                echo "  ⚠ No preset files found in zip"
            fi
            
            # Clean up
            rm -rf "$extract_dir"
        else
            echo "  ✗ Failed to extract $zipfile"
        fi
        
        zip_count=$((zip_count + 1))
    fi
done

echo "Processed $zip_count zip files"

# Count total presets
if [ -d "presets" ]; then
    total_presets=$(find presets -name "*.milk" -o -name "*.prjm" | wc -l)
    echo ""
    echo "=== Final Results ==="
    echo "Total presets available: $total_presets"
    
    if [ $total_presets -gt 0 ]; then
        echo "Sample presets:"
        ls presets/ | head -10
        echo ""
        echo "✓ Presets are ready! Restart Gamp.py to use them."
    else
        echo "⚠ No preset files found"
    fi
else
    echo "Error: presets directory not created"
fi

echo "Extraction complete!"
