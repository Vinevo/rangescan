import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK - Bybit Scanner is alive")

    def log_message(self, format, *args):
        pass  # Отключаем логи HTTP запросов


def keep_alive():
    """Запускаем HTTP сервер в отдельном потоке."""
    port = 8080
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"🌐 Keep-alive сервер запущен на порту {port}")
