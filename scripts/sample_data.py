"""Lead generation now lives in the package so the daily run can use it."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from cdp.sample_data import *  # noqa: E402,F401,F403
