"""测试包：在任何 app 模块被导入之前，把数据目录切到临时目录。

不这么做的话，跑一次测试就会在用户真实的 data/ 里写进
history.json / memory.json / scenes.json，甚至改掉 config——
测试残留和真实数据混在一起，排查问题时根本分不清是谁产生的。
"""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("XIAOAI_DATA_DIR"):
    _TMP = tempfile.mkdtemp(prefix="xiaoai-tests-")
    os.environ["XIAOAI_DATA_DIR"] = _TMP
    atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
