#!/usr/bin/env python3
"""Build a local AppKit bundle without downloading dependencies."""
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dist/Hearthstone AI.app"


def build_awake(destination):
    subprocess.run(["/usr/bin/xcrun", "clang", "-Os", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "native/KeepAwake.c"), "-framework", "CoreFoundation",
                    "-framework", "IOKit", "-o", str(destination)], check=True)


def build_focus(destination):
    subprocess.run(["/usr/bin/xcrun", "clang", "-Os", "-Wall", "-Wextra", "-Werror",
                    "-Wno-deprecated-declarations", str(ROOT / "native/RankedFocus.c"),
                    "-framework", "Carbon", "-framework", "ApplicationServices", "-o", str(destination)], check=True)
    subprocess.run(["/usr/bin/xcrun", "clang", "-dynamiclib", "-Os", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "native/PointerTarget.c"), "-framework", "ApplicationServices",
                    "-o", str(destination.with_name('pointer-target.dylib'))], check=True)


def main():
    if APP.exists():
        shutil.rmtree(APP)
    macos = APP / "Contents/MacOS"
    resources = APP / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    subprocess.run(["/usr/bin/xcrun", "clang", "-fobjc-arc", "-framework", "Foundation", "-framework", "AppKit",
                    str(ROOT / "native/Mirror.m"), "-o", str(ROOT / "runtime/mirror-status")], check=True)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements",
                    str(ROOT / "native/Mirror.entitlements.plist"), str(ROOT / "runtime/mirror-status")], check=True)
    subprocess.run(["/usr/bin/xcrun", "swiftc", "-parse-as-library", "-num-threads", "1", str(ROOT / "native/App.swift"),
                    str(ROOT / "native/InputCommands.swift"),
                    "-o", str(macos / "HearthstoneAI")], check=True)
    shutil.copytree(ROOT / "runtime", resources / "runtime", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    build_awake(resources / "runtime/app-awake")
    build_focus(resources / "runtime/ranked-focus")
    with (APP / "Contents/Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleName": "Hearthstone AI", "CFBundleDisplayName": "Hearthstone AI",
            "CFBundleIdentifier": "local.hearthstoneai.app", "CFBundleExecutable": "HearthstoneAI",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "2",
            "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Local experimental project. Includes MIT-licensed hearthstone-cli parser."}, f)
    # Do not deep re-sign the separately signed HearthMirror helper.
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(APP)], check=True)
    print(APP)


if __name__ == "__main__":
    main()
