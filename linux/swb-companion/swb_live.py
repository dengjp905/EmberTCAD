#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, approval-facing adapter around the official Sentaurus SWB Python API."""

import argparse
import ctypes
import datetime as dt
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time


DEFAULT_SENTAURUS_ROOT = "/usr/synopsys/sentaurus/O_2018.06-SP2"
DEFAULT_LICENSE = "/usr/synopsys/scl/2018.06-SP1/admin/license/license.dat"
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
VALUE_RE = re.compile(r"^[A-Za-z0-9_+.:/() -]{1,128}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
RUN_STATE_ROOT = os.path.realpath(os.environ.get(
    "AITCAD_RUN_STATE_ROOT", os.path.expanduser("~/.local/state/aitcad/live-runs"),
))


def emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def project_path(value):
    path = os.path.realpath(value)
    if not os.path.isfile(os.path.join(path, "gtree.dat")):
        raise RuntimeError("Not an SWB project: %s" % path)
    home = os.path.realpath(os.path.expanduser("~"))
    if path != home and not path.startswith(home + os.sep):
        raise RuntimeError("Live project must be located under the current user's home directory")
    return path


def _run_state_path(run_id):
    run_id = str(run_id or "")
    if not RUN_ID_RE.match(run_id):
        raise RuntimeError("Invalid run id")
    return os.path.join(RUN_STATE_ROOT, run_id + ".json")


def _save_run_state(state):
    os.makedirs(RUN_STATE_ROOT, mode=0o700, exist_ok=True)
    path = _run_state_path(state.get("runId"))
    temporary = path + ".tmp-%d" % os.getpid()
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def _load_run_state(run_id):
    path = _run_state_path(run_id)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        raise RuntimeError("Run registry was not found: %s" % run_id)


def _process_matches_run(state):
    pid = int(state.get("pid") or 0)
    if pid <= 1:
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as stream:
            command = stream.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "gsub" in command and os.path.realpath(state.get("project") or "") in command


def stop_run(project, run_id):
    """Immediately stop only the registered gsub process group for one run."""
    project = project_path(project)
    state = _load_run_state(run_id)
    if os.path.realpath(state.get("project") or "") != project:
        raise RuntimeError("Run registry belongs to another project")
    pid = int(state.get("pid") or 0)
    pgid = int(state.get("pgid") or pid)
    if not _process_matches_run(state):
        state.update({"status": "not-running", "stoppedAt": dt.datetime.utcnow().isoformat() + "Z"})
        _save_run_state(state)
        return {"ok": True, "action": "stop-run", "runId": run_id, "pid": pid, "stopped": False, "alreadyExited": True}
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 2.0
    while time.time() < deadline and _process_matches_run(state):
        time.sleep(0.1)
    forced = False
    if _process_matches_run(state):
        forced = True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    state.update({
        "status": "cancelled", "forced": forced,
        "stoppedAt": dt.datetime.utcnow().isoformat() + "Z",
    })
    _save_run_state(state)
    return {
        "ok": True, "action": "stop-run", "runId": run_id, "pid": pid,
        "pgid": pgid, "stopped": True, "forced": forced,
    }


def run_gtcl(project, body):
    sentaurus_root = os.environ.get("AITCAD_RUN_STROOT", DEFAULT_SENTAURUS_ROOT)
    gtclsh = os.path.join(sentaurus_root, "bin", "gtclsh")
    if not os.path.isfile(gtclsh):
        raise RuntimeError("gtclsh not found: %s" % gtclsh)
    tree_file = os.path.join(project, "gtree.dat").replace("}", "\\}")
    script = """::gtree::New aitcad
::gtree::Load {%s}
if {[catch {
%s
} __aitcad_error]} {
    puts stderr "__AITCAD_ERROR__ $__aitcad_error"
    exit 2
}
""" % (tree_file, body)
    environment = os.environ.copy()
    environment["STROOT"] = sentaurus_root
    environment["STDB"] = os.environ.get("AITCAD_RUN_STDB") or os.path.dirname(os.path.dirname(project))
    environment["PATH"] = os.path.join(sentaurus_root, "bin") + os.pathsep + environment.get("PATH", "")
    process = subprocess.Popen(
        [gtclsh], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, text=True,
    )
    stdout, stderr = process.communicate(script)
    if process.returncode:
        raise RuntimeError(stderr.strip() or stdout.strip() or "gtclsh failed")
    return stdout


def parse_values(value):
    values = [item.strip() for item in value.replace(";", ",").split(",")]
    values = [item for item in values if item]
    if not values:
        raise RuntimeError("At least one parameter value is required")
    for item in values:
        if not VALUE_RE.match(item):
            raise RuntimeError("Invalid SWB parameter value: %s" % item)
    return values


def tcl_list(values):
    return "[list %s]" % " ".join("{%s}" % item.replace("}", "\\}") for item in values)


def swb_window_inventory():
    """Return visible SWB top-level window ids and titles."""
    try:
        output = subprocess.check_output(
            ["xwininfo", "-root", "-tree"], stderr=subprocess.DEVNULL, text=True
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    windows = []
    for line in output.splitlines():
        if "SWB@" not in line:
            continue
        match = re.search(r"^\s*(0x[0-9a-fA-F]+)\s+\"([^\"]+)\"", line)
        if not match:
            continue
        windows.append((int(match.group(1), 16), match.group(2)))
    return windows


def find_swb_windows(project, allow_other=False):
    """Return SWB windows that visibly own ``project``.

    Older code fell back to every SWB window.  That made a refresh look like a
    live link even when the generated project had never been opened.  Match the
    full path or project basename; callers may explicitly request the old
    fallback only for diagnostics.
    """
    project = os.path.realpath(project)
    basename = os.path.basename(project).lower()
    inventory = swb_window_inventory()
    matches = []
    for window, title in inventory:
        lowered = title.lower()
        if project.lower() in lowered or re.search(
                r"(?:^|[/\\\s])%s(?:\s+-\s+swb@|[/\\\s]|$)" % re.escape(basename),
                lowered):
            matches.append(window)
    return matches or ([item[0] for item in inventory] if allow_other else [])


def open_project_in_swb(project):
    """Ensure the published project is visible in a real SWB window."""
    project = project_path(project)
    matching = find_swb_windows(project)
    if matching:
        return {
            "ok": True, "action": "open-project", "project": project,
            "opened": True, "launched": False,
            "swbWindowsRefreshed": refresh_swb(project),
        }
    display = os.environ.get("DISPLAY")
    if not display:
        raise RuntimeError("DISPLAY is empty; cannot open the generated project in SWB")
    sentaurus_root = os.environ.get("AITCAD_RUN_STROOT", DEFAULT_SENTAURUS_ROOT)
    swb = os.path.join(sentaurus_root, "bin", "swb")
    if not os.path.isfile(swb):
        raise RuntimeError("swb not found: %s" % swb)
    log_root = os.path.expanduser("~/.local/state/aitcad/swb-ui")
    os.makedirs(log_root, mode=0o700, exist_ok=True)
    log_path = os.path.join(log_root, "open-%s-%s.log" % (
        re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(project)),
        dt.datetime.now().strftime("%Y%m%d-%H%M%S"),
    ))
    environment = os.environ.copy()
    environment["STROOT"] = sentaurus_root
    environment["STDB"] = os.environ.get("AITCAD_RUN_STDB") or os.path.dirname(os.path.dirname(project))
    environment["PATH"] = os.path.join(sentaurus_root, "bin") + os.pathsep + environment.get("PATH", "")
    license_file = os.environ.get("AITCAD_LICENSE_FILE", DEFAULT_LICENSE)
    if os.path.isfile(license_file):
        environment["LM_LICENSE_FILE"] = environment.get("LM_LICENSE_FILE", license_file)
        environment["SNPSLMD_LICENSE_FILE"] = environment.get("SNPSLMD_LICENSE_FILE", license_file)
    log = open(log_path, "ab", buffering=0)
    process = subprocess.Popen(
        [swb, "-b", project], cwd=project, env=environment,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        matching = find_swb_windows(project)
        if matching:
            return {
                "ok": True, "action": "open-project", "project": project,
                "opened": True, "launched": True, "pid": process.pid,
                "log": log_path, "windowCount": len(matching),
            }
        time.sleep(0.25)
    return {
        "ok": True, "action": "open-project", "project": project,
        "opened": False, "launched": True, "pid": process.pid,
        "log": log_path,
        "warning": "SWB was launched but its project window was not visible within 15 seconds",
    }


def send_f5(window):
    """Focus one X11 window and inject F5 using libraries already present on CentOS 7."""
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    x11.XStringToKeysym.restype = ctypes.c_ulong
    x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XKeysymToKeycode.restype = ctypes.c_ubyte
    x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Cannot connect to the X11 display")
    failed = [False]

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def ignore_bad_window(_display, _event):
        failed[0] = True
        return 0

    previous_handler = x11.XSetErrorHandler(ctypes.cast(ignore_bad_window, ctypes.c_void_p))
    try:
        keycode = x11.XKeysymToKeycode(display, x11.XStringToKeysym(b"F5"))
        x11.XRaiseWindow(display, ctypes.c_ulong(window))
        x11.XSetInputFocus(display, ctypes.c_ulong(window), 2, 0)
        x11.XFlush(display)
        time.sleep(0.08)
        xtst.XTestFakeKeyEvent(display, int(keycode), 1, 0)
        xtst.XTestFakeKeyEvent(display, int(keycode), 0, 0)
        x11.XFlush(display)
        x11.XSync(display, 0)
        if failed[0]:
            raise RuntimeError("SWB window disappeared before F5")
    finally:
        x11.XSetErrorHandler(previous_handler)
        x11.XCloseDisplay(display)


def click_swb_reload(window):
    """Click SWB 2018's own Reload toolbar button using window-relative coordinates."""
    x11 = ctypes.CDLL("libX11.so.6")
    xtst = ctypes.CDLL("libXtst.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XTranslateCoordinates.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
    ]
    x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
    x11.XSetErrorHandler.restype = ctypes.c_void_p
    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    if not display:
        raise RuntimeError("Cannot connect to the X11 display")
    failed = [False]

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    def ignore_bad_window(_display, _event):
        failed[0] = True
        return 0

    previous_handler = x11.XSetErrorHandler(ctypes.cast(ignore_bad_window, ctypes.c_void_p))
    try:
        root = x11.XDefaultRootWindow(display)
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        child = ctypes.c_ulong()
        translated = x11.XTranslateCoordinates(
            display, ctypes.c_ulong(window), root, 0, 0,
            ctypes.byref(root_x), ctypes.byref(root_y), ctypes.byref(child),
        )
        if not translated:
            raise RuntimeError("Cannot locate the SWB window")
        # In SWB O-2018.06 the Reload button is the third toolbar item.
        xtst.XTestFakeMotionEvent(display, -1, root_x.value + 102, root_y.value + 43, 0)
        x11.XRaiseWindow(display, ctypes.c_ulong(window))
        x11.XSetInputFocus(display, ctypes.c_ulong(window), 2, 0)
        x11.XFlush(display)
        time.sleep(0.08)
        xtst.XTestFakeButtonEvent(display, 1, 1, 0)
        xtst.XTestFakeButtonEvent(display, 1, 0, 0)
        x11.XFlush(display)
        x11.XSync(display, 0)
        if failed[0]:
            raise RuntimeError("SWB window disappeared before reload")
    finally:
        x11.XSetErrorHandler(previous_handler)
        x11.XCloseDisplay(display)


def refresh_swb(project):
    refreshed = 0
    attempted = set()
    # A Reload can rebuild the SWB top-level window. Rediscover it once rather
    # than reusing an XID that may already have become invalid.
    for _attempt in range(2):
        windows = [item for item in find_swb_windows(project) if item not in attempted]
        if not windows:
            break
        for window in windows:
            attempted.add(window)
            try:
                release = os.environ.get("AITCAD_SENTAURUS_RELEASE", "")
                # The extra Reload toolbar click is an O-2018.06 workaround;
                # later SWB releases may place a different command at that
                # coordinate.  F5 is the portable refresh path.
                if release.startswith("O-2018.06") or not release:
                    click_swb_reload(window)
                    time.sleep(0.15)
                # Find the window again after Reload before sending F5.
                current = find_swb_windows(project)
                target = current[0] if current else window
                send_f5(target)
                refreshed += 1
            except Exception:
                # UI refresh is best-effort. A completed parameter edit or
                # submitted simulation must never be reported as failed merely
                # because the user closed/reopened SWB during the operation.
                continue
        if refreshed:
            break
    return refreshed


def _refresh_swb_after_launch(project):
    """Refresh the visible SWB graph without delaying the launch response."""
    for delay in (0.08, 1.20):
        time.sleep(delay)
        try:
            refresh_swb(project)
        except Exception:
            pass


def queue_swb_refresh(project):
    worker = threading.Thread(target=_refresh_swb_after_launch, args=(project,))
    worker.daemon = False
    worker.start()
    return worker


def set_parameter(project, name, value):
    if not NAME_RE.match(name):
        raise RuntimeError("Invalid SWB parameter name")
    values = parse_values(value)
    output = run_gtcl(project, """
if {[lsearch -exact [::gtree::AllPnames] %s] < 0} {error {Parameter does not exist: %s}}
set previous [::gtree::Pvalues %s]
if {[llength $previous] != %d} {error {Replace requires the same number of values; use Add values to create experiments}}
::gtree::ChangeParamValues %s %s
::gtree::ResetNodeStates
::gtree::Save
puts "__AITCAD_PREVIOUS__\\t[join $previous ,]"
puts "__AITCAD_CURRENT__\\t[join [::gtree::Pvalues %s] ,]"
""" % (name, name, name, len(values), name, tcl_list(values), name))
    previous = []
    current = []
    for line in output.splitlines():
        if line.startswith("__AITCAD_PREVIOUS__\t"):
            previous = line.split("\t", 1)[1].split(",")
        elif line.startswith("__AITCAD_CURRENT__\t"):
            current = line.split("\t", 1)[1].split(",")
    if not previous:
        raise RuntimeError("Could not read the previous SWB parameter value")
    if current != values:
        raise RuntimeError("SWB parameter verification failed")
    refreshed = refresh_swb(project)
    return {"ok": True, "action": "set-parameter", "name": name, "previous": previous, "current": current, "swbWindowsRefreshed": refreshed}


def add_values(project, name, value):
    if not NAME_RE.match(name):
        raise RuntimeError("Invalid SWB parameter name")
    values = parse_values(value)
    output = run_gtcl(project, """
set pnames [::gtree::AllPnames]
set pindex [lsearch -exact $pnames %s]
if {$pindex < 0} {error {Parameter does not exist: %s}}
set before [::gtree::Pvalues %s]
set basepaths {}
foreach leaf [::gtree::AllLeafNodes] {
    set path [::gtree::NodePvalues $leaf]
    if {[llength $path] == [llength $pnames]} {lappend basepaths $path}
}
foreach path $basepaths {
    foreach newvalue %s {
        set candidate $path
        lset candidate $pindex $newvalue
        ::gtree::AddPath $candidate
    }
}
::gtree::Save
puts "__AITCAD_PREVIOUS__\\t[join $before ,]"
puts "__AITCAD_CURRENT__\\t[join [::gtree::Pvalues %s] ,]"
""" % (name, name, name, tcl_list(values), name))
    previous = []
    current = []
    for line in output.splitlines():
        if line.startswith("__AITCAD_PREVIOUS__\t"):
            previous = [item for item in line.split("\t", 1)[1].split(",") if item]
        elif line.startswith("__AITCAD_CURRENT__\t"):
            current = [item for item in line.split("\t", 1)[1].split(",") if item]
    added = [item for item in current if item not in previous]
    refreshed = refresh_swb(project)
    return {"ok": True, "action": "add-values", "name": name, "added": added, "current": current, "swbWindowsRefreshed": refreshed}


def add_parameter(project, name, value, step):
    if not NAME_RE.match(name):
        raise RuntimeError("Invalid SWB parameter name")
    values = parse_values(value)
    if step < 0:
        raise RuntimeError("Parameter step must be zero or greater")
    extras = values[1:]
    output = run_gtcl(project, """
if {[lsearch -exact [::gtree::AllPnames] %s] >= 0} {error {Parameter already exists: %s}}
::gtree::AddParam %s {%s} %d
set pnames [::gtree::AllPnames]
set pindex [lsearch -exact $pnames %s]
set basepaths {}
foreach leaf [::gtree::AllLeafNodes] {
    set path [::gtree::NodePvalues $leaf]
    if {[llength $path] == [llength $pnames]} {lappend basepaths $path}
}
foreach path $basepaths {
    foreach newvalue %s {
        set candidate $path
        lset candidate $pindex $newvalue
        ::gtree::AddPath $candidate
    }
}
::gtree::ResetNodeStates
::gtree::Save
puts "__AITCAD_CURRENT__\\t[join [::gtree::Pvalues %s] ,]"
""" % (name, name, name, values[0], step, name, tcl_list(extras), name))
    current = []
    for line in output.splitlines():
        if line.startswith("__AITCAD_CURRENT__\t"):
            current = [item for item in line.split("\t", 1)[1].split(",") if item]
    refreshed = refresh_swb(project)
    return {"ok": True, "action": "add-parameter", "name": name, "step": step, "current": current, "swbWindowsRefreshed": refreshed}


def _launch_node(project, node, run_id=None, task_id=None, started=None):
    output = run_gtcl(project, 'puts "__AITCAD_NODES__\\t[join [::gtree::AllNodes] ,]"')
    nodes = []
    for line in output.splitlines():
        if line.startswith("__AITCAD_NODES__\t"):
            nodes = [int(item) for item in line.split("\t", 1)[1].split(",") if item]
    if node not in nodes:
        raise RuntimeError("Node does not exist: %s" % node)
    run_gtcl(project, "::gtree::SetNodeState %d none\n::gtree::Save" % node)
    sentaurus_root = os.environ.get("AITCAD_RUN_STROOT", DEFAULT_SENTAURUS_ROOT)
    gsub = os.path.join(sentaurus_root, "bin", "gsub")
    if not os.path.isfile(gsub):
        raise RuntimeError("gsub not found: %s" % gsub)
    log_root = os.path.expanduser("~/.local/state/aitcad/live-runs")
    os.makedirs(log_root, mode=0o700, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(log_root, "%s-node-%s.log" % (stamp, node))
    environment = os.environ.copy()
    environment["STROOT"] = sentaurus_root
    environment["STDB"] = os.environ.get("AITCAD_RUN_STDB") or os.path.dirname(os.path.dirname(project))
    environment["PATH"] = os.path.join(sentaurus_root, "bin") + os.pathsep + environment.get("PATH", "")
    license_file = os.environ.get("AITCAD_LICENSE_FILE", DEFAULT_LICENSE)
    if os.path.isfile(license_file):
        environment["LM_LICENSE_FILE"] = environment.get("LM_LICENSE_FILE", license_file)
        environment["SNPSLMD_LICENSE_FILE"] = environment.get("SNPSLMD_LICENSE_FILE", license_file)
    log = open(log_path, "ab", buffering=0)
    process = subprocess.Popen(
        [gsub, "-nodes", str(node), project],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    result = {
        "ok": True, "action": "run-node", "node": node, "pid": process.pid,
        "pgid": process.pid, "runId": run_id, "log": log_path,
        "swbWillTrack": True, "swbRefreshQueued": True,
    }
    if run_id:
        result["registryPath"] = _save_run_state({
            "runId": run_id, "taskId": str(task_id or ""), "project": project,
            "node": node, "pid": process.pid, "pgid": process.pid,
            "log": log_path, "status": "running",
            "startedAt": dt.datetime.utcnow().isoformat() + "Z",
        })
    if started is not None:
        started(dict(result))
    queue_swb_refresh(project)
    return process, result


def run_node(project, node, run_id=None, task_id=None):
    _process, result = _launch_node(project, node, run_id, task_id)
    return result


def _launch_scenario(project, scenario="default", run_id=None, task_id=None, started=None):
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", str(scenario or "")):
        raise RuntimeError("Invalid SWB scenario")
    run_gtcl(project, "::gtree::ResetNodeStates\n::gtree::Save")
    sentaurus_root = os.environ.get("AITCAD_RUN_STROOT", DEFAULT_SENTAURUS_ROOT)
    gsub = os.path.join(sentaurus_root, "bin", "gsub")
    if not os.path.isfile(gsub):
        raise RuntimeError("gsub not found: %s" % gsub)
    log_root = os.path.expanduser("~/.local/state/aitcad/live-runs")
    os.makedirs(log_root, mode=0o700, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(log_root, "%s-scenario-%s.log" % (stamp, scenario))
    environment = os.environ.copy()
    environment["STROOT"] = sentaurus_root
    environment["STDB"] = os.environ.get("AITCAD_RUN_STDB") or os.path.dirname(os.path.dirname(project))
    environment["PATH"] = os.path.join(sentaurus_root, "bin") + os.pathsep + environment.get("PATH", "")
    license_file = os.environ.get("AITCAD_LICENSE_FILE", DEFAULT_LICENSE)
    if os.path.isfile(license_file):
        environment["LM_LICENSE_FILE"] = environment.get("LM_LICENSE_FILE", license_file)
        environment["SNPSLMD_LICENSE_FILE"] = environment.get("SNPSLMD_LICENSE_FILE", license_file)
    log = open(log_path, "ab", buffering=0)
    process = subprocess.Popen(
        [gsub, "-nodes", scenario, project], cwd=project, env=environment,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    result = {
        "ok": True, "action": "run-scenario", "scenario": scenario,
        "pid": process.pid, "pgid": process.pid, "runId": run_id,
        "log": log_path, "swbWillTrack": True, "swbRefreshQueued": True,
    }
    if run_id:
        result["registryPath"] = _save_run_state({
            "runId": run_id, "taskId": str(task_id or ""), "project": project,
            "scenario": scenario, "pid": process.pid, "pgid": process.pid,
            "log": log_path, "status": "running",
            "startedAt": dt.datetime.utcnow().isoformat() + "Z",
        })
    if started is not None:
        started(dict(result))
    queue_swb_refresh(project)
    return process, result


def _tail_text(path, max_bytes=32768):
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes), os.SEEK_SET)
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def collect_node_diagnostics(project, node, outer_log=None):
    """Return the Tool error, not only gsub's outer scheduling report."""
    project = project_path(project)
    node = int(node)
    patterns = (
        "n%d_*.err" % node, "n%d_*.out" % node, "n%d_*.log" % node,
        "pp%d_*.cmd" % node, "n%d_*.sta" % node,
    )
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(os.path.join(project, pattern)))
    paths = sorted(set(paths), key=lambda value: (0 if value.endswith(".err") else 1, value))
    files = []
    error_text = ""
    for path in paths[:16]:
        content = _tail_text(path)
        if not content:
            continue
        files.append({"path": path, "name": os.path.basename(path), "tail": content[-12000:]})
        if not error_text and (path.endswith(".err") or re.search(r"(?i)\b(?:error|syntax error|fatal)\b", content)):
            error_text = content
    if not error_text and outer_log:
        error_text = _tail_text(outer_log)
    summary = "Tool 节点 %d 未完成；请展开技术细节查看真实错误文件。" % node
    location = None
    offending = None
    match = re.search(
        r'Error in\s+"([^"]+)"\s+at line\s+(\d+),\s*offending input:\s*([^\r\n]*)',
        error_text, re.I,
    )
    if match:
        location = {"file": match.group(1), "line": int(match.group(2))}
        offending = match.group(3).strip()
        summary = "%s 第 %s 行语法错误%s" % (
            match.group(1), match.group(2),
            "（出错内容：%s）" % offending if offending else "",
        )
    else:
        lines = [line.strip() for line in error_text.splitlines() if re.search(
            r"(?i)\b(?:error|syntax error|fatal|failed|cannot|could not|not found)\b", line
        )]
        if lines:
            summary = lines[0][:500]
    return {
        "node": node, "summary": summary, "location": location,
        "offendingInput": offending, "files": files,
        "outerLog": outer_log,
    }


def _wait_gsub(project, process, result, timeout=1800, progress=None, run_id=None,
               diagnostic_node=None, raise_on_failure=True):
    launched_at = time.time()
    next_progress = launched_at + 10
    while True:
        returncode = process.poll()
        if returncode is not None:
            break
        now = time.time()
        if now - launched_at >= timeout:
            raise RuntimeError("Timed out waiting for SWB execution after %s seconds; the job may still be running" % timeout)
        if progress is not None and now >= next_progress:
            progress(int(now - launched_at))
            next_progress = now + 10
        time.sleep(1)
    result["returncode"] = returncode
    result["finished"] = True
    if run_id:
        try:
            state = _load_run_state(run_id)
            state.update({
                "status": "completed" if returncode == 0 else "failed",
                "returncode": returncode,
                "finishedAt": dt.datetime.utcnow().isoformat() + "Z",
            })
            _save_run_state(state)
        except RuntimeError:
            pass
    queue_swb_refresh(project)
    result["swbRefreshQueuedAfterRun"] = True
    try:
        with open(result["log"], "r", errors="replace") as stream:
            result["logTail"] = "\n".join(stream.read().splitlines()[-20:])
    except OSError:
        result["logTail"] = ""
    failed_in_log = "failed" in result["logTail"].lower() and "0 failed" not in result["logTail"].lower()
    if (returncode or failed_in_log) and diagnostic_node is not None:
        result["diagnostic"] = collect_node_diagnostics(project, diagnostic_node, result.get("log"))
    if returncode and raise_on_failure:
        raise RuntimeError("SWB execution failed (exit %s): %s" % (returncode, result["logTail"][-1200:]))
    if failed_in_log and raise_on_failure:
        raise RuntimeError("SWB execution reported failure: %s" % result["logTail"][-1200:])
    return result


def run_node_wait(project, node, timeout=1800, progress=None, started=None, run_id=None, task_id=None,
                  raise_on_failure=True):
    process, result = _launch_node(project, node, run_id, task_id, started=started)
    return _wait_gsub(project, process, result, timeout, progress, run_id, node, raise_on_failure)


def run_scenario_wait(project, scenario="default", timeout=1800, progress=None, started=None,
                      run_id=None, task_id=None, raise_on_failure=True):
    process, result = _launch_scenario(project, scenario, run_id, task_id, started=started)
    return _wait_gsub(project, process, result, timeout, progress, run_id, None, raise_on_failure)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("set-parameter", "add-values", "add-parameter", "run-node", "stop-run", "refresh-swb", "open-project"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--name")
    parser.add_argument("--value")
    parser.add_argument("--node", type=int)
    parser.add_argument("--step", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    project = project_path(args.project)
    if args.action == "set-parameter":
        if not args.name or args.value is None:
            raise RuntimeError("set-parameter requires --name and --value")
        result = set_parameter(project, args.name, args.value)
    elif args.action == "add-values":
        if not args.name or args.value is None:
            raise RuntimeError("add-values requires --name and --value")
        result = add_values(project, args.name, args.value)
    elif args.action == "add-parameter":
        if not args.name or args.value is None or args.step is None:
            raise RuntimeError("add-parameter requires --name, --value and --step")
        result = add_parameter(project, args.name, args.value, args.step)
    elif args.action == "run-node":
        if args.node is None:
            raise RuntimeError("run-node requires --node")
        result = run_node(project, args.node, args.run_id, args.task_id)
    elif args.action == "stop-run":
        if not args.run_id:
            raise RuntimeError("stop-run requires --run-id")
        result = stop_run(project, args.run_id)
    elif args.action == "open-project":
        result = open_project_in_swb(project)
    else:
        result = {"ok": True, "action": "refresh-swb", "swbWindowsRefreshed": refresh_swb(project)}
    emit(result)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"ok": False, "error": str(error)})
        sys.exit(1)
