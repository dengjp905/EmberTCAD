#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function

import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time


APP_VERSION = "0.1.0"
CONNECTOR = os.environ.get(
    "AITCAD_CONNECTOR",
    os.path.expanduser("~/.local/share/aitcad/connector.py"),
)
DEFAULT_PROJECT = os.environ.get("AITCAD_PROJECT", "nmos_study/nmos_project")
RUNTIME_CONFIG_PATH = os.path.expanduser("~/.config/aitcad/runtime.json")


def _sentaurus_release(root):
    current = os.path.join(root, "tcad", "current")
    if os.path.exists(current):
        return os.path.basename(os.path.realpath(current))
    return os.path.basename(root.rstrip(os.sep)).replace("_", "-", 1)


def _valid_sentaurus_root(root):
    root = os.path.realpath(os.path.expanduser(root or ""))
    required = ("gtclsh", "swb", "gsub")
    return bool(root and os.path.isdir(root) and all(
        os.path.isfile(os.path.join(root, "bin", command)) and
        os.access(os.path.join(root, "bin", command), os.X_OK)
        for command in required
    ))


def _saved_sentaurus_root():
    try:
        with open(RUNTIME_CONFIG_PATH, "r") as stream:
            value = json.load(stream).get("sentaurusRoot")
        if _valid_sentaurus_root(value):
            return os.path.realpath(os.path.expanduser(value))
    except (IOError, OSError, ValueError, TypeError):
        pass
    return None


SENTAURUS_2018_ROOT = (_saved_sentaurus_root() or
                       os.environ.get("AITCAD_RUN_STROOT") or
                       "/usr/synopsys/sentaurus/O_2018.06-SP2")
GTCLSH = os.path.join(SENTAURUS_2018_ROOT, "bin", "gtclsh")
os.environ["AITCAD_RUN_STROOT"] = SENTAURUS_2018_ROOT
os.environ["AITCAD_SWB_API_STROOT"] = SENTAURUS_2018_ROOT
os.environ["AITCAD_SENTAURUS_RELEASE"] = _sentaurus_release(SENTAURUS_2018_ROOT)


def _command_path(name):
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _helper_python():
    explicit = os.environ.get("AITCAD_HELPER_PYTHON") or os.environ.get("AITCAD_SWB_API_PYTHON")
    candidates = [explicit, _command_path("python3"),
                  "/usr/synopsys/sentaurus/X-2025.06/tcad/X-2025.06/linux64/bin/python3.11"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("EmberTCAD needs Python 3 for its model, task, and SWB helpers")


SWB_API_PYTHON = _helper_python()
SWB_API_STROOT = os.environ.get("AITCAD_SWB_API_STROOT", SENTAURUS_2018_ROOT)
LIVE_HELPER = os.path.join(os.path.dirname(os.path.realpath(__file__)), "swb_live.py")
AI_HELPER = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ai_agent.py")
RESEARCH_HELPER = os.path.join(os.path.dirname(os.path.realpath(__file__)), "research_service.py")
TEXT_EXTENSIONS = set((
    "cmd", "par", "tcl", "txt", "log", "out", "err", "dat", "csv", "json", "xml", "py", "sh"
))


def discover_sentaurus_installations():
    """Return runnable local releases without assuming one fixed install path."""
    candidates = []
    for value in (os.environ.get("AITCAD_RUN_STROOT"), os.environ.get("STROOT"),
                  SENTAURUS_2018_ROOT, "/usr/synopsys/sentaurus/current"):
        if value:
            candidates.append(value)
    for pattern in ("/usr/synopsys/sentaurus/*", "/opt/synopsys/sentaurus/*",
                    "/opt/synopsys/*/sentaurus/*"):
        candidates.extend(glob.glob(pattern))
    seen = set()
    rows = []
    for candidate in candidates:
        root = os.path.realpath(os.path.expanduser(candidate))
        if root in seen or not _valid_sentaurus_root(root):
            continue
        seen.add(root)
        commands = {}
        for command in ("swb", "gtclsh", "gsub", "sde", "sdevice", "snmesh"):
            path = os.path.join(root, "bin", command)
            commands[command] = path if os.path.isfile(path) and os.access(path, os.X_OK) else None
        rows.append({
            "root": root,
            "release": _sentaurus_release(root),
            "commands": commands,
            "selected": root == os.path.realpath(SENTAURUS_2018_ROOT),
        })
    rows.sort(key=lambda item: item.get("release") or "")
    return rows


def sentaurus_runtime_status():
    return {
        "ok": True,
        "selectedRoot": os.path.realpath(SENTAURUS_2018_ROOT),
        "selectedRelease": _sentaurus_release(SENTAURUS_2018_ROOT),
        "installations": discover_sentaurus_installations(),
        "configPath": RUNTIME_CONFIG_PATH,
    }


def select_sentaurus_root(root):
    """Persist and activate a detected release for all subsequent helpers."""
    global SENTAURUS_2018_ROOT, GTCLSH, SWB_API_STROOT
    requested = os.path.realpath(os.path.expanduser(root or ""))
    detected = dict((item["root"], item) for item in discover_sentaurus_installations())
    if requested not in detected:
        raise RuntimeError("所选目录不是本机可运行的 Sentaurus 安装：%s" % requested)
    folder = os.path.dirname(RUNTIME_CONFIG_PATH)
    if not os.path.isdir(folder):
        os.makedirs(folder, 0o700)
    temporary = RUNTIME_CONFIG_PATH + ".tmp"
    with open(temporary, "w") as stream:
        json.dump({"sentaurusRoot": requested,
                   "sentaurusRelease": detected[requested]["release"]}, stream, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    if hasattr(os, "replace"):
        os.replace(temporary, RUNTIME_CONFIG_PATH)
    else:
        if os.path.exists(RUNTIME_CONFIG_PATH):
            os.unlink(RUNTIME_CONFIG_PATH)
        os.rename(temporary, RUNTIME_CONFIG_PATH)
    SENTAURUS_2018_ROOT = requested
    SWB_API_STROOT = requested
    GTCLSH = os.path.join(requested, "bin", "gtclsh")
    os.environ["AITCAD_RUN_STROOT"] = requested
    os.environ["AITCAD_SWB_API_STROOT"] = requested
    os.environ["AITCAD_SENTAURUS_RELEASE"] = detected[requested]["release"]
    year = re.search(r"(?:19|20)\d{2}", detected[requested]["release"])
    os.environ["AITCAD_SENTAURUS_VERSION"] = year.group(0) if year else "configured"
    return sentaurus_runtime_status()


def rpc(method, params=None):
    request = json.dumps({"method": method, "params": params or {}}) + "\n"
    process = subprocess.Popen(
        [sys.executable, CONNECTOR, "rpc"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    stdout, stderr = process.communicate(request.encode("utf-8"))
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "Connector returned no data")
    result = json.loads(text.splitlines()[-1])
    if not result.get("ok"):
        error = result.get("error") or {}
        raise RuntimeError(error.get("message") or "Connector request failed")
    return result


def read_live_swb_state(project):
    if not os.path.isfile(os.path.join(project, "gtree.dat")):
        return {"parameters": [], "nodes": []}
    if not os.path.isfile(GTCLSH):
        raise RuntimeError("gtclsh not found: %s" % GTCLSH)
    safe_project = project.replace("\\", "/").replace("}", "\\}")
    script = """::gtree::New aitcad
::gtree::Load {%s/gtree.dat}
foreach p [::gtree::AllPnames] {
    puts "__AITCAD_PARAM__\\t$p\\t[::gtree::PdefaultValue $p]\\t[join [::gtree::Pvalues $p] ,]\\t[::gtree::PnameStep $p]"
}
foreach n [::gtree::AllNodes] {
    puts "__AITCAD_NODE__\\t$n\\t[::gtree::NodeTool $n]\\t[join [::gtree::NodePvalues $n] ,]\\t[::gtree::NodeState $n]"
}
""" % safe_project
    environment = os.environ.copy()
    environment["STROOT"] = SENTAURUS_2018_ROOT
    environment["STDB"] = os.path.dirname(os.path.dirname(project))
    environment["PATH"] = os.path.join(SENTAURUS_2018_ROOT, "bin") + os.pathsep + environment.get("PATH", "")
    process = subprocess.Popen([GTCLSH], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    stdout, stderr = process.communicate(script.encode("utf-8"))
    if process.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "gtclsh failed")
    parameters = []
    nodes = []
    for raw in stdout.decode("utf-8", "replace").splitlines():
        parts = raw.split("\t")
        if parts and parts[0] == "__AITCAD_PARAM__" and len(parts) >= 5:
            parameters.append({"name": parts[1], "default": parts[2], "values": parts[3], "step": parts[4]})
        elif parts and parts[0] == "__AITCAD_NODE__" and len(parts) >= 5:
            node = parts[1]
            status = parts[4] or "none"
            status_files = glob.glob(os.path.join(project, "n%s_*.sta" % node))
            if status_files:
                newest = max(status_files, key=os.path.getmtime)
                try:
                    with open(newest, "rb") as stream:
                        line = stream.readline().decode("utf-8", "replace").strip()
                    fields = line.split("|")
                    if len(fields) >= 4 and fields[2]:
                        status = fields[2]
                except Exception:
                    pass
            nodes.append({"node": node, "tool": parts[2], "values": parts[3], "status": status})
    return {"parameters": parameters, "nodes": nodes}


def live_action(action, project, **kwargs):
    if not os.path.isfile(SWB_API_PYTHON):
        raise RuntimeError("Official SWB Python API runtime was not found")
    command = [SWB_API_PYTHON, LIVE_HELPER, action, "--project", project]
    if action in ("set-parameter", "add-values", "add-parameter"):
        command.extend(["--name", kwargs["name"], "--value", kwargs["value"]])
        if action == "add-parameter":
            command.extend(["--step", str(kwargs["step"])])
    elif action == "run-node":
        command.extend(["--node", str(kwargs["node"])])
        if kwargs.get("run_id"):
            command.extend(["--run-id", str(kwargs["run_id"])])
        if kwargs.get("task_id"):
            command.extend(["--task-id", str(kwargs["task_id"])])
    elif action == "stop-run":
        command.extend(["--run-id", str(kwargs["run_id"])])
    environment = os.environ.copy()
    environment["STROOT"] = SWB_API_STROOT
    environment["PATH"] = os.path.join(SWB_API_STROOT, "bin") + os.pathsep + environment.get("PATH", "")
    environment["AITCAD_RUN_STROOT"] = SENTAURUS_2018_ROOT
    environment["AITCAD_RUN_STDB"] = os.path.dirname(os.path.dirname(project))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    stdout, stderr = process.communicate()
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "SWB live action returned no data")
    result = json.loads(text.splitlines()[-1])
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "SWB live action failed")
    return result


def ai_rpc(request, event_callback=None, process_callback=None):
    if not os.path.isfile(AI_HELPER):
        raise RuntimeError("AI helper was not found")
    process = subprocess.Popen(
        [SWB_API_PYTHON, AI_HELPER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    if process_callback:
        process_callback(process)
    process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
    process.stdin.close()
    result = None
    for raw in iter(process.stdout.readline, b""):
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except ValueError:
            continue
        if item.get("event"):
            if event_callback:
                event_callback(item)
        else:
            result = item
    stderr = process.stderr.read()
    process.wait()
    if process_callback:
        process_callback(None)
    if result is None:
        raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "AI helper returned no final result")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "AI request failed")
    return result


def research_rpc(request):
    """Call the Python 3 evidence/index/task service and return one JSON result."""
    if not os.path.isfile(RESEARCH_HELPER):
        raise RuntimeError("Research helper was not found")
    process = subprocess.Popen(
        [SWB_API_PYTHON, RESEARCH_HELPER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )
    stdout, stderr = process.communicate((json.dumps(request) + "\n").encode("utf-8"))
    text = stdout.decode("utf-8", "replace").strip()
    if not text:
        raise RuntimeError(stderr.decode("utf-8", "replace").strip() or "Research helper returned no data")
    try:
        result = json.loads(text.splitlines()[-1])
    except ValueError:
        raise RuntimeError("Research helper returned invalid JSON")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "Research request failed")
    return result


def probe():
    status = rpc("system.probe")
    projects = status.get("projects") or []
    project = DEFAULT_PROJECT if any(
        item.get("relativePath") == DEFAULT_PROJECT for item in projects
    ) else (projects[0].get("relativePath") if projects else "")
    tree = rpc("project.tree", {"relativePath": project, "maxDepth": 2}) if project else {"entries": []}
    print(json.dumps({
        "ok": True,
        "version": APP_VERSION,
        "workspace": status.get("workspace", {}).get("root"),
        "projects": [item.get("relativePath") for item in projects],
        "activeProject": project,
        "entries": len(tree.get("entries") or []),
    }, ensure_ascii=True))


def acquire_single_instance():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ("/tmp/aitcad-%d" % os.getuid())
    if not os.path.isdir(runtime):
        os.makedirs(runtime, 0o700)
    lock_path = os.path.join(runtime, "native-assistant.lock")
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def run_gui():
    import pygtk
    pygtk.require("2.0")
    import gtk
    import gobject
    import pango

    gobject.threads_init()

    gtk.rc_parse_string("""
      style "aitcad-default" {
        font_name = "Sans 10"
        bg[NORMAL] = "#f4f7fb"
        bg[ACTIVE] = "#e7edf7"
        bg[PRELIGHT] = "#edf3ff"
        fg[NORMAL] = "#172033"
        fg[ACTIVE] = "#172033"
        fg[PRELIGHT] = "#172033"
        text[NORMAL] = "#172033"
        base[NORMAL] = "#ffffff"
        base[ACTIVE] = "#eef4ff"
        base[SELECTED] = "#2f6fed"
        text[SELECTED] = "#ffffff"
        GtkWidget::focus-padding = 0
        GtkButton::default-border = { 0, 0, 0, 0 }
        GtkButton::inner-border = { 10, 10, 6, 6 }
        GtkTreeView::vertical-separator = 5
      }
      style "aitcad-header" = "aitcad-default" {
        bg[NORMAL] = "#102044"
        fg[NORMAL] = "#ffffff"
      }
      style "aitcad-accent" = "aitcad-default" {
        bg[NORMAL] = "#2f6fed"
        fg[NORMAL] = "#ffffff"
        bg[PRELIGHT] = "#4b82ef"
        fg[PRELIGHT] = "#ffffff"
      }
      style "aitcad-task-card" = "aitcad-default" {
        bg[NORMAL] = "#101d3d"
        fg[NORMAL] = "#ffffff"
      }
      style "aitcad-progress" = "aitcad-default" {
        bg[NORMAL] = "#24365f"
        bg[PRELIGHT] = "#38d6b3"
        fg[NORMAL] = "#ffffff"
        GtkProgressBar::min-horizontal-bar-height = 10
      }
      widget "*" style "aitcad-default"
      widget "*.Header" style "aitcad-header"
      widget "*.Accent" style "aitcad-accent"
      widget "*.TaskCard" style "aitcad-task-card"
      widget "*.TaskProgress" style "aitcad-progress"
    """)

    class AssistantWindow(gtk.Window):
        def __init__(self):
            gtk.Window.__init__(self, gtk.WINDOW_TOPLEVEL)
            self.set_title("EmberTCAD")
            self.set_role("aitcad-assistant")
            self.set_keep_above(True)
            self.set_skip_taskbar_hint(False)
            self.set_border_width(0)
            self.connect("destroy", gtk.main_quit)
            self.connect("realize", self.on_realize)
            self.status = None
            self.projects = []
            self.active_project = DEFAULT_PROJECT
            self.workspace_root = ""
            self.refreshing = False
            self.project_paths = []
            self.last_signature = None
            self.selected_parameter = None
            self.selected_node = None
            self.selected_node_tool = ""
            self.ai_history = []
            self.ai_busy = False
            self.iteration_rows = {}
            self.current_iteration = None
            self.task_target = None
            self.task_tolerance = None
            self.build_ui()
            self.position_at_right_edge()
            gobject.timeout_add(1000, self.refresh)
            self.refresh()
            threading.Thread(target=self.load_ai_status_worker).start()

        def position_at_right_edge(self):
            screen = self.get_screen()
            monitor = screen.get_monitor_at_point(max(0, screen.get_width() - 10), 50)
            area = screen.get_monitor_geometry(monitor)
            width = min(610, max(520, int(area.width * 0.32)))
            height = max(620, area.height - 64)
            self.set_default_size(width, height)
            self.move(area.x + area.width - width - 8, area.y + 36)

        def on_realize(self, *_args):
            self.position_at_right_edge()

        def label(self, text, markup=False, wrap=False):
            label = gtk.Label()
            label.set_alignment(0.0, 0.5)
            label.set_line_wrap(wrap)
            if markup:
                label.set_markup(text)
            else:
                label.set_text(text)
            return label

        def build_ui(self):
            root = gtk.VBox(False, 0)
            self.add(root)

            header = gtk.EventBox()
            header.set_name("Header")
            header_box = gtk.HBox(False, 12)
            header_box.set_border_width(16)
            mark = self.label("<span size='x-large' foreground='#65d7ff'><b>✦</b></span>", True)
            titles = gtk.VBox(False, 2)
            titles.pack_start(self.label("<span size='large' foreground='#ffffff'><b>EmberTCAD</b></span>", True), False, False, 0)
            titles.pack_start(self.label("<span size='small' foreground='#b9c9ee'>LOCAL · LIVE SWB</span>", True), False, False, 0)
            header_box.pack_start(mark, False, False, 0)
            header_box.pack_start(titles, True, True, 0)
            header.add(header_box)
            root.pack_start(header, False, False, 0)

            body = gtk.VBox(False, 12)
            body.set_border_width(14)
            root.pack_start(body, True, True, 0)

            project_box = gtk.VBox(False, 7)
            project_box.pack_start(self.label("<span size='small' foreground='#52627a'><b>当前 SWB 工程</b></span>", True), False, False, 0)
            project_row = gtk.HBox(False, 7)
            self.project_combo = gtk.combo_box_new_text()
            self.project_combo.connect("changed", self.on_project_changed)
            refresh_button = gtk.Button("双向刷新")
            refresh_button.set_name("Accent")
            refresh_button.connect("clicked", self.on_refresh_all)
            project_row.pack_start(self.project_combo, True, True, 0)
            project_row.pack_end(refresh_button, False, False, 0)
            project_box.pack_start(project_row, False, False, 0)
            body.pack_start(project_box, False, False, 0)

            status_frame = gtk.Frame()
            status_frame.set_shadow_type(gtk.SHADOW_ETCHED_IN)
            status_box = gtk.VBox(False, 5)
            status_box.set_border_width(11)
            self.system_label = self.label("Connector：检测中")
            self.workspace_label = self.label("工作区：—")
            self.change_label = self.label("最近同步：—")
            status_box.pack_start(self.system_label, False, False, 0)
            status_box.pack_start(self.workspace_label, False, False, 0)
            status_box.pack_start(self.change_label, False, False, 0)
            status_frame.add(status_box)
            body.pack_start(status_frame, False, False, 0)

            notebook = gtk.Notebook()
            self.notebook = notebook
            body.pack_start(notebook, True, True, 0)

            files_box = gtk.VBox(False, 7)
            self.file_store = gtk.ListStore(str, str, str, str)
            self.file_view = gtk.TreeView(self.file_store)
            self.file_view.set_headers_visible(False)
            self.file_view.connect("row-activated", self.on_file_activated)
            icon_renderer = gtk.CellRendererText()
            icon_renderer.set_property("foreground", "#2f6fed")
            icon_renderer.set_property("weight", pango.WEIGHT_BOLD)
            name_renderer = gtk.CellRendererText()
            name_renderer.set_property("ellipsize", pango.ELLIPSIZE_END)
            time_renderer = gtk.CellRendererText()
            time_renderer.set_property("foreground", "#718096")
            type_column = gtk.TreeViewColumn("", icon_renderer, text=0)
            type_column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
            type_column.set_fixed_width(55)
            name_column = gtk.TreeViewColumn("", name_renderer, text=1)
            name_column.set_expand(True)
            time_column = gtk.TreeViewColumn("", time_renderer, text=2)
            time_column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
            time_column.set_fixed_width(72)
            self.file_view.append_column(type_column)
            self.file_view.append_column(name_column)
            self.file_view.append_column(time_column)
            scroll = gtk.ScrolledWindow()
            scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            scroll.add(self.file_view)
            files_box.pack_start(scroll, True, True, 0)
            notebook.append_page(files_box, self.label("工程文件"))

            params_box = gtk.VBox(False, 8)
            params_box.set_border_width(8)
            self.param_store = gtk.ListStore(str, str, str, str)
            self.param_view = gtk.TreeView(self.param_store)
            self.param_view.set_headers_visible(True)
            self.param_view.get_selection().connect("changed", self.on_parameter_selected)
            for title, index, width in (("参数", 0, 120), ("当前值", 2, 120), ("步骤", 3, 48)):
                renderer = gtk.CellRendererText()
                if index == 0:
                    renderer.set_property("foreground", "#185bd6")
                    renderer.set_property("weight", pango.WEIGHT_BOLD)
                column = gtk.TreeViewColumn(title, renderer, text=index)
                column.set_min_width(width)
                self.param_view.append_column(column)
            params_scroll = gtk.ScrolledWindow()
            params_scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            params_scroll.add(self.param_view)
            params_box.pack_start(params_scroll, True, True, 0)
            edit_row = gtk.HBox(False, 7)
            edit_row.pack_start(self.label("名称"), False, False, 0)
            self.param_name_entry = gtk.Entry()
            self.param_name_entry.set_width_chars(15)
            self.param_name_entry.set_text("con_pwell")
            self.param_step_entry = gtk.Entry()
            self.param_step_entry.set_width_chars(4)
            self.param_step_entry.set_text("2")
            edit_row.pack_start(self.param_name_entry, True, True, 0)
            edit_row.pack_start(self.label("步骤"), False, False, 0)
            edit_row.pack_end(self.param_step_entry, False, False, 0)
            values_row = gtk.HBox(False, 7)
            values_row.pack_start(self.label("取值"), False, False, 0)
            self.param_value_entry = gtk.Entry()
            self.param_value_entry.set_text("1e18")
            self.param_value_entry.connect("activate", lambda *_: self.on_parameter_action("set-parameter"))
            values_row.pack_start(self.param_value_entry, True, True, 0)
            values_row.pack_end(self.label("逗号分隔"), False, False, 0)
            action_row = gtk.HBox(False, 7)
            replace_button = gtk.Button("替换现有值")
            replace_button.connect("clicked", lambda *_: self.on_parameter_action("set-parameter"))
            add_values_button = gtk.Button("添加取值")
            add_values_button.set_name("Accent")
            add_values_button.connect("clicked", lambda *_: self.on_parameter_action("add-values"))
            add_param_button = gtk.Button("新增参数")
            add_param_button.connect("clicked", lambda *_: self.on_parameter_action("add-parameter"))
            action_row.pack_start(replace_button, True, True, 0)
            action_row.pack_start(add_values_button, True, True, 0)
            action_row.pack_start(add_param_button, True, True, 0)
            params_box.pack_start(edit_row, False, False, 0)
            params_box.pack_start(values_row, False, False, 0)
            params_box.pack_start(action_row, False, False, 0)
            notebook.append_page(params_box, self.label("参数"))

            nodes_box = gtk.VBox(False, 8)
            nodes_box.set_border_width(8)
            self.node_store = gtk.ListStore(str, str, str, str)
            self.node_view = gtk.TreeView(self.node_store)
            self.node_view.set_headers_visible(True)
            self.node_view.get_selection().connect("changed", self.on_node_selected)
            self.node_view.connect("row-activated", lambda *_: self.on_run_node())
            for title, index, width in (("节点", 0, 48), ("工具", 1, 105), ("状态", 2, 70), ("参数值", 3, 120)):
                renderer = gtk.CellRendererText()
                if index == 2:
                    renderer.set_property("foreground", "#15805d")
                column = gtk.TreeViewColumn(title, renderer, text=index)
                column.set_min_width(width)
                self.node_view.append_column(column)
            nodes_scroll = gtk.ScrolledWindow()
            nodes_scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            nodes_scroll.add(self.node_view)
            run_row = gtk.HBox(False, 7)
            self.selected_node_label = self.label("已选节点：尚未选择")
            self.run_node_entry = gtk.Entry()
            self.run_node_entry.set_width_chars(5)
            self.run_node_entry.set_tooltip_text("也可以直接输入节点号")
            self.run_node_entry.connect("activate", self.on_run_node)
            run_button = gtk.Button("运行节点")
            run_button.set_name("Accent")
            run_button.connect("clicked", self.on_run_node)
            run_row.pack_start(self.selected_node_label, True, True, 0)
            run_row.pack_start(self.label("节点号"), False, False, 0)
            run_row.pack_start(self.run_node_entry, False, False, 0)
            run_row.pack_end(run_button, False, False, 0)
            nodes_box.pack_start(nodes_scroll, True, True, 0)
            nodes_box.pack_end(run_row, False, False, 0)
            notebook.append_page(nodes_box, self.label("节点"))

            assistant_box = gtk.VBox(False, 9)
            assistant_box.set_border_width(10)

            task_card = gtk.EventBox()
            task_card.set_name("TaskCard")
            task_box = gtk.VBox(False, 7)
            task_box.set_border_width(14)
            task_heading = gtk.HBox(False, 8)
            self.task_title_label = self.label("<span foreground='#70e1ff' size='small'><b>AI · TCAD AUTOPILOT</b></span>", True)
            self.task_state_label = self.label("<span foreground='#9badcf'><b>待命</b></span>", True)
            task_heading.pack_start(self.task_title_label, True, True, 0)
            task_heading.pack_end(self.task_state_label, False, False, 0)
            self.task_detail_label = self.label("等待新的仿真目标", False, True)
            self.task_detail_label.modify_fg(gtk.STATE_NORMAL, gtk.gdk.color_parse("#ffffff"))
            self.reasoning_label = self.label("深度推理开启后，模型会先给出实验策略，再交给确定性控制器执行。", False, True)
            self.reasoning_label.modify_fg(gtk.STATE_NORMAL, gtk.gdk.color_parse("#a9badb"))
            self.task_progress = gtk.ProgressBar()
            self.task_progress.set_name("TaskProgress")
            self.task_progress.set_fraction(0.0)
            self.task_progress.set_text("READY")
            task_box.pack_start(task_heading, False, False, 0)
            task_box.pack_start(self.task_detail_label, False, False, 0)
            task_box.pack_start(self.reasoning_label, False, False, 0)
            task_box.pack_start(self.task_progress, False, False, 0)
            task_card.add(task_box)
            assistant_box.pack_start(task_card, False, False, 0)

            iterations_heading = gtk.HBox(False, 6)
            iterations_heading.pack_start(self.label("<span foreground='#172033'><b>实验迭代</b></span>", True), True, True, 0)
            self.iteration_summary_label = self.label("0 次")
            iterations_heading.pack_end(self.iteration_summary_label, False, False, 0)
            assistant_box.pack_start(iterations_heading, False, False, 0)

            self.iteration_store = gtk.ListStore(str, str, str, str, str, str)
            self.iteration_view = gtk.TreeView(self.iteration_store)
            self.iteration_view.set_headers_visible(True)
            self.iteration_view.set_rules_hint(True)
            iteration_columns = (
                ("#", 0, 32), ("P-well / cm⁻³", 1, 112), ("SDE", 2, 58),
                ("SDevice", 3, 72), ("Vth / V", 4, 78), ("状态", 5, 62),
            )
            for title, index, width in iteration_columns:
                renderer = gtk.CellRendererText()
                renderer.set_property("xpad", 5)
                if index == 4:
                    renderer.set_property("foreground", "#185bd6")
                    renderer.set_property("weight", pango.WEIGHT_BOLD)
                elif index == 5:
                    renderer.set_property("foreground", "#15805d")
                column = gtk.TreeViewColumn(title, renderer, text=index)
                column.set_sizing(gtk.TREE_VIEW_COLUMN_FIXED)
                column.set_fixed_width(width)
                self.iteration_view.append_column(column)
            iteration_scroll = gtk.ScrolledWindow()
            iteration_scroll.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
            iteration_scroll.set_shadow_type(gtk.SHADOW_ETCHED_IN)
            iteration_scroll.set_size_request(-1, 174)
            iteration_scroll.add(self.iteration_view)
            assistant_box.pack_start(iteration_scroll, False, False, 0)

            api_expander = gtk.Expander("模型与推理设置")
            api_expander.set_expanded(False)
            api_box = gtk.VBox(False, 6)
            api_box.set_border_width(8)
            api_url_row = gtk.HBox(False, 6)
            api_url_row.pack_start(self.label("Base URL"), False, False, 0)
            self.ai_base_entry = gtk.Entry()
            self.ai_base_entry.set_text("https://api.deepseek.com")
            api_url_row.pack_start(self.ai_base_entry, True, True, 0)
            api_model_row = gtk.HBox(False, 6)
            api_model_row.pack_start(self.label("模型"), False, False, 0)
            self.ai_model_entry = gtk.Entry()
            self.ai_model_entry.set_text("deepseek-flash")
            api_model_row.pack_start(self.ai_model_entry, True, True, 0)
            api_model_row.pack_start(self.label("API Key"), False, False, 0)
            self.ai_key_entry = gtk.Entry()
            self.ai_key_entry.set_visibility(False)
            self.ai_key_entry.set_width_chars(14)
            api_model_row.pack_end(self.ai_key_entry, False, False, 0)
            api_action_row = gtk.HBox(False, 6)
            self.ai_status_label = self.label("尚未配置")
            self.ai_thinking_toggle = gtk.CheckButton("深度思考")
            self.ai_thinking_toggle.set_tooltip_text("启用模型的 thinking mode；也可将模型名设为 deepseek-reasoner")
            api_save_button = gtk.Button("保存")
            api_save_button.connect("clicked", lambda *_: self.on_save_ai_config(False))
            api_test_button = gtk.Button("保存并测试")
            api_test_button.set_name("Accent")
            api_test_button.connect("clicked", lambda *_: self.on_save_ai_config(True))
            api_action_row.pack_start(self.ai_status_label, True, True, 0)
            api_action_row.pack_start(self.ai_thinking_toggle, False, False, 0)
            api_action_row.pack_end(api_test_button, False, False, 0)
            api_action_row.pack_end(api_save_button, False, False, 0)
            api_box.pack_start(api_url_row, False, False, 0)
            api_box.pack_start(api_model_row, False, False, 0)
            api_box.pack_start(api_action_row, False, False, 0)
            api_expander.add(api_box)
            assistant_box.pack_start(api_expander, False, False, 0)

            timeline_heading = gtk.HBox(False, 6)
            timeline_heading.pack_start(self.label("<span foreground='#172033'><b>实时执行记录</b></span>", True), True, True, 0)
            self.timeline_phase_label = self.label("LIVE")
            timeline_heading.pack_end(self.timeline_phase_label, False, False, 0)
            assistant_box.pack_start(timeline_heading, False, False, 0)
            self.chat_buffer = gtk.TextBuffer()
            self.chat_view = gtk.TextView(self.chat_buffer)
            self.chat_view.set_editable(False)
            self.chat_view.set_cursor_visible(False)
            self.chat_view.set_wrap_mode(gtk.WRAP_WORD_CHAR)
            self.chat_view.set_left_margin(8)
            self.chat_view.set_right_margin(8)
            self.chat_view.set_pixels_above_lines(3)
            self.chat_view.set_pixels_below_lines(3)
            chat_scroll = gtk.ScrolledWindow()
            chat_scroll.set_policy(gtk.POLICY_NEVER, gtk.POLICY_AUTOMATIC)
            chat_scroll.set_shadow_type(gtk.SHADOW_ETCHED_IN)
            chat_scroll.add(self.chat_view)
            self.append_chat("AITCAD", "告诉我目标 Vth。模型负责制定策略，控制器会实时修改工程、运行节点并提取 Id-Vg。")
            input_row = gtk.HBox(False, 7)
            self.chat_entry = gtk.Entry()
            self.chat_entry.set_text("")
            self.chat_entry.set_tooltip_text("例如：调整 P-well，使 Vth 达到 0.5±0.1 V，最多运行 8 次")
            self.chat_entry.connect("activate", self.on_send)
            self.send_button = gtk.Button("启动任务  ▶")
            self.send_button.set_name("Accent")
            self.send_button.connect("clicked", self.on_send)
            input_row.pack_start(self.chat_entry, True, True, 0)
            input_row.pack_end(self.send_button, False, False, 0)
            assistant_box.pack_start(chat_scroll, True, True, 0)
            assistant_box.pack_end(input_row, False, False, 0)
            notebook.append_page(assistant_box, self.label("AI 助手"))
            notebook.set_current_page(3)

            footer = gtk.HBox(False, 8)
            self.pin_toggle = gtk.CheckButton("置顶贴靠")
            self.pin_toggle.set_active(True)
            self.pin_toggle.connect("toggled", lambda button: self.set_keep_above(button.get_active()))
            dock_button = gtk.Button("重新贴靠")
            dock_button.connect("clicked", lambda *_: self.position_at_right_edge())
            footer.pack_start(self.pin_toggle, False, False, 0)
            footer.pack_end(dock_button, False, False, 0)
            body.pack_end(footer, False, False, 0)

        def append_chat(self, author, text):
            end = self.chat_buffer.get_end_iter()
            prefix = "\n" if self.chat_buffer.get_char_count() else ""
            self.chat_buffer.insert(end, prefix + "%s\n%s\n" % (author, text))
            self.chat_view.scroll_to_iter(self.chat_buffer.get_end_iter(), 0.0)

        def set_task_state(self, text, color="#70e1ff"):
            self.task_state_label.set_markup("<span foreground='%s'><b>● %s</b></span>" % (color, text))
            self.timeline_phase_label.set_text(text)

        def reset_task_dashboard(self, question):
            self.iteration_store.clear()
            self.iteration_rows = {}
            self.current_iteration = None
            self.task_target = None
            self.task_tolerance = None
            self.task_max_runs = None
            self.task_title_label.set_markup("<span foreground='#70e1ff' size='small'><b>VTH OPTIMIZATION · LIVE</b></span>")
            self.task_detail_label.set_text(question)
            self.reasoning_label.set_text("正在理解目标并生成实验策略…")
            self.task_progress.set_fraction(0.01)
            self.task_progress.set_text("ANALYZING")
            self.iteration_summary_label.set_text("0 次")
            self.set_task_state("深度思考中", "#ffd166")

        def ensure_iteration_row(self, data):
            try:
                run = int(data.get("run"))
            except (TypeError, ValueError):
                return None
            iterator = self.iteration_rows.get(run)
            if iterator is None:
                concentration = str(data.get("concentrationText") or "—")
                iterator = self.iteration_store.append((str(run), concentration, "—", "—", "—", "准备"))
                self.iteration_rows[run] = iterator
                self.iteration_summary_label.set_text("%d 次" % len(self.iteration_rows))
            return iterator

        def update_iteration(self, kind, data):
            iterator = self.ensure_iteration_row(data)
            if iterator is None:
                return
            run = int(data.get("run"))
            self.current_iteration = run
            phase = data.get("phase") or ""
            structure_node = data.get("structureNode")
            device_node = data.get("node")
            elapsed = data.get("elapsed")
            if data.get("concentrationText"):
                self.iteration_store.set_value(iterator, 1, str(data.get("concentrationText")))
            if phase == "materialize":
                self.iteration_store.set_value(iterator, 5, "创建分支")
            elif phase == "sde":
                text = "n%s" % structure_node if structure_node is not None else "SDE"
                if kind == "running" and elapsed is not None:
                    text += " · %ss" % elapsed
                self.iteration_store.set_value(iterator, 2, text)
                self.iteration_store.set_value(iterator, 5, "SDE 运行")
            elif phase == "sde_done":
                self.iteration_store.set_value(iterator, 2, "✓ n%s" % structure_node)
                self.iteration_store.set_value(iterator, 5, "SDevice")
            elif phase == "sdevice":
                text = "n%s" % device_node if device_node is not None else "SDevice"
                if kind == "running" and elapsed is not None:
                    text += " · %ss" % elapsed
                self.iteration_store.set_value(iterator, 3, text)
                self.iteration_store.set_value(iterator, 5, "仿真中")
            elif phase == "extract":
                self.iteration_store.set_value(iterator, 3, "✓ n%s" % device_node)
                self.iteration_store.set_value(iterator, 5, "提取 Vth")
            elif phase == "reused":
                self.iteration_store.set_value(iterator, 2, "✓ n%s" % structure_node)
                self.iteration_store.set_value(iterator, 3, "↻ n%s" % device_node)
                self.iteration_store.set_value(iterator, 5, "复用结果")
            if kind == "measurement":
                self.iteration_store.set_value(iterator, 2, "✓ n%s" % data.get("structureNode"))
                self.iteration_store.set_value(iterator, 3, "%s n%s" % ("↻" if data.get("reused") else "✓", data.get("node")))
                try:
                    self.iteration_store.set_value(iterator, 4, "%.6f" % float(data.get("vth")))
                except (TypeError, ValueError):
                    self.iteration_store.set_value(iterator, 4, "—")
                self.iteration_store.set_value(iterator, 5, "命中目标" if data.get("withinTolerance") else "已测量")

        def on_send(self, *_args):
            question = self.chat_entry.get_text().strip()
            if not question or self.ai_busy:
                return
            self.chat_entry.set_text("")
            self.append_chat("你", question)
            self.reset_task_dashboard(question)
            self.ai_busy = True
            self.send_button.set_sensitive(False)
            self.ai_status_label.set_markup("<span foreground='#b7791f'>AI / TCAD 任务执行中…</span>")
            threading.Thread(target=self.ai_chat_worker, args=(question, list(self.ai_history))).start()

        def load_ai_status_worker(self):
            try:
                result = ai_rpc({"action": "status"})
                gobject.idle_add(self.apply_ai_status, result)
            except Exception as error:
                gobject.idle_add(self.apply_ai_error, str(error))

        def apply_ai_status(self, result):
            self.ai_base_entry.set_text(result.get("baseUrl") or "https://api.deepseek.com")
            self.ai_model_entry.set_text(result.get("model") or "deepseek-flash")
            self.ai_thinking_toggle.set_active(bool(result.get("thinking")))
            if result.get("configured"):
                self.ai_status_label.set_markup("<span foreground='#15805d'>● API 已配置</span>")
            else:
                self.ai_status_label.set_markup("<span foreground='#718096'>○ 尚未配置 Key</span>")
            return False

        def on_save_ai_config(self, test_after):
            if self.ai_busy:
                return
            request = {
                "action": "save-config",
                "baseUrl": self.ai_base_entry.get_text().strip(),
                "model": self.ai_model_entry.get_text().strip(),
                "apiKey": self.ai_key_entry.get_text().strip(),
                "thinking": self.ai_thinking_toggle.get_active(),
            }
            self.ai_busy = True
            self.ai_status_label.set_markup("<span foreground='#b7791f'>正在保存…</span>")
            threading.Thread(target=self.save_ai_config_worker, args=(request, test_after)).start()

        def save_ai_config_worker(self, request, test_after):
            try:
                result = ai_rpc(request)
                if test_after:
                    test_result = ai_rpc({"action": "test"})
                    result["testText"] = test_result.get("text")
                    result["testModel"] = test_result.get("model")
                gobject.idle_add(self.apply_ai_config_saved, result)
            except Exception as error:
                gobject.idle_add(self.apply_ai_error, str(error))

        def apply_ai_config_saved(self, result):
            self.ai_busy = False
            self.ai_key_entry.set_text("")
            self.apply_ai_status(result)
            if result.get("testText"):
                self.append_chat("AITCAD", "API 测试成功：%s · %s" % (result.get("testModel"), result.get("testText")))
            else:
                self.append_chat("AITCAD", "API 配置已保存。")
            return False

        def ai_chat_worker(self, question, history):
            try:
                result = ai_rpc({
                    "action": "chat",
                    "project": self.project_absolute_path(),
                    "message": question,
                    "history": history,
                }, lambda event: gobject.idle_add(self.apply_ai_event, event))
                gobject.idle_add(self.apply_ai_response, question, result)
            except Exception as error:
                gobject.idle_add(self.apply_ai_error, str(error))

        def apply_ai_event(self, event):
            kind = event.get("event") or "progress"
            message = event.get("message") or ""
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if not message:
                return False
            labels = {
                "thinking": "◈ 深度分析",
                "reasoning": "◆ 思考摘要",
                "plan": "任务计划",
                "step": "执行步骤",
                "trial": "参数试验",
                "tool": "工具调用",
                "tool_result": "工具结果",
                "running": "仿真进度",
                "measurement": "测量结果",
                "result": "执行结果",
                "conclusion": "阶段结论",
            }
            self.append_chat(labels.get(kind, "任务进度"), message)
            self.ai_status_label.set_text("● %s" % message[:62])
            if kind == "thinking":
                self.set_task_state("深度思考中", "#ffd166")
                self.task_progress.pulse()
                self.task_progress.set_text("REASONING")
            elif kind == "reasoning":
                compact_reasoning = " ".join(message.split())
                if len(compact_reasoning) > 220:
                    compact_reasoning = compact_reasoning[:217] + "…"
                self.reasoning_label.set_text(compact_reasoning)
                if data.get("reasoningUsed"):
                    suffix = " · %s reasoning tokens" % data.get("reasoningTokens") if data.get("reasoningTokens") else ""
                    self.set_task_state("深度推理完成%s" % suffix, "#70e1ff")
                else:
                    self.set_task_state("策略已生成", "#70e1ff")
                self.task_progress.set_fraction(0.03)
                self.task_progress.set_text("PLAN READY")
            elif kind == "plan":
                self.task_target = data.get("targetV")
                self.task_tolerance = data.get("toleranceV")
                self.task_max_runs = None
                self.task_detail_label.set_text(message)
                self.set_task_state("闭环执行中", "#38d6b3")
                self.task_progress.set_fraction(0.05)
                self.task_progress.set_text("准备第 1 次迭代")
            elif kind in ("trial", "step", "running", "result", "measurement"):
                self.update_iteration(kind, data)
                run = data.get("run") or self.current_iteration or 0
                if kind == "measurement":
                    self.set_task_state("结果评估", "#70e1ff")
                else:
                    self.set_task_state("仿真运行中" if kind == "running" else "闭环执行中", "#38d6b3")
                self.task_progress.pulse()
                self.task_progress.set_text("第 %s 次迭代" % run)
                self.task_detail_label.set_text(message)
            elif kind == "conclusion":
                self.task_detail_label.set_text(message)
                self.set_task_state("任务完成", "#38d6b3")
                self.task_progress.set_fraction(1.0)
                self.task_progress.set_text("COMPLETE")
            if kind in ("step", "result", "measurement", "conclusion"):
                self.refresh(True)
            return False

        def apply_ai_response(self, question, result):
            self.ai_busy = False
            self.send_button.set_sensitive(True)
            self.ai_status_label.set_markup("<span foreground='#15805d'>● API 已配置</span>")
            tool_names = [item.get("name") for item in result.get("tools") or [] if item.get("name")]
            if tool_names:
                self.append_chat("工具", " → ".join(tool_names))
            answer = result.get("text") or "操作已完成。"
            self.append_chat("AITCAD · %s" % (result.get("model") or "MODEL"), answer)
            if self.task_progress.get_fraction() < 1.0:
                self.set_task_state("任务完成", "#38d6b3")
                self.task_progress.set_fraction(1.0)
                self.task_progress.set_text("COMPLETE")
            self.ai_history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
            self.ai_history = self.ai_history[-8:]
            self.refresh(True)
            return False

        def apply_ai_error(self, message):
            self.ai_busy = False
            self.send_button.set_sensitive(True)
            self.ai_status_label.set_text("● %s" % message[:70])
            self.task_detail_label.set_text(message)
            self.set_task_state("任务异常", "#ff7b8a")
            self.task_progress.set_text("STOPPED")
            self.append_chat("AITCAD", "API/Agent 操作失败：%s" % message)
            return False

        def on_project_changed(self, combo):
            index = combo.get_active()
            if index < 0 or index >= len(self.project_paths):
                return
            selected = self.project_paths[index]
            if selected != self.active_project:
                self.active_project = selected
                self.last_signature = None
                self.refresh(True)

        def on_parameter_selected(self, selection):
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            self.selected_parameter = model.get_value(iterator, 0)
            self.param_name_entry.set_text(self.selected_parameter)
            self.param_value_entry.set_text(model.get_value(iterator, 2))
            self.param_step_entry.set_text(model.get_value(iterator, 3))

        def on_node_selected(self, selection):
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            self.selected_node = int(model.get_value(iterator, 0))
            self.selected_node_tool = model.get_value(iterator, 1)
            self.run_node_entry.set_text(str(self.selected_node))
            self.selected_node_label.set_text("已选节点：%s · %s" % (self.selected_node, self.selected_node_tool))

        def project_absolute_path(self):
            if not self.workspace_root or not self.active_project:
                raise RuntimeError("尚未载入 SWB 工程")
            root = os.path.realpath(self.workspace_root)
            project = os.path.realpath(os.path.join(root, self.active_project))
            if not project.startswith(root + os.sep):
                raise RuntimeError("工程路径超出当前 STDB")
            return project

        def on_parameter_action(self, action):
            name = self.param_name_entry.get_text().strip()
            value = self.param_value_entry.get_text().strip()
            if not name or not value:
                self.show_message("请输入参数名称和值。")
                return
            step = None
            if action == "add-parameter":
                try:
                    step = int(self.param_step_entry.get_text().strip())
                except ValueError:
                    self.show_message("新增参数时，步骤必须是整数。")
                    return
            labels = {
                "set-parameter": "替换参数值",
                "add-values": "添加实验取值",
                "add-parameter": "新增参数",
            }
            self.append_chat("AITCAD", "正在%s：%s=%s …" % (labels[action], name, value))
            threading.Thread(target=self.parameter_worker, args=(action, name, value, step)).start()

        def parameter_worker(self, action, name, value, step):
            try:
                kwargs = {"name": name, "value": value}
                if step is not None:
                    kwargs["step"] = step
                result = live_action(action, self.project_absolute_path(), **kwargs)
                if action == "set-parameter":
                    message = "参数 %s：%s → %s" % (name, ",".join(result.get("previous") or []), ",".join(result.get("current") or []))
                elif action == "add-values":
                    message = "参数 %s 已添加取值：%s" % (name, ",".join(result.get("added") or []))
                else:
                    message = "已在步骤 %s 新增参数 %s：%s" % (result.get("step"), name, ",".join(result.get("current") or []))
                gobject.idle_add(self.action_success, "%s；SWB 已自动重载。" % message)
            except Exception as error:
                gobject.idle_add(self.action_error, str(error))

        def on_run_node(self, *_args):
            raw_node = self.run_node_entry.get_text().strip()
            if raw_node:
                try:
                    node = int(raw_node)
                except ValueError:
                    self.show_message("节点号必须是整数。")
                    return
            elif self.selected_node is not None:
                node = self.selected_node
            else:
                self.show_message("请在列表中选择节点，或直接输入节点号。")
                return
            tool = self.selected_node_tool if node == self.selected_node else ""
            self.selected_node = node
            self.run_node_entry.set_text(str(node))
            self.selected_node_label.set_text("已选节点：%s%s" % (node, " · %s" % tool if tool else ""))
            self.append_chat("AITCAD", "正在向 SWB 提交节点 %s（%s）…" % (node, tool))
            threading.Thread(target=self.run_node_worker, args=(node,)).start()

        def on_refresh_all(self, *_args):
            threading.Thread(target=self.refresh_all_worker).start()

        def refresh_all_worker(self):
            try:
                result = live_action("refresh-swb", self.project_absolute_path())
                gobject.idle_add(self.action_success, "已同步工程并刷新 %s 个 SWB 窗口。" % result.get("swbWindowsRefreshed", 0))
            except Exception as error:
                gobject.idle_add(self.action_error, str(error))

        def run_node_worker(self, node):
            try:
                result = live_action("run-node", self.project_absolute_path(), node=node)
                gobject.idle_add(self.action_success, "节点 %s 已提交给 gsub（PID %s）。SWB 将显示 queued/running/done；日志：%s" % (
                    node, result.get("pid"), result.get("log")
                ))
            except Exception as error:
                gobject.idle_add(self.action_error, str(error))

        def action_success(self, message):
            self.append_chat("AITCAD", message)
            self.refresh(True)
            return False

        def action_error(self, message):
            self.append_chat("AITCAD", "操作失败：%s" % message)
            self.show_message(message)
            return False

        def on_file_activated(self, _view, path, _column):
            model = self.file_view.get_model()
            iterator = model.get_iter(path)
            relative_path = model.get_value(iterator, 3)
            extension = os.path.splitext(relative_path)[1].lower().lstrip(".")
            if extension not in TEXT_EXTENSIONS:
                self.show_message("这个文件需要专用查看器，当前先支持文本预览。")
                return
            threading.Thread(target=self.load_preview_worker, args=(relative_path,)).start()

        def load_preview_worker(self, relative_path):
            try:
                result = rpc("file.readText", {"relativePath": relative_path})
                gobject.idle_add(self.show_preview, relative_path, result.get("content") or "")
            except Exception as error:
                gobject.idle_add(self.show_message, str(error))

        def show_preview(self, title, content):
            dialog = gtk.Dialog(title, self, gtk.DIALOG_DESTROY_WITH_PARENT, (gtk.STOCK_CLOSE, gtk.RESPONSE_CLOSE))
            dialog.set_default_size(820, 620)
            view = gtk.TextView()
            view.set_editable(False)
            view.modify_font(pango.FontDescription("Monospace 9"))
            view.get_buffer().set_text(content)
            scroll = gtk.ScrolledWindow()
            scroll.set_policy(gtk.POLICY_AUTOMATIC, gtk.POLICY_AUTOMATIC)
            scroll.add(view)
            dialog.vbox.pack_start(scroll, True, True, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()
            return False

        def show_message(self, message):
            dialog = gtk.MessageDialog(self, gtk.DIALOG_DESTROY_WITH_PARENT, gtk.MESSAGE_INFO, gtk.BUTTONS_OK, message)
            dialog.run()
            dialog.destroy()
            return False

        def refresh(self, manual=False):
            if self.refreshing:
                return True
            self.refreshing = True
            threading.Thread(target=self.refresh_worker, args=(manual,)).start()
            return True

        def refresh_worker(self, manual):
            try:
                status = rpc("system.probe")
                projects = status.get("projects") or []
                paths = [item.get("relativePath") for item in projects if item.get("relativePath")]
                project = self.active_project
                if project not in paths:
                    project = paths[0] if paths else ""
                tree = rpc("project.tree", {"relativePath": project, "maxDepth": 3}) if project else {"entries": []}
                workspace_root = (status.get("workspace") or {}).get("root") or ""
                project_path = os.path.realpath(os.path.join(workspace_root, project)) if project else ""
                live_state = read_live_swb_state(project_path) if project_path else {"parameters": [], "nodes": []}
                gobject.idle_add(self.apply_refresh, status, paths, project, tree.get("entries") or [], live_state, manual)
            except Exception as error:
                gobject.idle_add(self.apply_error, str(error))

        def apply_refresh(self, status, paths, project, entries, live_state, manual):
            self.refreshing = False
            self.status = status
            self.active_project = project
            if paths != self.project_paths:
                self.project_paths = paths
                self.project_combo.handler_block_by_func(self.on_project_changed)
                self.project_combo.get_model().clear()
                for path in paths:
                    self.project_combo.append_text(path)
                self.project_combo.set_active(paths.index(project) if project in paths else -1)
                self.project_combo.handler_unblock_by_func(self.on_project_changed)
            system = status.get("system") or {}
            workspace = status.get("workspace") or {}
            self.workspace_root = workspace.get("root") or ""
            self.system_label.set_text("Connector：v%s · %s@%s" % (status.get("connectorVersion", "?"), system.get("user", "user"), system.get("hostname", "linux")))
            self.workspace_label.set_text("工作区：%s" % workspace.get("root", "—"))
            signature = tuple((item.get("relativePath"), item.get("modifiedAt"), item.get("size")) for item in entries)
            changed = self.last_signature is not None and signature != self.last_signature
            self.last_signature = signature
            self.change_label.set_text("最近同步：%s%s" % (time.strftime("%H:%M:%S"), " · 检测到变化" if changed else ""))
            self.render_files(entries)
            self.render_live_state(live_state)
            if manual:
                self.append_chat("AITCAD", "已刷新工程：%s" % project)
            return False

        def render_files(self, entries):
            self.file_store.clear()
            files = [item for item in entries if item.get("type") == "file"]
            files.sort(key=lambda item: item.get("modifiedAt") or "", reverse=True)
            for item in files[:140]:
                extension = (item.get("extension") or "file").upper()[:4]
                modified = (item.get("modifiedAt") or "").replace("T", " ").replace("Z", "")[-8:]
                self.file_store.append((extension, item.get("name") or "", modified, item.get("relativePath") or ""))

        def render_live_state(self, live_state):
            self.param_store.clear()
            for item in live_state.get("parameters") or []:
                iterator = self.param_store.append((item.get("name") or "", item.get("default") or "", item.get("values") or "", item.get("step") or ""))
                if item.get("name") == self.selected_parameter:
                    self.param_view.get_selection().select_iter(iterator)
            self.node_store.clear()
            for item in live_state.get("nodes") or []:
                iterator = self.node_store.append((str(item.get("node") or ""), item.get("tool") or "", item.get("status") or "none", item.get("values") or ""))
                if item.get("node") and int(item.get("node")) == self.selected_node:
                    self.node_view.get_selection().select_iter(iterator)

        def apply_error(self, message):
            self.refreshing = False
            self.system_label.set_text("Connector：%s" % message)
            return False

    window = AssistantWindow()
    window.show_all()
    window.notebook.set_current_page(3)
    gtk.main()


def main():
    if "--probe" in sys.argv:
        probe()
        return 0
    lock = acquire_single_instance()
    if lock is None:
        print("AITCAD native assistant is already running")
        return 0
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
