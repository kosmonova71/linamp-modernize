#!/bin/bash

# Create projectM directories
echo "Creating projectM directories..."
mkdir -p ~/.projectM/presets
mkdir -p /tmp/projectm_download

# Download some basic presets from projectM repository
echo "Downloading projectM presets..."
cd /tmp/projectm_download

# Try to download from projectM GitHub
if command -v wget >/dev/null 2>&1; then
    echo "Using wget to download presets..."
    wget -q https://github.com/projectM-visualizer/projectm-presets/archive/refs/heads/master.zip -O presets.zip
elif command -v curl >/dev/null 2>&1; then
    echo "Using curl to download presets..."
    curl -s -L https://github.com/projectM-visualizer/projectm-presets/archive/refs/heads/master.zip -o presets.zip
else
    echo "Neither wget nor curl available. Please install one of them."
    exit 1
fi

# Extract presets if download was successful
if [ -f presets.zip ]; then
    echo "Extracting presets..."
    unzip -q presets.zip
    
    # Copy preset files to user directory
    if [ -d "projectm-presets-master" ]; then
        echo "Installing presets to ~/.projectM/presets..."
        find projectm-presets-master -name "*.milk" -exec cp {} ~/.projectM/presets/ \;
        find projectm-presets-master -name "*.prjm" -exec cp {} ~/.projectM/presets/ \;
        
        echo "Installation complete!"
        echo "Presets installed in: ~/.projectM/presets"
        echo "Number of presets: $(ls ~/.projectM/presets/ | wc -l)"
    else
        echo "Failed to extract presets"
    fi
    
    # Cleanup
    rm -rf presets.zip projectm-presets-master
    cd /
    rm -rf /tmp/projectm_download
else
    echo "Failed to download presets"
fi
