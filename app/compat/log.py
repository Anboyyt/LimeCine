"""Logger shim：替代 MoviePilot 的 app.log，保持 logger 接口一致。"""
import logging
import sys

logger = logging.getLogger("lime-cover")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False
