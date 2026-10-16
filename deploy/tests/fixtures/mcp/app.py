"""Disposable MCP protocol fixture; contains no real credentials or tool actions."""
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        healthy = os.environ.get('FAIL_HEALTH') != 'true'
        self.reply(200 if healthy else 503, {'ok': healthy, 'version': os.environ.get('VERSION')})

    def do_POST(self):
        if self.headers.get('Authorization') != 'Bearer ' + os.environ['TEST_API_KEY']:
            self.reply(401, {'error': 'unauthorized'})
        else:
            self.reply(200, {'jsonrpc': '2.0', 'id': 1, 'result': {'tools': [{'name': 'fixture', 'inputSchema': {'type': 'object'}}]}})


signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
HTTPServer(('0.0.0.0', int(os.environ['PORT'])), Handler).serve_forever()
