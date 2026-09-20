#!/usr/bin/env python3
"""Release breeze_infer's inference lock when a client disconnects.

breeze_infer.api guards the model with a threading.Lock held for the whole
response, released in the streaming generator's finally:

    if not _request_lock.acquire(blocking=False):
        raise HTTPException(409, "An inference request is already running.")
    def body() -> Iterator[bytes]:
        try: ...
        finally: _request_lock.release()

body() is a synchronous generator, so Starlette iterates it with
iterate_in_threadpool. When the client disconnects, Starlette stops pulling but
never closes the generator -- a sync generator's finally only runs on garbage
collection, and this one is kept alive by the CUDA state it references. The lock
is then held with no owner: the GPU falls idle while every request answers 409,
and only restarting the process clears it.

This rewrites the handler to attach a BackgroundTask that closes the generator
and releases the lock. Starlette runs background tasks after its response task
group exits, which includes the disconnect path, so both happen exactly once and
promptly. Closing the generator matters as much as the release: it raises
GeneratorExit inside the suspended frame, unwinding it and dropping the
references that hold decoder state in VRAM.

Reported upstream at https://github.com/breezeblue-ai/breeze-tts/issues/20; drop this script once a release carries the fix.

Applied at image build time. Verify with:
    python3 patches/release-inference-lock-on-disconnect.py --check
"""

import argparse
import pathlib
import re
import sys

TARGET = "/opt/breeze-tts/breeze_infer/api.py"

SENTINEL = "# --- adapter patch: release the inference lock on disconnect ---"

HELPER = '''
# --- adapter patch: release the inference lock on disconnect ---
def _make_lock_guard():
    """One-shot release bound to a single acquisition.

    Per request, never module-level: releasing a threading.Lock that another
    request has since acquired would hand two callers the model at once.
    """
    import threading as _t
    done = _t.Event()

    def release_once():
        if not done.is_set():
            done.set()
            try:
                _request_lock.release()
            except RuntimeError:
                pass

    return release_once
# --- end adapter patch ---
'''

OLD_BODY_TAIL = """        finally:
            if reference_path is not None:
                reference_path.unlink(missing_ok=True)
            _request_lock.release()

    return StreamingResponse(
        body(),"""

NEW_BODY_TAIL = """        finally:
            if reference_path is not None:
                reference_path.unlink(missing_ok=True)
            _release_guard()

    _stream = body()

    def _cleanup() -> None:
        # Closing first raises GeneratorExit inside the suspended frame, so the
        # finally above runs and drops the decoder state it holds. The release is
        # repeated here because a client that disconnects before the first chunk
        # leaves the generator un-started, where close() runs no finally at all.
        try:
            _stream.close()
        except Exception:
            pass
        _release_guard()

    return StreamingResponse(
        _stream,
        background=BackgroundTask(_cleanup),"""

OLD_ACQUIRE = """    if not _request_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="An inference request is already running."
        )
"""

NEW_ACQUIRE = """    if not _request_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="An inference request is already running."
        )
    _release_guard = _make_lock_guard()
"""

OLD_EARLY_RELEASE = """    except Exception:
        if reference_path is not None:
            reference_path.unlink(missing_ok=True)
        _request_lock.release()
        raise
"""

NEW_EARLY_RELEASE = """    except Exception:
        if reference_path is not None:
            reference_path.unlink(missing_ok=True)
        _release_guard()
        raise
"""


def check(text: str) -> int:
    ok = SENTINEL in text and "BackgroundTask(_cleanup)" in text
    print("patched" if ok else "NOT patched")
    return 0 if ok else 1


def apply(path: pathlib.Path) -> int:
    text = path.read_text()
    if SENTINEL in text:
        print("already patched, nothing to do")
        return 0

    for old in (OLD_ACQUIRE, OLD_EARLY_RELEASE, OLD_BODY_TAIL):
        if old not in text:
            print("upstream source no longer matches; refusing to patch blindly.\n"
                  "The block expected was:\n" + old, file=sys.stderr)
            return 1

    text = text.replace(OLD_ACQUIRE, NEW_ACQUIRE, 1)
    text = text.replace(OLD_EARLY_RELEASE, NEW_EARLY_RELEASE, 1)
    text = text.replace(OLD_BODY_TAIL, NEW_BODY_TAIL, 1)

    # Helper goes right after the lock it guards.
    anchor = "_request_lock = threading.Lock()\n"
    if anchor not in text:
        print("lock definition not found", file=sys.stderr)
        return 1
    text = text.replace(anchor, anchor + HELPER, 1)

    if "from starlette.background import BackgroundTask" not in text:
        match = re.search(r"^from fastapi import .*$", text, re.M)
        if not match:
            print("no fastapi import to anchor to", file=sys.stderr)
            return 1
        text = (text[:match.end()]
                + "\nfrom starlette.background import BackgroundTask"
                + text[match.end():])

    compile(text, str(path), "exec")
    path.write_text(text)
    print(f"patched {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--path", default=TARGET)
    args = parser.parse_args()
    path = pathlib.Path(args.path)
    if not path.exists():
        print(f"{path} not found", file=sys.stderr)
        return 1
    return check(path.read_text()) if args.check else apply(path)


if __name__ == "__main__":
    sys.exit(main())
