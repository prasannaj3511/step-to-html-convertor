#!/usr/bin/env python3
"""Serve this folder over HTTP so the browser will load the model.

Opening index.html straight from disk trips the browser's file:// security
rules, which block reading the .glb. Running this script sidesteps that.

    python serve.py            # then open the printed address
    python serve.py 9000       # use a specific port
"""
import http.server
import os
import socketserver
import sys
import webbrowser

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
os.chdir(os.path.dirname(os.path.abspath(__file__)))


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
    }

    def end_headers(self):
        # These let the browser cache the (immutable) model between reloads.
        self.send_header("Cache-Control", "public, max-age=3600")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("127.0.0.1", PORT), Handler) as httpd:
    url = f"http://127.0.0.1:{PORT}/"
    print(f"iSTP2HTML viewer serving at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
