#!/usr/bin/env python3

import runpy
import sys


sys.argv = [sys.argv[0], "--collect"] + sys.argv[1:]
runpy.run_path(str(__import__("pathlib").Path(__file__).resolve().parent / "app.py"), run_name="__main__")
