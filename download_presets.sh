#!/bin/bash

echo "Setting up enhanced projectM presets..."

# Create directories
mkdir -p ~/.projectM/presets
mkdir -p /tmp/preset_download

cd /tmp/preset_download

# Try to download from multiple sources
echo "Attempting to download presets..."

# Source 1: projectM presets main repository
echo "Downloading from projectM-visualizer/projectm-presets..."
if command -v wget >/dev/null 2>&1; then
    wget -q --timeout=30 --tries=3 https://github.com/projectM-visualizer/projectm-presets/archive/refs/heads/master.zip -O presets1.zip
elif command -v curl >/dev/null 2>&1; then
    curl -s -L --max-time 30 https://github.com/projectM-visualizer/projectm-presets/archive/refs/heads/master.zip -o presets1.zip
fi

# Source 2: Alternative preset collections
echo "Downloading additional preset collections..."
if command -v wget >/dev/null 2>&1; then
    wget -q --timeout=30 --tries=3 https://github.com/projectM-visualizer/milkdrop-presets/archive/refs/heads/master.zip -O presets2.zip
elif command -v curl >/dev/null 2>&1; then
    curl -s -L --max-time 30 https://github.com/projectM-visualizer/milkdrop-presets/archive/refs/heads/master.zip -o presets2.zip
fi

# Extract and install presets
preset_count=0

for zipfile in presets1.zip presets2.zip; do
    if [ -f "$zipfile" ]; then
        echo "Extracting $zipfile..."
        unzip -q "$zipfile"
        
        # Find and copy .milk files
        find . -name "*.milk" -exec cp {} ~/.projectM/presets/ \; 2>/dev/null
        find . -name "*.prjm" -exec cp {} ~/.projectM/presets/ \; 2>/dev/null
        
        echo "Extracted from $zipfile"
    fi
done

# Count installed presets
if [ -d ~/.projectM/presets ]; then
    preset_count=$(ls ~/.projectM/presets/*.milk ~/.projectM/presets/*.prjm 2>/dev/null | wc -l)
    echo "Total presets installed: $preset_count"
    
    # List some preset names
    echo "Sample presets:"
    ls ~/.projectM/presets/ | head -10
else
    echo "Preset directory not found"
fi

# Cleanup
cd /
rm -rf /tmp/preset_download

echo "Preset installation complete!"
echo "Restart Gamp.py to see the new presets"
