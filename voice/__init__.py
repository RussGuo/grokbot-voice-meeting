"""voice/ 作为包被 main.py 导入时（`from voice import adapters`），把本目录加进 sys.path，
使内部的顶层包 `voice_layer` 可导入。"""
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)
