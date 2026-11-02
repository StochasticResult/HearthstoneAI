"""Use the exact authorized native app executable for every input operation."""
import os
from pathlib import Path


def input_command(*args):
    executable = Path(os.environ.get("HEARTHSTONE_AI_NATIVE", str(Path.home() / "Applications/Hearthstone AI.app/Contents/MacOS/HearthstoneAI")))
    if not str(executable).endswith(".app/Contents/MacOS/HearthstoneAI"):
        raise RuntimeError("Invalid native application input component")
    return [str(executable), "--input", *map(str, args)]
