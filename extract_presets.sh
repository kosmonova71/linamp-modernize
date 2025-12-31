#!/bin/bash

echo "Extracting projectM preset zip files..."

cd ~/.projectM

# Find and extract all zip files
for zipfile in *.zip; do
    if [ -f "$zipfile" ]; then
        echo "Extracting $zipfile..."
        # Extract to a temporary directory first
        temp_dir="${zipfile%.zip}_extracted"
        mkdir -p "$temp_dir"
        unzip -q "$zipfile" -d "$temp_dir"
        
        # Find and copy all .milk and .prjm files to presets directory
        find "$temp_dir" -name "*.milk" -exec cp {} presets/ \; 2>/dev/null
        find "$temp_dir" -name "*.prjm" -exec cp {} presets/ \; 2>/dev/null
        
        # Clean up temporary directory
        rm -rf "$temp_dir"
        
        echo "Extracted $zipfile"
    fi
done

# Count extracted presets
if [ -d "presets" ]; then
    preset_count=$(ls presets/*.milk presets/*.prjm 2>/dev/null | wc -l)
    echo "Total presets in directory: $preset_count"
    echo "Sample presets:"
    ls presets/ | head -10
else
    echo "No presets directory found"
fi

echo "Extraction complete!"
