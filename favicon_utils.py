import os
import re
import json
import urllib.parse
from gi.repository import GLib, Gio, Gdk, GdkPixbuf
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Cache for favicons to avoid repeated downloads
FAVICON_CACHE = {}
CACHE_FILE = os.path.expanduser("~/.cache/shadowbrowser/favicon_cache.json")
CACHE_DIR = os.path.dirname(CACHE_FILE)

# Common favicon paths to check
FAVICON_PATHS = [
    "/favicon.ico",
    "/favicon.png",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png"
]

# Create cache directory if it doesn't exist
if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR, exist_ok=True)

# Load favicon cache from disk
def load_favicon_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}

# Save favicon cache to disk
def save_favicon_cache():
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(FAVICON_CACHE, f)
    except IOError:
        pass

# Initialize the cache
FAVICON_CACHE = load_favicon_cache()

# Session with retries for favicon downloads
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount('http://', HTTPAdapter(max_retries=retries))
session.mount('https://', HTTPAdapter(max_retries=retries))

def get_favicon_url(html_content, base_url):
    """Extract favicon URL from HTML content."""
    if not html_content or not base_url:
        return None
        
    # Look for favicon in link tags
    icon_links = re.findall(
        r'<link[^>]+rel=[\'"](?:icon|shortcut icon|apple-touch-icon)[\'"][^>]*>',
        html_content,
        re.IGNORECASE
    )
    
    for link in icon_links:
        # Extract href attribute
        match = re.search(r'href=[\'"]([^\'"]+)[\'"]', link)
        if match:
            return urllib.parse.urljoin(base_url, match.group(1))
    
    return None

def download_favicon(url):
    """Download favicon from URL and return as bytes."""
    try:
        response = session.get(url, timeout=5, headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        response.raise_for_status()
        return response.content
    except Exception:
        return None

def favicon_to_paintable(favicon_data):
    """Convert favicon bytes to Gdk.Paintable."""
    if not favicon_data:
        return None
        
    try:
        stream = Gio.MemoryInputStream.new_from_bytes(
            GLib.Bytes.new(favicon_data)
        )
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(stream, None)
        if pixbuf:
            return Gdk.Texture.new_for_pixbuf(pixbuf)
    except Exception:
        pass
    return None

def get_favicon_for_url(url, callback):
    """Get favicon for a URL, using cache if available."""
    if not url or not url.startswith(('http://', 'https://')):
        GLib.idle_add(callback, None)
        return
    
    # Try to get from cache first
    domain = urllib.parse.urlparse(url).netloc
    if domain in FAVICON_CACHE:
        favicon_data = FAVICON_CACHE[domain]
        paintable = favicon_to_paintable(favicon_data)
        if paintable:
            GLib.idle_add(callback, paintable)
            return
    
    # Not in cache, fetch it
    def fetch_and_cache():
        # Try to get favicon from common locations
        base_url = f"{urllib.parse.urlparse(url).scheme}://{domain}"
        
        # Try to fetch HTML first to find favicon
        try:
            response = session.get(url, timeout=5, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            if response.status_code == 200:
                favicon_url = get_favicon_url(response.text, response.url)
                if favicon_url:
                    favicon_data = download_favicon(favicon_url)
                    if favicon_data:
                        FAVICON_CACHE[domain] = favicon_data
                        save_favicon_cache()
                        paintable = favicon_to_paintable(favicon_data)
                        if paintable:
                            GLib.idle_add(callback, paintable)
                            return
        except Exception:
            pass
        
        # If no favicon found in HTML, try common locations
        for path in FAVICON_PATHS:
            favicon_url = f"{base_url}{path}"
            favicon_data = download_favicon(favicon_url)
            if favicon_data:
                FAVICON_CACHE[domain] = favicon_data
                save_favicon_cache()
                paintable = favicon_to_paintable(favicon_data)
                if paintable:
                    GLib.idle_add(callback, paintable)
                    return
        
        # If we get here, no favicon was found
        GLib.idle_add(callback, None)
    
    # Run in a separate thread to avoid blocking the UI
    import threading
    threading.Thread(target=fetch_and_cache, daemon=True).start()

def clear_favicon_cache():
    """Clear the favicon cache."""
    global FAVICON_CACHE
    FAVICON_CACHE = {}
    if os.path.exists(CACHE_FILE):
        os.unlink(CACHE_FILE)
