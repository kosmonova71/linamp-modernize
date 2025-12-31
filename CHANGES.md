2025-12-31 - Improvements

- Make logging robust: ensure log directory exists and handle FileHandler errors gracefully
- Replace print() calls with structured logging via `logger` (info/warning/error)
- Allow importing in headless/test environments by avoiding sys.exit on missing GTK/GStreamer
- Remove duplicate return in `_clear_status_message`
- Add basic unit tests for `get_file_metadata` and `Config` save/load
