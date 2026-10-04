"""Vendor hook dedup-guard installer/uninstaller and integrity check.

The guard itself lives in hook_guard.sh next to this module.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .paths import get_state_dir, get_vendor_hooks_dir

_GUARD_RESOURCE = Path(__file__).with_name("hook_guard.sh")


_guard_template_cache: str | None = None


def _load_guard_template() -> str:
    text = _GUARD_RESOURCE.read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text


def get_hook_guard_template() -> str:
    """Return the guard shell block, reading hook_guard.sh lazily on first use.

    Deliberately not done at import: a missing/unreadable resource must only break
    hook install/repatch, never the Herdr event hot path or --cleanup.
    Raises OSError when the resource cannot be read.
    """
    global _guard_template_cache
    if _guard_template_cache is None:
        _guard_template_cache = _load_guard_template()
    return _guard_template_cache


def __getattr__(name: str):
    # PEP 562: keep `from herdr_bartender.hooks import HOOK_GUARD_TEMPLATE` working
    # without an import-time file read.
    if name == "HOOK_GUARD_TEMPLATE":
        return get_hook_guard_template()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def verify_vendor_hooks_intact() -> tuple[bool, list[str]]:
    hooks_dir = get_vendor_hooks_dir()
    if not hooks_dir.exists():
        return False, []
    named = ["claude-event-hook.sh", "codex-notify-hook.sh"]
    missing = []
    for name in named:
        hf = hooks_dir / name
        if not hf.exists():
            continue
        try:
            with open(hf, "r", encoding="utf-8") as f:
                content = f.read()
            if "# BEGIN HERDR-BARTENDER DEDUP GUARD" not in content or "# END HERDR-BARTENDER DEDUP GUARD" not in content:
                missing.append(name)
        except Exception:
            missing.append(name)
    return (len(missing) == 0), missing


def install_hooks() -> bool:
    import hashlib
    hooks_dir = get_vendor_hooks_dir()
    if not hooks_dir.exists():
        print(f"[-] Vendor hooks directory not found: {hooks_dir}")
        return False

    # Create sticky NO_HOOKS opt-out file to prevent reconciler from auto re-patching
    try:
        state_dir = get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "NO_HOOKS").touch(exist_ok=True)
    except Exception:
        pass
    named = ["claude-event-hook.sh", "codex-notify-hook.sh"]
    hook_files = [hooks_dir / name for name in named if (hooks_dir / name).exists()]
    if not hook_files:
        print(f"[-] No target hook scripts found in {hooks_dir}")
        return False

    try:
        guard_template = get_hook_guard_template()
    except OSError as e:
        print(f"[-] Cannot read guard template {_GUARD_RESOURCE}: {e}")
        return False

    state_dir = get_state_dir()
    sha_file = state_dir / "vendor-hook-sha.json"
    sha_cache = {}
    if sha_file.exists():
        try:
            with open(sha_file, "r", encoding="utf-8") as f:
                sha_cache = json.load(f)
        except Exception:
            sha_cache = {}

    # Remove NO_HOOKS opt-out file and HOOK_NEEDS_REVIEW on explicit install
    try:
        (state_dir / "NO_HOOKS").unlink(missing_ok=True)
        (state_dir / "HOOK_NEEDS_REVIEW").unlink(missing_ok=True)
        (state_dir / ".hook_review_alerted").unlink(missing_ok=True)
    except Exception:
        pass
    all_ok = True
    begin_marker = "# BEGIN HERDR-BARTENDER DEDUP GUARD"
    end_marker = "# END HERDR-BARTENDER DEDUP GUARD"

    for hook in hook_files:
        try:
            orig_mode = os.stat(hook).st_mode
            with open(hook, "r", encoding="utf-8") as f:
                content = f.read()

            clean_content = content
            if begin_marker in content and end_marker in content:
                clean_content = content.split(begin_marker)[0] + content.split(end_marker)[1]
            sha_cache[hook.name] = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()

            target_mode = orig_mode | 0o100
            bak_path = hook.with_suffix(hook.suffix + ".pristine")
            if not bak_path.exists() and begin_marker not in content:
                with open(bak_path, "w", encoding="utf-8") as bf:
                    bf.write(content)
                os.chmod(bak_path, target_mode)
            elif bak_path.exists():
                try:
                    with open(bak_path, "r", encoding="utf-8") as bf:
                        old_pristine = bf.read()
                    if old_pristine != clean_content:
                        with open(bak_path, "w", encoding="utf-8") as bf:
                            bf.write(clean_content)
                except Exception:
                    pass

            if begin_marker in content and end_marker in content:
                pre = content.split(begin_marker)[0]
                post = content.split(end_marker)[1]
                new_content = f"{pre}{guard_template}{post}"
            else:
                lines = content.splitlines(keepends=True)
                insert_idx = 0
                for idx, line in enumerate(lines):
                    if line.strip() == "set -u":
                        insert_idx = idx + 1
                        break
                    elif line.startswith("#!") and insert_idx == 0:
                        insert_idx = idx + 1

                if insert_idx == 0:
                    print(f"[-] Aborting patch for {hook.name}: no valid insertion anchor (shebang/set -u) found")
                    all_ok = False
                    continue

                new_lines = lines[:insert_idx] + [f"\n{guard_template}\n"] + lines[insert_idx:]
                new_content = "".join(new_lines)

            tmp_path = hook.with_suffix(f"{hook.suffix}.tmp.{os.getpid()}")
            with open(tmp_path, "w", encoding="utf-8") as tf:
                tf.write(new_content)
            os.chmod(tmp_path, target_mode)

            check = subprocess.run(["bash", "-n", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if check.returncode != 0:
                print(f"[-] Syntax validation failed for {hook.name}: {check.stderr.decode('utf-8')}")
                tmp_path.unlink(missing_ok=True)
                all_ok = False
                continue

            with open(hook, "r", encoding="utf-8") as f_check:
                current_disk_content = f_check.read()
            if current_disk_content != content:
                print(f"[-] Aborting patch for {hook.name}: file modified on disk during patch preparation")
                tmp_path.unlink(missing_ok=True)
                all_ok = False
                continue

            os.replace(tmp_path, hook)
            print(f"[+] Installed dedup guard in {hook.name}")
        except Exception as e:
            print(f"[-] Error installing guard in {hook.name}: {e}")
            all_ok = False

    try:
        with open(sha_file, "w", encoding="utf-8") as f:
            json.dump(sha_cache, f, indent=2)
    except Exception:
        pass

    return all_ok


def uninstall_hooks() -> bool:
    hooks_dir = get_vendor_hooks_dir()
    if not hooks_dir.exists():
        return True

    named = ["claude-event-hook.sh", "codex-notify-hook.sh"]
    hook_files = [hooks_dir / name for name in named if (hooks_dir / name).exists()]
    if not hook_files:
        return True

    begin_marker = "# BEGIN HERDR-BARTENDER DEDUP GUARD"
    end_marker = "# END HERDR-BARTENDER DEDUP GUARD"
    all_ok = True

    for hook in hook_files:
        try:
            orig_mode = os.stat(hook).st_mode
            with open(hook, "r", encoding="utf-8") as f:
                content = f.read()

            if begin_marker not in content or end_marker not in content:
                continue

            clean_content = content.split(begin_marker)[0] + content.split(end_marker)[1]
            tmp_path = hook.with_suffix(f"{hook.suffix}.tmp.{os.getpid()}")
            with open(tmp_path, "w", encoding="utf-8") as tf:
                tf.write(clean_content)
            os.chmod(tmp_path, orig_mode)

            check = subprocess.run(["bash", "-n", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if check.returncode != 0:
                print(f"[-] Syntax validation failed after removing guard from {hook.name}")
                tmp_path.unlink(missing_ok=True)
                all_ok = False
                continue

            with open(hook, "r", encoding="utf-8") as f_check:
                current_disk_content = f_check.read()
            if current_disk_content != content:
                print(f"[-] Aborting removal for {hook.name}: file modified on disk during preparation")
                tmp_path.unlink(missing_ok=True)
                all_ok = False
                continue

            os.replace(tmp_path, hook)
            bak_path = hook.with_suffix(hook.suffix + ".pristine")
            bak_path.unlink(missing_ok=True)
            print(f"[+] Uninstalled dedup guard from {hook.name}")
        except Exception as e:
            print(f"[-] Error uninstalling guard from {hook.name}: {e}")
            all_ok = False

    sha_file = get_state_dir() / "vendor-hook-sha.json"
    sha_file.unlink(missing_ok=True)
    return all_ok
