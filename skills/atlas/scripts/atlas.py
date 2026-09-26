#!/usr/bin/env python3
"""Атлас — память и карта проекта (этап M0). Точка входа: python3 atlas.py <команда>."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atlaskit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
