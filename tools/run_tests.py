"""跑全量测试的统一入口。

用法：
    ./.venv/Scripts/python.exe tools/run_tests.py

为什么不用 `python -m unittest discover`：
  这台机器同时跑着模拟器、脚本和编辑器，内存本来就紧。
  OpenBLAS 默认按 CPU 核数开线程，每个线程都要预留缓冲区，
  于是偶尔直接抛
      OpenBLAS error: Memory allocation still failed after 10 retries
  整个测试文件崩掉，看着像代码坏了，其实是线程池开太大。

  这些变量必须在 numpy / cv2 导入之前设置，所以只能放在进程入口。
"""
import os
import sys

for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
