#!/usr/bin/env python3
print("Testing imports...")

# Test basic imports
try:
    import datetime
    import json
    import logging
    import os
    import platform
    import re
    import shutil
    import socket
    import ssl
    import subprocess
    import sys
    import threading
    import time
    import urllib.request
    from urllib.parse import urlparse, urlunparse
    import random
    import requests
    print("Basic imports: OK")
except ImportError as e:
    print(f"Basic imports failed: {e}")
    sys.exit(1)

# Test cryptography
try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    print("Cryptography: OK")
except ImportError as e:
    print(f"Cryptography failed: {e}")
    sys.exit(1)

# Test requests
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    print("Requests: OK")
except ImportError as e:
    print(f"Requests failed: {e}")
    sys.exit(1)

# Test stem
try:
    from stem.control import Controller
    print("Stem: OK")
except ImportError as e:
    print(f"Stem failed: {e}")
    sys.exit(1)

# Test gi
try:
    import gi
    print("gi: OK")
except ImportError as e:
    print(f"gi: FAILED - {e}")
    sys.exit(1)
