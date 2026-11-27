from __future__ import annotations

import logging
import socket

import uvicorn

from .ai import make_analyzer
from .app import create_app
from .config import load_config


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 実際には通信しない。使う側のIPが分かるだけ
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config()
    analyzer = make_analyzer(cfg)
    print("=" * 56)
    print(" 修復歴チェッカー を起動しました")
    print(f"  パソコンで開く : http://127.0.0.1:{cfg.port}")
    if cfg.host != "127.0.0.1":
        print(f"  スマホで開く   : http://{lan_ip()}:{cfg.port}  (同じWi-Fiにつないでください)")
    print(f"  AI判定         : {'使えます (' + cfg.model + ')' if analyzer else '未設定(自分の目で判定するモード)'}")
    print("  終了するには、この黒い画面を閉じます")
    print("=" * 56)
    uvicorn.run(create_app(cfg, analyzer), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
