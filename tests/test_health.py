"""基础健康检查测试。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request

from service.main import create_server


class HealthTest(unittest.TestCase):
    """验证基础服务可以响应。"""

    def test_health(self) -> None:
        server = create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with urllib.request.urlopen(f"http://{host}:{port}/health") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"status": "ok"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
