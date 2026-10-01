#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""EmberTCAD 0.1.0 native GTK3 desktop application.

The proven 0.9 SWB/Agent bridge remains in native_assistant.py.  This module
only replaces the presentation shell and keeps the same local subprocess and
JSON-lines contracts, so the simulation path does not depend on the UI rewrite.
"""
from __future__ import print_function

import math
import json
import os
import re
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango

import native_assistant as core


APP_VERSION = "0.1.0"
MODEL_PROVIDERS = (
    ("openai", u"OpenAI API", "https://api.openai.com/v1", "gpt-6.1-sol", "responses", "medium"),
    ("deepseek", u"DeepSeek", "https://api.deepseek.com", "deepseek-flash", "chat_completions", "high"),
    ("anthropic", u"Anthropic Claude", "https://api.anthropic.com/v1", "claude-sonnet-5", "anthropic_messages", "auto"),
    ("gemini", u"Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash", "chat_completions", "auto"),
    ("openrouter", u"OpenRouter", "https://openrouter.ai/api/v1", "~openai/gpt-latest", "chat_completions", "auto"),
    ("siliconflow", u"SiliconFlow", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V4-Flash", "chat_completions", "auto"),
    ("moonshot", u"Moonshot", "https://api.moonshot.cn/v1", "kimi-k2.5", "chat_completions", "auto"),
    ("zhipu", u"智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-5", "chat_completions", "auto"),
    ("dashscope", u"阿里百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max", "chat_completions", "auto"),
    ("custom", u"自定义 OpenAI 兼容服务", "", "", "chat_completions", "auto"),
)
API_STYLES = (
    ("responses", u"Responses API（OpenAI 推理与工具调用）"),
    ("chat_completions", u"Chat Completions（通用兼容接口）"),
    ("anthropic_messages", u"Anthropic Messages API"),
)
PROVIDER_REASONING_EFFORTS = {
    "openai": (("auto", u"自动：使用模型默认值"), ("low", u"低"),
               ("medium", u"中（GPT-6.1 Sol 默认）"), ("high", u"高"),
               ("xhigh", u"超高"), ("max", u"最大")),
    "deepseek": (("auto", u"自动：使用 DeepSeek 默认值"),
                 ("standard", u"关闭推理：速度优先"), ("low", u"低"),
                 ("high", u"高"), ("max", u"最大")),
    "anthropic": (("auto", u"自动：使用 Claude 默认值"),
                  ("standard", u"标准回答：不启用扩展思考"),
                  ("high", u"扩展思考")),
    "default": (("auto", u"自动：使用模型默认值"),
                ("standard", u"标准：不发送额外推理参数")),
}
MODEL_SUGGESTIONS = {
    "openai": ("gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-terra"),
    "deepseek": ("deepseek-flash", "deepseek-v4-pro"),
    "anthropic": ("claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    "gemini": ("gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.5-flash-lite"),
    "openrouter": ("~openai/gpt-latest", "~anthropic/claude-sonnet-latest", "openai/gpt-6.1-sol", "deepseek/deepseek-v4-pro", "google/gemini-3.8-flash"),
    "siliconflow": ("deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4-Pro"),
    "moonshot": ("kimi-k2.5",),
    "zhipu": ("glm-5", "glm-4.7"),
    "dashscope": ("qwen3-max", "qwen3.5-plus", "qwen3.5-flash"),
}
CSS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "aitcad.css")
LOGO_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets", "embertcad-logo.png")

try:
    text_type = unicode
    binary_type = str
except NameError:  # pragma: no cover - allows local Python 3 syntax checks.
    text_type = str
    binary_type = bytes


def as_text(value):
    if value is None:
        return u""
    if isinstance(value, text_type):
        return value
    if isinstance(value, binary_type):
        return value.decode("utf-8", "replace")
    try:
        return text_type(value)
    except Exception:
        return u""


def available_project_name(parent, requested):
    """Return a non-existing sibling name without ever replacing user data."""
    parent = os.path.realpath(as_text(parent or u""))
    requested = as_text(requested or u"").strip()
    if not parent or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$", requested):
        return requested
    if not os.path.lexists(os.path.join(parent, requested)):
        return requested
    for index in range(2, 1000):
        suffix = u"_%d" % index
        candidate = requested[:48 - len(suffix)] + suffix
        if not os.path.lexists(os.path.join(parent, candidate)):
            return candidate
    return requested


def error_text(error):
    """Return an exception message without triggering Python 2 ASCII encoding."""
    message = getattr(error, "message", None)
    if message:
        text = as_text(message).strip()
        if text:
            return text
    parts = []
    for value in getattr(error, "args", ()):
        text = as_text(value).strip()
        if text:
            parts.append(text)
    if parts:
        return u"；".join(parts)
    text = as_text(error).strip()
    return text or as_text(type(error).__name__) or u"未知错误"


def append_buffer_text(buffer, value, limit=12000):
    """Append without rebuilding the complete Gtk.TextBuffer on every event.

    Repeated get_text/set_text cycles made long-running task pages noticeably
    sluggish over the CentOS remote desktop.  Keep a bounded live tail instead.
    """
    text = as_text(value)
    if not text:
        return
    buffer.insert(buffer.get_end_iter(), text)
    count = buffer.get_char_count()
    if count > limit:
        trim_to = min(count, count - limit + 1200)
        buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_offset(trim_to))


def clean_visible_text(value):
    """Render model summaries cleanly in a native TextView, not as raw Markdown."""
    text = as_text(value).replace(u"**", u"").replace(u"`", u"")
    text = re.sub(r"^\s*[-*]\s+", u"• ", text, flags=re.M)
    return text.strip()


def add_class(widget, name):
    widget.get_style_context().add_class(name)
    return widget


def set_margins(widget, top=0, right=0, bottom=0, left=0):
    widget.set_margin_top(top)
    widget.set_margin_end(right)
    widget.set_margin_bottom(bottom)
    widget.set_margin_start(left)
    return widget


def label(text=u"", css=None, wrap=False, xalign=0.0):
    item = Gtk.Label(label=as_text(text))
    item.set_xalign(xalign)
    item.set_yalign(0.5)
    item.set_line_wrap(wrap)
    item.set_selectable(False)
    if wrap:
        item.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # A wrapped Gtk.Label otherwise advertises its full one-line natural
        # width and can force an entire page wider than the viewport.
        item.set_width_chars(1)
        item.set_max_width_chars(88)
        item.set_hexpand(True)
        item.set_halign(Gtk.Align.FILL)
    if css:
        add_class(item, css)
    return item


def button(text, icon=None, css=None, tooltip=None):
    item = Gtk.Button()
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
    row.set_halign(Gtk.Align.CENTER)
    if icon:
        image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
        row.pack_start(image, False, False, 0)
    row.pack_start(Gtk.Label(label=as_text(text)), False, False, 0)
    item.add(row)
    if css:
        add_class(item, css)
    if tooltip:
        item.set_tooltip_text(as_text(tooltip))
    return item


def card(content, css="card", padding=15):
    frame = Gtk.Frame()
    frame.set_shadow_type(Gtk.ShadowType.NONE)
    add_class(frame, css)
    set_margins(content, padding, padding, padding, padding)
    frame.add(content)
    return frame


def forward_scroll_at_edge(scroll, event):
    """Pass wheel motion to the outer page after an inner pane reaches an edge."""
    direction = event.direction
    delta = 0.0
    if direction == Gdk.ScrollDirection.DOWN:
        delta = 1.0
    elif direction == Gdk.ScrollDirection.UP:
        delta = -1.0
    elif direction == Gdk.ScrollDirection.SMOOTH:
        try:
            values = event.get_scroll_deltas()
            if values and values[0]:
                delta = float(values[2])
        except Exception:
            return False
    if not delta:
        return False
    adjustment = scroll.get_vadjustment()
    lower = adjustment.get_lower()
    upper = max(lower, adjustment.get_upper() - adjustment.get_page_size())
    at_edge = (delta > 0 and adjustment.get_value() >= upper - 1.0) or (
        delta < 0 and adjustment.get_value() <= lower + 1.0
    )
    if not at_edge:
        return False
    parent = scroll.get_parent()
    while parent is not None:
        if isinstance(parent, Gtk.ScrolledWindow):
            outer = parent.get_vadjustment()
            outer_lower = outer.get_lower()
            outer_upper = max(outer_lower, outer.get_upper() - outer.get_page_size())
            step = max(48.0, outer.get_step_increment() * 4.0)
            value = max(outer_lower, min(outer_upper, outer.get_value() + delta * step))
            if abs(value - outer.get_value()) > 0.5:
                outer.set_value(value)
                return True
        parent = parent.get_parent()
    return False


def scroller(child, horizontal=Gtk.PolicyType.NEVER, vertical=Gtk.PolicyType.AUTOMATIC):
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(horizontal, vertical)
    scroll.set_shadow_type(Gtk.ShadowType.NONE)
    # GTK 3 on CentOS 7 can otherwise feed a wrapped child's changing natural
    # width back into the top-level every time a wheel event relayouts it.
    # The viewport owns scrolling; its content must never resize the window.
    try:
        scroll.set_propagate_natural_width(False)
        scroll.set_propagate_natural_height(False)
        # Persistent scrollbars are easier to discover and operate on the
        # CentOS 7 desktop than narrow overlay indicators.
        scroll.set_overlay_scrolling(False)
    except AttributeError:
        pass
    scroll.connect("scroll-event", forward_scroll_at_edge)
    child.set_hexpand(True)
    scroll.add(child)
    return scroll


def logo_image(size):
    if os.path.isfile(LOGO_PATH):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(LOGO_PATH, size, size, True)
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except Exception:
            pass
    fallback = label(u"N", "brand-mark", xalign=0.5)
    fallback.set_size_request(size, size)
    return fallback


def start_thread(target, args=()):
    worker = threading.Thread(target=target, args=args)
    worker.daemon = True
    worker.start()
    return worker


class LabelTextBuffer(object):
    """Small set_text adapter for read-only wrapped report labels."""

    def __init__(self, target):
        self.target = target

    def set_text(self, value):
        self.target.set_text(as_text(value))


class WrappingTextView(Gtk.TextView):
    """TextView whose preferred width is independent of long model output."""

    def do_get_preferred_width(self):
        return (120, 520)


class TrajectoryChart(Gtk.DrawingArea):
    """Dependency-free Cairo chart for concentration-to-Vth search history."""

    def __init__(self, compact=False):
        Gtk.DrawingArea.__init__(self)
        self.compact = compact
        self.points = []
        self.target = None
        self.tolerance = None
        self.set_size_request(-1, 135 if compact else 235)
        self.connect("draw", self.on_draw)

    def set_data(self, points, target=None, tolerance=None):
        cleaned = []
        for point in points or []:
            try:
                concentration = float(point.get("concentration"))
                vth = float(point.get("vth"))
            except (TypeError, ValueError):
                continue
            if concentration > 0 and not math.isnan(vth) and not math.isinf(vth):
                cleaned.append({
                    "run": point.get("run"),
                    "concentration": concentration,
                    "vth": vth,
                    "withinTolerance": bool(point.get("withinTolerance")),
                })
        self.points = cleaned
        self.target = target
        self.tolerance = tolerance
        self.queue_draw()

    @staticmethod
    def set_color(context, color):
        context.set_source_rgb(color[0], color[1], color[2])

    def draw_text(self, context, text, x, y, color=(0.40, 0.45, 0.52), size=10):
        self.set_color(context, color)
        context.select_font_face("Sans", 0, 0)
        context.set_font_size(size)
        context.move_to(x, y)
        context.show_text(as_text(text).encode("utf-8"))

    def on_draw(self, _widget, context):
        width = max(220.0, float(self.get_allocated_width()))
        height = max(100.0, float(self.get_allocated_height()))
        left = 42.0 if not self.compact else 12.0
        right = 14.0
        top = 14.0
        bottom = 34.0 if not self.compact else 12.0
        plot_width = max(10.0, width - left - right)
        plot_height = max(10.0, height - top - bottom)

        self.set_color(context, (0.985, 0.989, 0.994))
        context.rectangle(left, top, plot_width, plot_height)
        context.fill()

        values = [item["vth"] for item in self.points]
        if self.target is not None and self.tolerance is not None:
            values.extend([float(self.target) - float(self.tolerance), float(self.target) + float(self.tolerance)])
        if not values:
            self.draw_text(context, u"运行任务后将在这里显示参数—Vth 搜索轨迹", left + 14, top + plot_height / 2.0, size=10)
            return False

        y_min = min(values)
        y_max = max(values)
        padding = max(0.04, (y_max - y_min) * 0.16)
        y_min -= padding
        y_max += padding
        if y_max <= y_min:
            y_max = y_min + 1.0

        x_values = [math.log10(item["concentration"]) for item in self.points]
        x_min = min(x_values) if x_values else 14.0
        x_max = max(x_values) if x_values else 21.0
        if x_max <= x_min:
            x_min -= 0.5
            x_max += 0.5

        def x_pos(value):
            return left + (math.log10(value) - x_min) / (x_max - x_min) * plot_width

        def y_pos(value):
            return top + (y_max - value) / (y_max - y_min) * plot_height

        if self.target is not None and self.tolerance is not None:
            band_top = y_pos(float(self.target) + float(self.tolerance))
            band_bottom = y_pos(float(self.target) - float(self.tolerance))
            self.set_color(context, (0.87, 0.97, 0.93))
            context.rectangle(left, band_top, plot_width, max(2.0, band_bottom - band_top))
            context.fill()
            self.set_color(context, (0.52, 0.80, 0.68))
            context.set_line_width(1.0)
            context.move_to(left, y_pos(float(self.target)))
            context.line_to(left + plot_width, y_pos(float(self.target)))
            context.stroke()

        self.set_color(context, (0.89, 0.91, 0.93))
        context.set_line_width(1.0)
        for fraction in (0.0, 0.5, 1.0):
            y = top + plot_height * fraction
            context.move_to(left, y)
            context.line_to(left + plot_width, y)
            context.stroke()

        if len(self.points) > 1:
            self.set_color(context, (0.145, 0.388, 0.922))
            context.set_line_width(2.5)
            for index, item in enumerate(self.points):
                x = x_pos(item["concentration"])
                y = y_pos(item["vth"])
                if index == 0:
                    context.move_to(x, y)
                else:
                    context.line_to(x, y)
            context.stroke()

        for index, item in enumerate(self.points):
            x = x_pos(item["concentration"])
            y = y_pos(item["vth"])
            is_last = index == len(self.points) - 1
            radius = 6.0 if is_last else 4.5
            if item.get("withinTolerance"):
                self.set_color(context, (0.02, 0.47, 0.34))
            else:
                self.set_color(context, (0.145, 0.388, 0.922))
            context.arc(x, y, radius, 0, math.pi * 2)
            context.fill()
            self.set_color(context, (1.0, 1.0, 1.0))
            context.set_line_width(2.0)
            context.arc(x, y, radius, 0, math.pi * 2)
            context.stroke()

        if not self.compact:
            self.draw_text(context, "%.3g" % y_max, 2, top + 4)
            self.draw_text(context, "%.3g" % y_min, 2, top + plot_height)
            self.draw_text(context, "1e%.0f" % x_min, left, height - 9)
            right_text = "1e%.0f" % x_max
            self.draw_text(context, right_text, width - 42, height - 9)
            if self.points:
                latest = self.points[-1]
                text = "%.6f V" % latest["vth"]
                self.draw_text(context, text, max(left, width - 92), top + 13, (0.11, 0.31, 0.72), 10)
        return False


class AssistantWindow(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="EmberTCAD")
        self.set_role("embertcad")
        self.connect("destroy", Gtk.main_quit)
        self.set_border_width(0)
        self.set_resizable(True)
        self.set_default_size(1400, 900)
        self.set_position(Gtk.WindowPosition.CENTER)
        if os.path.isfile(LOGO_PATH):
            try:
                self.set_icon_from_file(LOGO_PATH)
            except Exception:
                pass

        self.status = None
        self.projects = []
        self.active_project = core.DEFAULT_PROJECT
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
        self.task_max_runs = None
        self.trajectory_points = []
        self.nav_buttons = {}
        self.chat_views = []
        self.chat_entries = []
        self.send_buttons = []
        self.ai_status_labels = []
        self.task_state_labels = []
        self.task_title_labels = []
        self.task_detail_labels = []
        self.task_progress_bars = []
        self.task_progress_text_labels = []
        self.best_parameter_labels = []
        self.best_vth_labels = []
        self.summary_status_labels = []
        self.charts = []
        self.event_lists = []
        self.last_results_load = 0
        self.results_loading = False
        self.results_project = None
        self.loaded_result_signature = None
        self.research_status = None
        self.research_plan = None
        self.research_busy = False
        self.manual_result_rows = []
        self.selected_history_task_id = None
        self.project_create_plan = None
        self.current_live_state = {"parameters": [], "nodes": []}
        self.project_report = None
        self.project_report_task_id = None
        self.project_report_loading = False
        self.project_read_started_at = None
        self.last_project_report_name = None
        self.workspace_tasks = []
        self.history_tasks = []
        self.history_projects = []
        self.history_project_tasks = []
        self.selected_history_project = None
        self.history_selected_task = None
        self.selected_workspace_task_id = None
        self.current_workspace_task = None
        self.workspace_busy = False
        self.current_workspace_execution_id = None
        self.selected_result_task_id = None
        self.pending_project_read = None
        self.project_session_started = False
        self.onboarding_project_paths = []
        self.workspace_flow_stage = "select"
        self.workspace_view_stage = "select"
        self.workspace_unlocked_index = 1
        self.workspace_step_widgets = []
        self.workspace_read_steps = {}
        self.workspace_plan_steps = {}
        self.workspace_plan_input_widgets = {}
        self.current_run_id = None
        self.active_ai_process = None
        self.execution_stopping = False
        self.execution_pulse_source = None
        self.last_workspace_progress_persist = 0.0
        self.generation_task_id = None
        self.generation_run_id = None
        self.generation_cancel_requested = False
        self.generation_project_path = None
        self.generation_target_parent = None
        self.generation_reference = None
        self.generation_reference_busy = False
        self.generation_ai_process = None
        self.generation_busy = False
        self.generation_started_at = None
        self.generation_last_event_at = None
        self.generation_reconcile_busy = False
        self.pending_repair_goal = None
        self.generation_last_reconcile_at = 0.0
        self.new_project_active_stage = "basic"
        self.new_project_view_stage = "basic"
        self.new_project_unlocked_index = 0
        self.new_project_reached_stages = set(["basic"])

        self.iteration_store = Gtk.ListStore(str, str, str, str, str, str, str)
        self.file_store = Gtk.ListStore(str, str, str, str)
        self.param_store = Gtk.ListStore(str, str, str, str)
        self.node_store = Gtk.ListStore(str, str, str, str)
        self.manual_store = Gtk.ListStore(str, str, str, str, str)
        self.history_store = Gtk.ListStore(str, str, str, str, str, str)
        self.history_detail_store = Gtk.ListStore(str, str, str, str, str, str)
        self.model_param_store = Gtk.ListStore(str, str, str, str, str)
        self.workspace_task_store = Gtk.ListStore(str, str, str, str, str, str)
        self.report_task_store = Gtk.ListStore(str, str, str, str, str, str)

        self.chat_buffer = Gtk.TextBuffer()
        self.chat_buffer.create_tag("author_ai", foreground="#1d4ed8", weight=Pango.Weight.BOLD)
        self.chat_buffer.create_tag("author_user", foreground="#111827", weight=Pango.Weight.BOLD)
        self.chat_buffer.create_tag("author_event", foreground="#047857", weight=Pango.Weight.BOLD)
        self.chat_buffer.create_tag("body", foreground="#3f4a59")

        self.build_ui()
        # Remote X11/VNC redraws are expensive on CentOS 7.  Human-visible
        # progress does not need 5-6 repaints per second, and project polling
        # must not compete with an active model request.
        GLib.timeout_add(500, self.pulse_generation)
        GLib.timeout_add(3000, self.refresh)
        self.refresh()
        start_thread(self.load_ai_status_worker)
        start_thread(self.load_research_status_worker)
        self.append_chat("EmberTCAD", u"先选择并读取工程，再告诉我你希望完成什么。执行前我会先给出可审核的方案。")

    def build_ui(self):
        self.studio_shell = self.build_studio_shell()
        self.add(self.studio_shell)

    def build_brand(self, full=True):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        row.set_valign(Gtk.Align.CENTER)
        mark = logo_image(38 if full else 32)
        mark.set_valign(Gtk.Align.CENTER)
        row.pack_start(mark, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        titles.set_valign(Gtk.Align.CENTER)
        titles.set_vexpand(False)
        brand_title = label("EmberTCAD", "brand-title")
        brand_title.set_yalign(0.5)
        titles.pack_start(brand_title, False, False, 0)
        if full:
            subtitle = label("Open-source AI Assistant for Sentaurus TCAD · v%s" % APP_VERSION, "small-muted")
            subtitle.set_ellipsize(Pango.EllipsizeMode.END)
            subtitle.set_yalign(0.5)
            titles.pack_start(subtitle, False, False, 0)
        row.pack_start(titles, False, False, 0)
        return row

    def build_studio_header(self):
        header = add_class(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14), "app-header")
        set_margins(header, 10, 14, 10, 14)
        header.pack_start(self.build_brand(True), False, False, 0)

        project_area = add_class(Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8), "header-project")
        project_area.set_valign(Gtk.Align.CENTER)
        project_area.pack_start(Gtk.Image.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.project_combo = Gtk.ComboBoxText()
        self.project_combo.set_hexpand(True)
        self.project_combo.connect("changed", self.on_project_changed)
        project_area.pack_start(self.project_combo, True, True, 0)
        header.pack_start(project_area, True, True, 12)

        return header

    def build_studio_shell(self):
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        shell.set_hexpand(True)
        shell.set_vexpand(True)
        shell.set_halign(Gtk.Align.FILL)
        shell.pack_start(self.build_studio_header(), False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_hexpand(True)
        body.set_vexpand(True)
        body.set_halign(Gtk.Align.FILL)
        navigation = self.build_navigation()
        navigation.set_hexpand(False)
        navigation.set_halign(Gtk.Align.START)
        body.pack_start(navigation, False, False, 0)

        self.page_stack = Gtk.Stack()
        self.page_stack.set_hexpand(True)
        self.page_stack.set_vexpand(True)
        self.page_stack.set_halign(Gtk.Align.FILL)
        self.page_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.page_stack.set_transition_duration(160)
        self.page_stack.add_named(self.build_dashboard_page(), "dashboard")
        self.page_stack.add_named(self.build_project_page(), "project")
        self.page_stack.add_named(self.build_history_page(), "history")
        self.page_stack.add_named(self.build_new_project_page(), "new-project")
        # Preserve the legacy detail widgets for old history rows; no navigation
        # entry or active workflow routes new Tool changes to this page.
        self.page_stack.add_named(self.build_code_page(), "code")
        self.page_stack.add_named(self.build_manuals_page(), "manuals")
        self.page_stack.add_named(self.build_settings_page(), "settings")
        # The application has one content surface.  Conversation and execution
        # feedback live in the task workspace; no secondary inspector is
        # allowed to reserve width or resize the top-level.
        body.pack_start(self.page_stack, True, True, 0)
        shell.pack_start(body, True, True, 0)
        return shell

    def build_navigation(self):
        panel = add_class(Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3), "sidebar")
        # Keep the complete application usable at the 1000 px release target.
        # 205 px made the hidden-page stack impose a 1013 px window minimum on
        # CentOS 7; all current navigation labels fit cleanly at 192 px.
        panel.set_size_request(192, -1)
        # Descendant buttons must not make the whole horizontal shell treat the
        # sidebar as expandable.  The sidebar owns a fixed left column; only the
        # page stack receives the remaining width.
        panel.set_hexpand(False)
        panel.set_halign(Gtk.Align.START)
        set_margins(panel, 14, 10, 14, 10)
        panel.pack_start(label(u"工作", "small-muted"), False, False, 7)
        items = [("dashboard", u"任务工作台", "go-home-symbolic")]
        for name, text, icon in items:
            self.add_nav_button(panel, name, text, icon)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(10)
        separator.set_margin_bottom(10)
        panel.pack_start(separator, False, False, 0)
        panel.pack_start(label(u"构建与研究", "small-muted"), False, False, 7)
        future = [
            ("new-project", u"从零创建工程", "folder-new-symbolic"),
            ("manuals", u"手册中心", "help-browser-symbolic"),
        ]
        for name, text, icon in future:
            self.add_nav_button(panel, name, text, icon)

        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(10)
        separator.set_margin_bottom(10)
        panel.pack_start(separator, False, False, 0)
        panel.pack_start(label(u"记录", "small-muted"), False, False, 7)
        self.add_nav_button(panel, "history", u"任务历史", "document-open-recent-symbolic")

        panel.pack_start(Gtk.Box(), True, True, 0)
        self.add_nav_button(panel, "settings", u"设置", "preferences-system-symbolic")
        self.set_active_nav("dashboard")
        return panel

    def add_nav_button(self, parent, name, text, icon):
        item = button(text, icon, "nav-button")
        item.set_hexpand(False)
        item.set_halign(Gtk.Align.FILL)
        item.get_child().set_halign(Gtk.Align.START)
        item.connect("clicked", lambda _button, page=name: self.show_page(page))
        self.nav_buttons[name] = item
        parent.pack_start(item, False, False, 0)

    def set_active_nav(self, name):
        for page, item in self.nav_buttons.items():
            context = item.get_style_context()
            if page == name:
                context.add_class("active")
            else:
                context.remove_class("active")

    def show_page(self, name):
        self.page_stack.set_visible_child_name(name)
        self.set_active_nav(name)
        if name == "history":
            if hasattr(self, "history_page_stack"):
                self.history_page_stack.set_visible_child_name("projects")
            self.on_refresh_history()
        elif name == "dashboard":
            self.on_refresh_workspace_tasks()

    def page_container(self, content):
        set_margins(content, 20, 20, 20, 20)
        # Product pages never scroll sideways. Wide tables own their internal
        # horizontal scrolling; all ordinary page text must wrap to this view.
        page = scroller(content, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        add_class(page, "page-scroll")
        return page

    def build_flow_progress_row(self, store, key, heading, detail):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = label(heading, "title-medium", True)
        status = label(u"等待", "small-muted")
        head.pack_start(title, True, True, 0)
        head.pack_end(status, False, False, 0)
        row.pack_start(head, False, False, 0)
        detail_label = label(detail, "small-muted", True)
        row.pack_start(detail_label, False, False, 0)
        progress = Gtk.ProgressBar()
        progress.set_fraction(0.0)
        row.pack_start(progress, False, False, 0)
        store[key] = {"bar": progress, "status": status, "detail": detail_label}
        return card(row, "flow-progress-row", 11)

    def build_execution_phase_cell(self, store, key, heading):
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        title = label(heading, "small-muted", True, 0.5)
        status = label(u"等待", "small-muted", True, 0.5)
        progress = Gtk.ProgressBar()
        progress.set_pulse_step(0.08)
        progress.set_size_request(36, -1)
        add_class(progress, "execution-phase-progress")
        cell.pack_start(title, False, False, 0)
        cell.pack_start(progress, False, False, 0)
        cell.pack_start(status, False, False, 0)
        # Detailed phase text belongs in the execution summary/log, not in the
        # five-column strip where it could establish an oversized minimum width.
        detail = Gtk.Label()
        store[key] = {"bar": progress, "status": status, "detail": detail}
        return card(cell, "flow-progress-row", 7)

    def build_dashboard_page(self):
        self.dashboard_state_stack = Gtk.Stack()
        if hasattr(self.dashboard_state_stack, "set_hhomogeneous"):
            self.dashboard_state_stack.set_hhomogeneous(False)
            self.dashboard_state_stack.set_vhomogeneous(False)
        else:
            self.dashboard_state_stack.set_homogeneous(False)
        self.dashboard_state_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.dashboard_state_stack.set_transition_duration(180)

        # The 0.14 welcome surface is intentionally preserved as the product's
        # stable entry point.  The workflow only changes after a project is chosen.
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        welcome.set_valign(Gtk.Align.CENTER)
        welcome.set_halign(Gtk.Align.FILL)
        # Keep the mature centered welcome composition, but do not let its
        # decorative gutters establish a >1000 px minimum window width.
        welcome.set_margin_start(35)
        welcome.set_margin_end(35)
        welcome.set_margin_top(52)
        welcome.set_margin_bottom(52)
        welcome_mark = logo_image(88)
        welcome_mark.set_halign(Gtk.Align.CENTER)
        welcome.pack_start(welcome_mark, False, False, 0)
        welcome_eyebrow = label(u"理解工程 · 审查方案 · 真实验证", "welcome-eyebrow", False, 0.5)
        welcome_eyebrow.set_halign(Gtk.Align.CENTER)
        welcome.pack_start(welcome_eyebrow, False, False, 0)
        welcome.pack_start(label(u"从一个 SWB Project 开始", "welcome-title", True, 0.5), False, False, 0)
        welcome.pack_start(label(
            u"选择工程后，EmberTCAD 会先读取真实源文件和节点，再调用已配置的 AI 深度分析。\n"
            u"你会在主界面看到每一步反馈；AI 完成接手报告后，才进入需求对话。",
            "welcome-subtitle", True, 0.5,
        ), False, False, 0)
        welcome_steps = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        welcome_steps.set_homogeneous(True)
        for number, heading, detail in (
            (u"1", u"选择 Project", u"从受管 STDB 列表或目录选择"),
            (u"2", u"AI 深度阅读", u"源文件、Tool 链、参数和节点"),
            (u"3", u"开始对话", u"先出方案，确认后执行与验证"),
        ):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
            number_label = label(number, "welcome-step-number", xalign=0.5)
            number_label.set_halign(Gtk.Align.CENTER)
            number_label.set_size_request(31, 31)
            box.pack_start(number_label, False, False, 0)
            box.pack_start(label(heading, "title-medium", True, 0.5), False, False, 0)
            box.pack_start(label(detail, "small-muted", True, 0.5), False, False, 0)
            welcome_steps.pack_start(card(box, "welcome-step", 15), True, True, 0)
        welcome.pack_start(welcome_steps, False, False, 4)
        chooser = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        chooser_title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chooser_title.pack_start(label(u"选择最近发现的工程", "title-medium"), True, True, 0)
        chooser_title.pack_end(label(u"只读接手", "safe-pill"), False, False, 0)
        chooser.pack_start(chooser_title, False, False, 0)
        choose_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        self.onboarding_project_combo = Gtk.ComboBoxText()
        self.onboarding_project_combo.set_hexpand(True)
        choose_row.pack_start(self.onboarding_project_combo, True, True, 0)
        start_button = button(u"选择并让 AI 阅读", "go-next-symbolic", "primary")
        start_button.connect("clicked", self.on_start_onboarding_project)
        choose_row.pack_end(start_button, False, False, 0)
        chooser.pack_start(choose_row, False, False, 0)
        chooser.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 4)
        browse = button(u"浏览其他 Project 路径", "folder-open-symbolic")
        browse.set_halign(Gtk.Align.CENTER)
        browse.connect("clicked", self.on_choose_workspace_project)
        chooser.pack_start(browse, False, False, 0)
        chooser.pack_start(label(
            u"AI 阅读会把所选工程的用户源文件发送给当前配置的模型；不会发送生成结果、密钥或工作区外文件，也不会修改工程。",
            "small-muted", True, 0.5,
        ), False, False, 0)
        welcome.pack_start(card(chooser, "welcome-chooser", 18), False, False, 4)
        self.dashboard_state_stack.add_named(welcome, "welcome")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.pack_start(label(u"任务工作台", "title-large"), False, False, 0)
        title_box.pack_start(label(u"一次只处理当前步骤；完成后才进入下一阶段。", "muted", True), False, False, 0)
        header.pack_start(title_box, True, True, 0)
        self.workspace_active_count_label = label(u"0 个运行任务", "planned-pill")
        self.workspace_project_count_label = label(u"0", "small-muted")
        self.workspace_report_count_label = label(u"0", "small-muted")
        header.pack_end(self.workspace_active_count_label, False, False, 0)
        new_task = button(u"新建任务", "list-add-symbolic", "primary")
        new_task.connect("clicked", self.on_new_workspace_task)
        header.pack_end(new_task, False, False, 0)
        root.pack_start(header, False, False, 0)

        stepper = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        stepper.set_homogeneous(True)
        for index, (stage_id, title) in enumerate((
            ("select", u"选择工程"), ("reading", u"AI 理解"), ("goal", u"描述目标"),
            ("plan", u"审查方案"), ("execute", u"执行验证"), ("complete", u"完成"),
        ), 1):
            step_button = Gtk.Button()
            add_class(step_button, "flow-step")
            step_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            number_label = label(as_text(index), "flow-step-number", xalign=0.5)
            title_label = label(title, "flow-step-title", True, 0.5)
            step_content.pack_start(number_label, False, False, 0)
            step_content.pack_start(title_label, False, False, 0)
            step_button.add(step_content)
            step_button.connect("clicked", self.on_workspace_stage_clicked, stage_id)
            stepper.pack_start(step_button, True, True, 0)
            self.workspace_step_widgets.append({
                "id": stage_id, "button": step_button, "number": number_label,
                "title": title_label, "index": index - 1,
            })
        root.pack_start(stepper, False, False, 0)

        self.workspace_flow_stack = Gtk.Stack()
        if hasattr(self.workspace_flow_stack, "set_hhomogeneous"):
            self.workspace_flow_stack.set_hhomogeneous(False)
            self.workspace_flow_stack.set_vhomogeneous(False)
        self.workspace_flow_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.workspace_flow_stack.set_transition_duration(220)

        # Stage 1 inside an existing session is a read-only project summary.
        # It must never mean "discard the current task and return to welcome".
        # Starting over remains an explicit action with its own button.
        selected = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        selected.set_valign(Gtk.Align.CENTER)
        selected_head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        selected_head.set_halign(Gtk.Align.CENTER)
        selected_head.pack_start(label(u"当前工程会话", "title-large", True, 0.5), False, False, 0)
        selected_head.pack_start(label(
            u"这里用于回看最初选择的 Project；当前任务、执行进度和日志不会被清空。",
            "muted", True, 0.5,
        ), False, False, 0)
        selected.pack_start(selected_head, False, False, 0)
        selected_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.workspace_selected_project_label = label(u"尚未选择工程", "title-medium", True)
        self.workspace_selected_path_label = label(u"—", "muted", True)
        self.workspace_selected_stage_label = label(u"当前进度：等待开始", "status-badge", True)
        selected_card.pack_start(self.workspace_selected_project_label, False, False, 0)
        selected_card.pack_start(self.workspace_selected_path_label, False, False, 0)
        selected_card.pack_start(self.workspace_selected_stage_label, False, False, 0)
        selected.pack_start(card(selected_card, "task-card", 16), False, False, 0)
        selected_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        selected_actions.set_halign(Gtk.Align.CENTER)
        continue_stage = button(u"返回当前进度", "go-next-symbolic", "primary")
        continue_stage.connect("clicked", self.on_continue_workspace_stage)
        change_project = button(u"更换工程", "folder-open-symbolic")
        change_project.connect("clicked", self.on_choose_workspace_project)
        start_fresh = button(u"结束当前会话并新建", "list-add-symbolic", "ghost")
        start_fresh.connect("clicked", self.on_new_workspace_task)
        selected_actions.pack_start(change_project, False, False, 0)
        selected_actions.pack_start(continue_stage, False, False, 0)
        selected_actions.pack_start(start_fresh, False, False, 0)
        selected.pack_start(selected_actions, False, False, 0)
        self.workspace_flow_stack.add_named(selected, "select")

        # Stage 2: honest, progressive project reading.  No report is shown
        # until all five sub-stages have completed.
        reading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        reading_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        reading_title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        reading_title.pack_start(label(u"AI 正在理解工程", "title-large"), False, False, 0)
        self.dashboard_project_name_label = label(u"正在发现 SWB 工程…", "muted", True)
        reading_title.pack_start(self.dashboard_project_name_label, False, False, 0)
        reading_head.pack_start(reading_title, True, True, 0)
        choose = button(u"更换工程", "folder-open-symbolic")
        choose.connect("clicked", self.on_choose_workspace_project)
        self.project_read_button = button(u"重新阅读", "view-refresh-symbolic")
        self.project_read_button.connect("clicked", self.on_read_workspace_project)
        reading_head.pack_end(self.project_read_button, False, False, 0)
        reading_head.pack_end(choose, False, False, 0)
        reading.pack_start(reading_head, False, False, 0)
        reading_status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        self.project_understanding_label = label(u"等待工程", "planned-pill")
        self.project_coverage_label = label(u"尚未开始", "small-muted", True)
        self.project_elapsed_label = label(u"", "small-muted")
        self.project_ai_spinner = Gtk.Spinner()
        reading_status.pack_start(self.project_understanding_label, False, False, 0)
        reading_status.pack_end(self.project_ai_spinner, False, False, 0)
        reading_status.pack_end(self.project_elapsed_label, False, False, 0)
        reading_status.pack_end(self.project_coverage_label, False, False, 0)
        reading.pack_start(reading_status, False, False, 0)
        self.project_coverage_bar = Gtk.ProgressBar()
        reading.pack_start(self.project_coverage_bar, False, False, 0)
        self.project_live_message_label = label(
            u"连接工程后，将依次完成清点、源码读取、深度分析和接手报告。",
            "reading-live", True,
        )
        reading.pack_start(self.project_live_message_label, False, False, 0)
        for key, heading, detail in (
            ("connect", u"连接并确认工程", u"核对工程路径、gtree.dat 和当前 SWB 状态。"),
            ("inventory", u"清点文件与节点", u"统计源文件、参数、节点和已有结果。"),
            ("source", u"读取 Tool 源文件", u"识别 SDE、SDevice、SProcess 与 Inspect 依赖。"),
            ("analysis", u"AI 深度分析", u"分析器件意图、参数关系、风险和未知项。"),
            ("report", u"生成工程接手报告", u"整理可验证结论并准备下一步输入。"),
        ):
            reading.pack_start(self.build_flow_progress_row(self.workspace_read_steps, key, heading, detail), False, False, 0)
        analysis_expander = Gtk.Expander(label=u"AI 分析过程（可审计摘要）")
        analysis_expander.set_expanded(True)
        self.project_analysis_buffer = Gtk.TextBuffer()
        analysis_view = WrappingTextView(buffer=self.project_analysis_buffer)
        analysis_view.set_editable(False)
        analysis_view.set_cursor_visible(False)
        analysis_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        analysis_scroll = scroller(analysis_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        analysis_scroll.set_size_request(-1, 150)
        analysis_expander.add(analysis_scroll)
        reading.pack_start(analysis_expander, False, False, 0)
        self.assistant_phase_label = label(u"等待选择工程", "planned-pill")
        reading_log = Gtk.Expander(label=u"查看实时详细记录")
        reading_log_view = self.build_chat_view()
        reading_log_view.set_size_request(-1, 175)
        reading_log.add(reading_log_view)
        reading.pack_start(reading_log, False, False, 0)
        self.workspace_flow_stack.add_named(reading, "reading")

        # Stage 3: completed understanding above, compact user composer below.
        goal_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        goal_page.pack_start(label(u"AI 已完成工程阅读", "title-large"), False, False, 0)
        goal_page.pack_start(label(u"先检查 AI 的理解边界，再描述希望完成的任务。", "muted", True), False, False, 0)
        self.project_report_label = label(u"等待工程读取。", "report-copy", True)
        self.project_report_label.set_selectable(True)
        self.project_report_label.set_yalign(0.0)
        self.project_report_buffer = LabelTextBuffer(self.project_report_label)
        goal_page.pack_start(card(self.project_report_label, "task-card", 14), False, False, 0)
        understanding_grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        understanding_grid.set_column_homogeneous(True)
        self.goal_device_label = label(u"等待分析", "muted", True)
        self.goal_toolchain_label = label(u"等待分析", "muted", True)
        self.goal_parameters_label = label(u"等待分析", "muted", True)
        self.goal_unknowns_label = label(u"等待分析", "muted", True)
        for index, (heading, content) in enumerate((
            (u"器件与目标", self.goal_device_label), (u"Tool 链与依赖", self.goal_toolchain_label),
            (u"关键参数与已有结果", self.goal_parameters_label), (u"风险与仍需确认", self.goal_unknowns_label),
        )):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.pack_start(label(heading, "small-muted"), False, False, 0)
            box.pack_start(content, False, False, 0)
            understanding_grid.attach(card(box, "understanding-card", 12), index % 2, index // 2, 1, 1)
        goal_page.pack_start(understanding_grid, False, False, 0)
        request_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        request_box.pack_start(label(u"你希望 EmberTCAD 完成什么？", "title-medium"), False, False, 0)
        request_box.pack_start(label(u"写清目标指标、可调参数、固定条件和验收方式；AI 会先给推荐方案。", "muted", True), False, False, 0)
        self.workspace_goal_buffer = Gtk.TextBuffer()
        self.workspace_goal_view = WrappingTextView(buffer=self.workspace_goal_buffer)
        self.workspace_goal_view.set_size_request(1, -1)
        self.workspace_goal_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        goal_scroll = scroller(self.workspace_goal_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        goal_scroll.set_size_request(-1, 118)
        request_box.pack_start(goal_scroll, False, False, 0)
        request_footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        request_footer.pack_start(label(u"提交后进入方案生成，不会立即修改或运行工程。", "small-muted", True), True, True, 0)
        self.workspace_plan_spinner = Gtk.Spinner()
        request_footer.pack_end(self.workspace_plan_spinner, False, False, 0)
        self.workspace_plan_button = button(u"生成推荐方案", "go-next-symbolic", "primary")
        self.workspace_plan_button.set_sensitive(False)
        self.workspace_plan_button.connect("clicked", self.on_create_workspace_task)
        request_footer.pack_end(self.workspace_plan_button, False, False, 0)
        request_box.pack_start(request_footer, False, False, 0)
        request_frame = card(request_box, "composer-centered", 13)
        self.workspace_request_frame = request_frame
        request_alignment = Gtk.Alignment.new(0.5, 0.5, 0.76, 0.0)
        request_alignment.add(request_frame)
        self.workspace_request_revealer = Gtk.Revealer()
        self.workspace_request_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.workspace_request_revealer.add(request_alignment)
        self.workspace_request_revealer.set_reveal_child(False)
        goal_page.pack_start(self.workspace_request_revealer, False, False, 0)
        self.workspace_flow_stack.add_named(goal_page, "goal")

        # Stage 4: one recommended plan, first as honest generation progress,
        # then as an editable review surface.
        self.workspace_plan_mode_stack = Gtk.Stack()
        self.workspace_plan_mode_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        planning_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        planning_page.pack_start(label(u"正在生成推荐方案", "title-large"), False, False, 0)
        planning_page.pack_start(label(u"EmberTCAD 正在把目标转换为可审核、可执行、可停止的 TCAD 任务。", "muted", True), False, False, 0)
        for key, heading, detail in (
            ("goal", u"解析目标与验收条件", u"识别目标指标、变量、固定条件和成功标准。"),
            ("evidence", u"检查证据与工程上下文", u"核对工程理解、Manual、Tutorial 和必要的研究依据。"),
            ("parameters", u"生成可调整参数", u"将目标值、容差、方法和固定条件转换为表单。"),
            ("route", u"规划执行路径", u"确定节点依赖、结果提取和停止条件。"),
            ("risk", u"完成风险审查", u"确认写入、运行、产物和失败边界。"),
        ):
            planning_page.pack_start(self.build_flow_progress_row(self.workspace_plan_steps, key, heading, detail), False, False, 0)
        plan_log = Gtk.Expander(label=u"查看方案生成记录")
        plan_log_view = self.build_chat_view()
        plan_log_view.set_size_request(-1, 150)
        plan_log.add(plan_log_view)
        planning_page.pack_start(plan_log, False, False, 0)
        self.workspace_plan_mode_stack.add_named(planning_page, "planning")

        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        review_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.workspace_detail_title_label = label(u"AI 推荐方案", "title-large", True)
        self.workspace_detail_status_label = label(u"推荐", "ready-pill")
        review_head.pack_start(self.workspace_detail_title_label, True, True, 0)
        review_head.pack_end(self.workspace_detail_status_label, False, False, 0)
        review.pack_start(review_head, False, False, 0)
        self.workspace_detail_progress = Gtk.ProgressBar()
        review.pack_start(self.workspace_detail_progress, False, False, 0)
        review_body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        self.workspace_detail_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        detail_scroll = scroller(self.workspace_detail_cards, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scroll.set_min_content_height(300)
        review_body.pack_start(detail_scroll, True, True, 0)
        inputs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inputs.pack_start(label(u"执行参数", "title-medium"), False, False, 0)
        inputs.pack_start(label(
            u"这里只显示真正需要你决定的物理条件。源文件、节点状态和报错日志由 EmberTCAD 自动读取。",
            "small-muted", True,
        ), False, False, 0)
        self.workspace_plan_inputs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        inputs.pack_start(self.workspace_plan_inputs_box, False, False, 0)
        inputs.pack_start(label(u"停止条件\n✓ 达到目标\n✓ 用户点击停止\n✓ 不可恢复错误\n✓ 无可行新候选点", "muted", True), False, False, 0)
        review_body.pack_start(card(inputs, "plan-inputs", 12), False, False, 0)
        review.pack_start(review_body, True, True, 0)
        review_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_goal = button(u"返回修改需求", "go-previous-symbolic")
        back_goal.connect("clicked", lambda *_args: self.set_workspace_stage("goal"))
        regenerate = button(u"重新生成方案", "view-refresh-symbolic")
        regenerate.connect("clicked", self.on_regenerate_workspace_plan)
        self.workspace_specialist_button = button(u"查看高级详情", "document-open-symbolic")
        self.workspace_specialist_button.set_sensitive(False)
        self.workspace_specialist_button.connect("clicked", self.open_selected_workspace_task)
        self.workspace_approve_button = button(u"确认并开始执行", "media-playback-start-symbolic", "primary")
        self.workspace_approve_button.set_sensitive(False)
        self.workspace_approve_button.connect("clicked", self.on_approve_workspace_task)
        self.conversation_approve_button = self.workspace_approve_button
        review_actions.pack_start(back_goal, False, False, 0)
        review_actions.pack_start(regenerate, False, False, 0)
        review_actions.pack_end(self.workspace_approve_button, False, False, 0)
        review.pack_start(review_actions, False, False, 0)
        self.workspace_plan_mode_stack.add_named(review, "review")
        self.workspace_flow_stack.add_named(self.workspace_plan_mode_stack, "plan")

        # Stage 5: one foreground execution with an explicit hard-stop control.
        execute = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        execute_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        execute_title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        execution_title = label(u"执行与验证", "title-large", True)
        execution_detail = label(u"等待用户确认方案。", "muted", True)
        execute_title_box.pack_start(execution_title, False, False, 0)
        execute_title_box.pack_start(execution_detail, False, False, 0)
        execute_head.pack_start(execute_title_box, True, True, 0)
        execution_state = label(u"等待执行", "planned-pill")
        execute_head.pack_end(execution_state, False, False, 0)
        self.workspace_stop_button = button(u"停止运行", "media-playback-stop-symbolic", "danger")
        self.workspace_stop_button.set_sensitive(False)
        self.workspace_stop_button.connect("clicked", self.on_stop_workspace_execution)
        execute_head.pack_end(self.workspace_stop_button, False, False, 0)
        execute.pack_start(execute_head, False, False, 0)
        execution_progress = Gtk.ProgressBar()
        execution_progress.set_pulse_step(0.06)
        execution_progress_text = label(u"尚未开始", "small-muted")
        execute.pack_start(execution_progress, False, False, 0)
        execute.pack_start(execution_progress_text, False, False, 0)
        self.task_title_labels.append(execution_title)
        self.task_detail_labels.append(execution_detail)
        self.task_state_labels.append(execution_state)
        self.task_progress_bars.append(execution_progress)
        self.task_progress_text_labels.append(execution_progress_text)
        phase_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        self.execution_phase_widgets = {}
        for key, title in (("materialize", u"创建分支"), ("sde", "SDE"), ("sdevice", "SDevice"), ("extract", u"提取指标"), ("evaluate", u"评估")):
            phase_box.pack_start(self.build_execution_phase_cell(self.execution_phase_widgets, key, title), True, True, 0)
        execute.pack_start(phase_box, False, False, 0)
        iteration_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        iteration_head.pack_start(label(u"已完成的迭代", "title-medium"), True, True, 0)
        self.execution_iteration_label = label(u"第 0 次迭代 · 无固定次数上限", "ready-pill")
        iteration_head.pack_end(self.execution_iteration_label, False, False, 0)
        execute.pack_start(iteration_head, False, False, 0)
        iteration_view = self.build_iteration_view(False)
        iteration_scroll = scroller(iteration_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        iteration_scroll.set_min_content_height(210)
        execute.pack_start(card(iteration_scroll, "surface", 0), False, False, 0)
        execution_log = Gtk.Expander(label=u"查看实时执行记录")
        execution_log_view = self.build_chat_view()
        execution_log_view.set_size_request(-1, 180)
        execution_log.add(execution_log_view)
        execute.pack_start(execution_log, False, False, 0)
        self.workspace_flow_stack.add_named(execute, "execute")

        complete = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        complete.set_valign(Gtk.Align.CENTER)
        complete_mark = logo_image(72)
        complete_mark.set_halign(Gtk.Align.CENTER)
        complete.pack_start(complete_mark, False, False, 0)
        self.workspace_complete_title = label(u"任务完成", "welcome-title", True, 0.5)
        self.workspace_complete_summary = label(u"执行结果将在这里汇总。", "welcome-subtitle", True, 0.5)
        complete.pack_start(self.workspace_complete_title, False, False, 0)
        complete.pack_start(self.workspace_complete_summary, False, False, 0)
        self.workspace_complete_details = label(u"", "report-copy", True)
        complete.pack_start(card(self.workspace_complete_details, "task-card", 16), False, False, 0)
        complete_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        complete_actions.set_halign(Gtk.Align.CENTER)
        history_button = button(u"在任务历史中查看", "document-open-recent-symbolic", "primary")
        history_button.connect("clicked", lambda *_args: self.show_page("history"))
        self.workspace_report_button = button(u"生成 / 更新报告", "document-save-symbolic")
        self.workspace_report_button.set_sensitive(False)
        self.workspace_report_button.connect("clicked", self.on_generate_workspace_report)
        self.workspace_complete_advanced_button = button(u"查看代码与证据详情", "document-open-symbolic")
        self.workspace_complete_advanced_button.set_sensitive(False)
        self.workspace_complete_advanced_button.connect("clicked", self.open_selected_workspace_task)
        self.workspace_resume_button = button(u"恢复并继续", "media-playback-start-symbolic")
        self.workspace_resume_button.set_sensitive(False)
        self.workspace_resume_button.connect("clicked", self.on_resume_workspace_task)
        another = button(u"新建任务", "list-add-symbolic")
        another.connect("clicked", self.on_new_workspace_task)
        complete_actions.pack_start(history_button, False, False, 0)
        complete_actions.pack_start(self.workspace_report_button, False, False, 0)
        complete_actions.pack_start(self.workspace_resume_button, False, False, 0)
        complete_actions.pack_start(another, False, False, 0)
        complete.pack_start(complete_actions, False, False, 0)
        report_state = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        report_state.set_halign(Gtk.Align.CENTER)
        self.workspace_report_spinner = Gtk.Spinner()
        self.workspace_report_status_label = label(u"任务记录保存后即可生成报告。", "small-muted", True, 0.5)
        report_state.pack_start(self.workspace_report_spinner, False, False, 0)
        report_state.pack_start(self.workspace_report_status_label, False, False, 0)
        complete.pack_start(report_state, False, False, 0)
        self.workspace_flow_stack.add_named(complete, "complete")

        root.pack_start(self.workspace_flow_stack, True, True, 0)
        self.workspace_flow_stack.set_visible_child_name("reading")
        # Compatibility stores remain off-screen; task switching and completed
        # reports now live in Task History instead of cluttering this flow.
        self.workspace_task_view = Gtk.TreeView(model=self.workspace_task_store)
        self.workspace_task_view.get_selection().connect("changed", self.on_workspace_task_selected)
        self.workspace_tasks_revealer = Gtk.Revealer()
        self.workspace_tasks_revealer.add(Gtk.Box())
        self.dashboard_state_stack.add_named(root, "session")
        self.dashboard_state_stack.set_visible_child_name("welcome")
        self.dashboard_page_scroll = self.page_container(self.dashboard_state_stack)
        return self.dashboard_page_scroll

    def on_workspace_stage_clicked(self, _button, stage_id):
        target = next((item["index"] for item in self.workspace_step_widgets if item["id"] == stage_id), 99)
        unlocked = getattr(self, "workspace_unlocked_index", 1)
        if target <= unlocked:
            self.set_workspace_stage(stage_id)

    def update_workspace_selection_summary(self):
        project = as_text(self.active_project or u"尚未选择工程")
        if hasattr(self, "workspace_selected_project_label"):
            self.workspace_selected_project_label.set_text(project)
            try:
                path = self.project_absolute_path()
            except Exception:
                path = os.path.join(as_text(self.workspace_root or u""), project.replace("/", os.sep))
            self.workspace_selected_path_label.set_text(as_text(path or u"—"))
            names = {
                "select": u"选择工程", "reading": u"AI 理解", "goal": u"描述目标",
                "plan": u"审查方案", "execute": u"执行验证", "complete": u"完成",
            }
            task = self.current_workspace_task or {}
            detail = as_text(task.get("stage") or task.get("summary") or u"")
            current = names.get(getattr(self, "workspace_flow_stage", "reading"), u"AI 理解")
            self.workspace_selected_stage_label.set_text(
                u"当前进度：%s%s" % (current, (u" · " + detail[:100]) if detail else u"")
            )

    def on_continue_workspace_stage(self, *_args):
        stage = getattr(self, "workspace_flow_stage", "reading")
        if stage == "select":
            stage = "reading"
        self.set_workspace_stage(stage)

    def set_workspace_stage(self, stage_id, unlock=False):
        indexes = {"select": 0, "reading": 1, "goal": 2, "plan": 3, "execute": 4, "complete": 5}
        if stage_id == "select" and not self.project_session_started:
            self.dashboard_state_stack.set_visible_child_name("welcome")
            self.workspace_flow_stage = "select"
            self.workspace_view_stage = "select"
            return
        target = indexes.get(stage_id, 1)
        if unlock:
            self.workspace_unlocked_index = max(getattr(self, "workspace_unlocked_index", 1), target)
            self.workspace_flow_stage = stage_id
        self.workspace_view_stage = stage_id
        self.dashboard_state_stack.set_visible_child_name("session")
        if stage_id == "select":
            self.update_workspace_selection_summary()
        self.workspace_flow_stack.set_visible_child_name(stage_id)
        active = indexes.get(getattr(self, "workspace_flow_stage", "reading"), 1)
        for item in self.workspace_step_widgets:
            context = item["button"].get_style_context()
            context.remove_class("active")
            context.remove_class("complete")
            context.remove_class("pending")
            context.remove_class("failed")
            context.remove_class("viewing")
            if item["index"] < active:
                context.add_class("complete")
            elif item["index"] == active:
                context.add_class("active")
            else:
                context.add_class("pending")
            if item["index"] == target and target != active:
                context.add_class("viewing")
            item["button"].set_sensitive(item["index"] <= getattr(self, "workspace_unlocked_index", 1))
        if hasattr(self, "dashboard_page_scroll"):
            self.dashboard_page_scroll.get_vadjustment().set_value(0.0)

    def mark_workspace_stage_failed(self, stage_id):
        for item in self.workspace_step_widgets:
            if item["id"] == stage_id:
                context = item["button"].get_style_context()
                context.remove_class("active")
                context.remove_class("complete")
                context.add_class("failed")
                break

    @staticmethod
    def set_flow_progress(store, key, fraction=None, status=None, detail=None, pulse=False):
        row = store.get(key)
        if not row:
            return
        if pulse:
            row["bar"].pulse()
        elif fraction is not None:
            row["bar"].set_fraction(max(0.0, min(1.0, float(fraction))))
        if status is not None:
            row["status"].set_text(as_text(status))
        if detail is not None:
            row["detail"].set_text(as_text(detail))

    def append_project_analysis(self, heading, message):
        if not hasattr(self, "project_analysis_buffer"):
            return
        end = self.project_analysis_buffer.get_end_iter()
        prefix = u"\n\n" if self.project_analysis_buffer.get_char_count() else u""
        self.project_analysis_buffer.insert(end, prefix + as_text(heading) + u"\n" + as_text(message))

    def reset_flow_progress(self, store):
        for row in store.values():
            row["bar"].set_fraction(0.0)
            row["status"].set_text(u"等待")

    def populate_understanding_summary(self, report):
        analysis = report.get("aiAnalysis") or {}
        self.project_report_buffer.set_text(as_text(
            analysis.get("overview") or report.get("summary") or u"AI 已完成工程阅读。"
        ))
        self.goal_device_label.set_text(as_text(
            analysis.get("deviceIntent") or u"尚未从源文件中确认明确的器件目标。"
        ))
        self.goal_toolchain_label.set_text(u"\n".join(
            [u"• " + as_text(value) for value in analysis.get("toolchain") or []]
        ) or u"尚未识别完整 Tool 链。")
        parameter_rows = [u"• " + as_text(value) for value in analysis.get("parameters") or []]
        result_rows = [u"• " + as_text(value) for value in analysis.get("existingResults") or []]
        self.goal_parameters_label.set_text(u"\n".join(parameter_rows + result_rows) or u"未确认可复用结果或关键参数。")
        risks = list(analysis.get("risks") or []) + list(analysis.get("unknowns") or [])
        self.goal_unknowns_label.set_text(u"\n".join([u"• " + as_text(value) for value in risks]) or u"未发现需要立即阻断的未知项。")

    def render_workspace_plan_inputs(self, task):
        for child in self.workspace_plan_inputs_box.get_children():
            self.workspace_plan_inputs_box.remove(child)
        self.workspace_plan_input_widgets = {}
        approved = task.get("approvedInputs") or {}
        for item in task.get("planInputs") or []:
            key = as_text(item.get("key") or u"")
            if not key:
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            heading = as_text(item.get("label") or key)
            unit = as_text(item.get("unit") or u"")
            row.pack_start(label(heading + ((u" · " + unit) if unit else u""), "small-muted", True), False, False, 0)
            value = approved.get(key, item.get("value"))
            if item.get("type") == "select" and item.get("options"):
                widget = Gtk.ComboBoxText()
                options = [as_text(option) for option in item.get("options") or []]
                for option in options:
                    widget.append_text(option)
                widget.set_active(options.index(as_text(value)) if as_text(value) in options else 0)
            else:
                widget = Gtk.Entry()
                widget.set_text(as_text(value))
                if item.get("type") == "number" and hasattr(widget, "set_input_purpose"):
                    widget.set_input_purpose(Gtk.InputPurpose.NUMBER)
            row.pack_start(widget, False, False, 0)
            if item.get("description"):
                row.pack_start(label(item.get("description"), "small-muted", True), False, False, 0)
            self.workspace_plan_inputs_box.pack_start(row, False, False, 0)
            self.workspace_plan_input_widgets[key] = (item, widget)
        if not self.workspace_plan_input_widgets:
            self.workspace_plan_inputs_box.pack_start(label(u"本任务没有需要用户调整的执行参数。", "muted", True), False, False, 0)
        self.workspace_plan_inputs_box.show_all()

    def collect_workspace_plan_inputs(self):
        values = {}
        for key, (item, widget) in self.workspace_plan_input_widgets.items():
            if isinstance(widget, Gtk.ComboBoxText):
                value = widget.get_active_text() or u""
            else:
                value = widget.get_text().strip()
            values[key] = as_text(value)
        return values

    def on_regenerate_workspace_plan(self, *_args):
        if self.workspace_busy:
            return
        self.workspace_plan_mode_stack.set_visible_child_name("planning")
        self.reset_flow_progress(self.workspace_plan_steps)
        self.set_workspace_stage("plan", unlock=True)
        self.on_create_workspace_task()

    def on_stop_workspace_execution(self, *_args):
        if self.execution_stopping or not self.current_workspace_execution_id or not self.current_run_id:
            return
        self.execution_stopping = True
        self.workspace_stop_button.set_sensitive(False)
        self.set_task_state(u"正在强制停止", "error")
        self.set_task_detail(u"正在终止当前 AI Helper 与 gsub 进程组；未完成产物不会进入结果。")
        self.set_task_progress(text=u"STOPPING", pulse=True)
        process = self.active_ai_process
        start_thread(self.stop_workspace_execution_worker, (
            self.current_workspace_execution_id, self.current_run_id, process,
        ))

    def stop_workspace_execution_worker(self, task_id, run_id, helper_process=None):
        messages = []
        try:
            core.research_rpc({
                "action": "update-workspace-execution", "taskId": task_id, "runId": run_id,
                "currentPhase": "stopping", "message": u"用户请求立即停止；开始终止 Helper 和 gsub 进程组。",
            })
        except Exception as error:
            messages.append(u"停止状态记录：%s" % error_text(error))
        if helper_process is not None:
            try:
                helper_process.terminate()
                deadline = time.time() + 2.0
                while helper_process.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                if helper_process.poll() is None:
                    helper_process.kill()
                messages.append(u"AI Helper 已终止。")
            except Exception as error:
                messages.append(u"AI Helper 停止检查：%s" % error_text(error))
        try:
            result = core.live_action("stop-run", self.project_absolute_path(), run_id=run_id)
            if result.get("stopped"):
                messages.append(u"已终止 gsub 进程组 %s。" % as_text(result.get("pgid") or result.get("pid")))
            else:
                messages.append(u"当前没有仍在运行的 gsub 节点。")
        except Exception as error:
            messages.append(u"节点停止检查：%s" % error_text(error))
        try:
            result = core.research_rpc({
                "action": "cancel-workspace-task", "taskId": task_id, "runId": run_id,
                "message": u"用户已强制停止运行。" + (u" ".join(messages)),
            })
            GLib.idle_add(self.apply_workspace_execution_stopped, result.get("task") or {}, u" ".join(messages))
        except Exception as error:
            GLib.idle_add(self.apply_workspace_plan_error, error_text(error))

    def apply_workspace_execution_stopped(self, task, message):
        self.execution_stopping = False
        self.ai_busy = False
        self.active_ai_process = None
        self.current_workspace_task = task
        self.current_workspace_execution_id = None
        self.current_run_id = None
        self.workspace_complete_title.set_text(u"任务已停止")
        self.workspace_complete_summary.set_text(u"运行已由用户强制终止；此前完成的有效结果已经保留。")
        self.workspace_complete_details.set_text(as_text(message or task.get("summary") or u"未完成产物已标记为无效。"))
        self.workspace_report_button.set_sensitive(bool(task.get("taskId")))
        self.workspace_report_spinner.stop()
        self.workspace_report_status_label.set_text(u"停止记录已保存，可以生成完整报告。")
        self.workspace_resume_button.set_sensitive(True)
        self.set_workspace_stage("complete", unlock=True)
        self.on_refresh_workspace_tasks()
        return False

    def on_resume_workspace_task(self, *_args):
        task = self.current_workspace_task or {}
        if self.workspace_busy or task.get("status") not in ("cancelled", "failed"):
            return
        self.workspace_busy = True
        self.workspace_resume_button.set_sensitive(False)
        start_thread(self.resume_workspace_task_worker, (task.get("taskId"),))

    def on_generate_workspace_report(self, *_args):
        task = self.current_workspace_task or {}
        if not task.get("taskId"):
            self.show_message(u"当前任务记录尚未保存，暂时不能生成报告。", Gtk.MessageType.WARNING)
            return
        self.workspace_report_button.set_sensitive(False)
        self.workspace_report_spinner.start()
        self.workspace_report_status_label.set_text(u"正在汇总工程理解、方案、执行事件、日志与结果文件…")
        start_thread(self.generate_workspace_report_worker, (task.get("taskId"),))

    def generate_workspace_report_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "report", "taskId": task_id})
            GLib.idle_add(self.apply_workspace_report, result)
        except Exception as error:
            GLib.idle_add(self.apply_workspace_report_error, error_text(error))

    def apply_workspace_report(self, result):
        self.workspace_report_button.set_sensitive(True)
        self.workspace_report_spinner.stop()
        path = as_text(result.get("path") or u"—")
        if self.current_workspace_task is not None:
            self.current_workspace_task["reportPath"] = path
        self.workspace_report_status_label.set_text(u"报告已保存：%s" % path)
        current = as_text(self.workspace_complete_details.get_text())
        self.workspace_complete_details.set_text((as_text(current) + u"\n\n报告已生成：" + as_text(path)).strip())
        self.show_preview(path, result.get("content") or u"报告已生成。")
        self.on_refresh_workspace_tasks()
        return False

    def apply_workspace_report_error(self, message):
        self.workspace_report_button.set_sensitive(True)
        self.workspace_report_spinner.stop()
        self.workspace_report_status_label.set_text(u"报告生成失败：%s" % as_text(message))
        self.show_message(u"报告生成失败：%s" % as_text(message), Gtk.MessageType.ERROR)
        return False

    def resume_workspace_task_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "resume-workspace-task", "taskId": task_id})
            GLib.idle_add(self.apply_workspace_task_approval, result.get("task") or {})
        except Exception as error:
            GLib.idle_add(self.apply_workspace_plan_error, error_text(error))

    def build_metric(self, heading, collection, caption):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(label(heading, "small-muted"), False, False, 0)
        value = label(u"—", "title-medium", True)
        collection.append(value)
        box.pack_start(value, False, False, 0)
        box.pack_start(label(caption, "small-muted"), False, False, 0)
        return card(box, "metric-card", 13)

    def build_feature_entry(self, heading, description, page):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.pack_start(label(heading, "title-medium"), True, True, 0)
        top.pack_end(label(u"可用", "ready-pill"), False, False, 0)
        box.pack_start(top, False, False, 0)
        box.pack_start(label(description, "muted", True), False, False, 0)
        open_button = button(u"打开", "go-next-symbolic", "ghost")
        open_button.set_halign(Gtk.Align.START)
        open_button.connect("clicked", lambda _button, name=page: self.show_page(name))
        box.pack_end(open_button, False, False, 0)
        return card(box, "placeholder-card", 13)

    def build_iteration_view(self, compact=False):
        view = Gtk.TreeView(model=self.iteration_store)
        view.set_headers_visible(True)
        view.set_rules_hint(False)
        columns = [
            ("#", 0, 34),
            ("P-well / cm⁻³", 1, 125),
            ("SDE", 2, 70),
            ("SDevice", 3, 80),
            ("Vth / V", 4, 88),
            ("状态", 5, 88),
        ]
        if not compact:
            columns.append(("用时", 6, 72))
        for title, index, width in columns:
            renderer = Gtk.CellRendererText()
            renderer.set_property("xpad", 7)
            if index == 4:
                renderer.set_property("foreground", "#1d4ed8")
                renderer.set_property("weight", Pango.Weight.BOLD)
            if index == 5:
                renderer.set_property("foreground", "#047857")
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_min_width(width)
            column.set_resizable(True)
            if index == 1:
                column.set_expand(True)
            view.append_column(column)
        return view

    def build_project_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        titles.pack_start(label(u"SWB 工程", "title-large"), False, False, 0)
        titles.pack_start(label(u"查看真实文件、参数、实验分支和节点状态。", "muted"), False, False, 0)
        heading.pack_start(titles, True, True, 0)
        refresh_button = button(u"双向刷新", "view-refresh-symbolic", "primary")
        refresh_button.connect("clicked", self.on_refresh_all)
        heading.pack_end(refresh_button, False, False, 0)
        root.pack_start(heading, False, False, 0)

        self.system_label = label(u"Connector：检测中", "muted")
        self.workspace_label = label(u"工作区：—", "muted")
        self.change_label = label(u"最近同步：—", "muted")
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        status_box.pack_start(self.system_label, False, False, 0)
        status_box.pack_start(self.workspace_label, True, True, 0)
        status_box.pack_end(self.change_label, False, False, 0)
        root.pack_start(card(status_box, "surface", 11), False, False, 0)

        notebook = Gtk.Notebook()
        notebook.append_page(self.build_files_tab(), label(u"工程文件"))
        notebook.append_page(self.build_parameters_tab(), label(u"参数"))
        notebook.append_page(self.build_nodes_tab(), label(u"节点"))
        root.pack_start(card(notebook, "surface", 8), True, True, 0)
        return self.page_container(root)

    def build_files_tab(self):
        self.file_view = Gtk.TreeView(model=self.file_store)
        self.file_view.set_headers_visible(False)
        self.file_view.connect("row-activated", self.on_file_activated)
        icon_renderer = Gtk.CellRendererText()
        icon_renderer.set_property("foreground", "#2563eb")
        icon_renderer.set_property("weight", Pango.Weight.BOLD)
        name_renderer = Gtk.CellRendererText()
        name_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        time_renderer = Gtk.CellRendererText()
        time_renderer.set_property("foreground", "#7a8493")
        self.file_view.append_column(Gtk.TreeViewColumn("", icon_renderer, text=0))
        name_column = Gtk.TreeViewColumn("", name_renderer, text=1)
        name_column.set_expand(True)
        self.file_view.append_column(name_column)
        self.file_view.append_column(Gtk.TreeViewColumn("", time_renderer, text=2))
        return scroller(self.file_view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

    def build_parameters_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.param_view = Gtk.TreeView(model=self.param_store)
        self.param_view.get_selection().connect("changed", self.on_parameter_selected)
        for title, index in ((u"参数", 0), (u"默认值", 1), (u"实验取值", 2), (u"步骤", 3)):
            renderer = Gtk.CellRendererText()
            if index == 0:
                renderer.set_property("foreground", "#1d4ed8")
                renderer.set_property("weight", Pango.Weight.BOLD)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            if index == 2:
                column.set_expand(True)
            self.param_view.append_column(column)
        param_scroll = scroller(self.param_view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        param_scroll.set_min_content_height(260)
        box.pack_start(param_scroll, True, True, 0)

        fields = Gtk.Grid(column_spacing=8, row_spacing=8)
        self.param_name_entry = Gtk.Entry()
        self.param_name_entry.set_text("con_pwell")
        self.param_step_entry = Gtk.Entry()
        self.param_step_entry.set_width_chars(5)
        self.param_step_entry.set_text("2")
        self.param_value_entry = Gtk.Entry()
        self.param_value_entry.set_text("1e18")
        self.param_value_entry.connect("activate", lambda *_args: self.on_parameter_action("set-parameter"))
        fields.attach(label(u"名称"), 0, 0, 1, 1)
        fields.attach(self.param_name_entry, 1, 0, 1, 1)
        fields.attach(label(u"步骤"), 2, 0, 1, 1)
        fields.attach(self.param_step_entry, 3, 0, 1, 1)
        fields.attach(label(u"取值"), 0, 1, 1, 1)
        fields.attach(self.param_value_entry, 1, 1, 3, 1)
        fields.set_column_homogeneous(False)
        self.param_name_entry.set_hexpand(True)
        box.pack_start(fields, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        replace_button = button(u"替换现有值")
        replace_button.connect("clicked", lambda *_args: self.on_parameter_action("set-parameter"))
        add_values_button = button(u"添加实验取值", "list-add-symbolic", "primary")
        add_values_button.connect("clicked", lambda *_args: self.on_parameter_action("add-values"))
        add_param_button = button(u"新增参数")
        add_param_button.connect("clicked", lambda *_args: self.on_parameter_action("add-parameter"))
        actions.pack_start(replace_button, False, False, 0)
        actions.pack_start(add_values_button, False, False, 0)
        actions.pack_start(add_param_button, False, False, 0)
        box.pack_start(actions, False, False, 0)
        return set_margins(box, 10, 10, 10, 10)

    def build_nodes_tab(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        self.node_view = Gtk.TreeView(model=self.node_store)
        self.node_view.get_selection().connect("changed", self.on_node_selected)
        self.node_view.connect("row-activated", lambda *_args: self.on_run_node())
        for title, index in ((u"节点", 0), (u"工具", 1), (u"状态", 2), (u"参数值", 3)):
            renderer = Gtk.CellRendererText()
            if index == 2:
                renderer.set_property("foreground", "#047857")
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            if index == 3:
                column.set_expand(True)
            self.node_view.append_column(column)
        node_scroll = scroller(self.node_view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        node_scroll.set_min_content_height(310)
        box.pack_start(node_scroll, True, True, 0)

        run_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.selected_node_label = label(u"尚未选择节点", "muted")
        self.run_node_entry = Gtk.Entry()
        self.run_node_entry.set_width_chars(6)
        self.run_node_entry.connect("activate", self.on_run_node)
        run_button = button(u"运行节点", "media-playback-start-symbolic", "primary")
        run_button.connect("clicked", self.on_run_node)
        run_row.pack_start(self.selected_node_label, True, True, 0)
        run_row.pack_start(label(u"节点号"), False, False, 0)
        run_row.pack_start(self.run_node_entry, False, False, 0)
        run_row.pack_end(run_button, False, False, 0)
        box.pack_start(run_row, False, False, 0)
        return set_margins(box, 10, 10, 10, 10)

    def build_history_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.pack_start(label(u"工程档案与任务历史", "title-large"), False, False, 0)
        title_box.pack_start(label(u"每个工程只显示一条档案；进入工程后可查看它的全部阅读、方案、执行、停止和报告记录。", "muted", True), False, False, 0)
        root.pack_start(title_box, False, False, 0)

        self.history_page_stack = Gtk.Stack()
        self.history_page_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.history_page_stack.set_transition_duration(180)

        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        refresh = button(u"刷新", "view-refresh-symbolic")
        refresh.connect("clicked", self.on_refresh_history)
        self.history_filter_combo = Gtk.ComboBoxText()
        for key, title in (("all", u"全部状态"), ("active", u"进行中"), ("pending", u"待处理"),
                           ("completed", u"已完成"), ("interrupted", u"已中断"),
                           ("cancelled", u"已停止"), ("failed", u"失败")):
            self.history_filter_combo.append(key, title)
        self.history_filter_combo.set_active_id("all")
        self.history_filter_combo.connect("changed", self.on_history_filter_changed)
        self.history_clear_button = button(u"清空历史", "edit-delete-symbolic", "danger")
        self.history_clear_button.connect("clicked", self.on_clear_history)
        open_project = button(u"查看工程档案", "document-open-symbolic", "primary")
        open_project.connect("clicked", self.on_open_history_project)
        header.pack_end(open_project, False, False, 0)
        header.pack_end(refresh, False, False, 0)
        header.pack_end(self.history_filter_combo, False, False, 0)
        header.pack_start(self.history_clear_button, False, False, 0)
        overview.pack_start(header, False, False, 0)

        self.history_status_label = label(u"正在读取本机任务库…", "small-muted", True)
        overview.pack_start(self.history_status_label, False, False, 0)
        view = Gtk.TreeView(model=self.history_store)
        view.set_headers_visible(True)
        view.get_selection().connect("changed", self.on_history_project_selected)
        view.connect("row-activated", lambda *_args: self.on_open_history_project())
        for title, column, width in ((u"工程", 0, 150), (u"状态", 1, 80), (u"最近目标", 2, 230), (u"记录", 3, 120), (u"最后更新", 4, 105)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            item = Gtk.TreeViewColumn(title, renderer, text=column)
            item.set_min_width(width)
            if column == 2:
                item.set_expand(True)
            view.append_column(item)
        history_scroll = scroller(view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        history_scroll.set_min_content_height(420)
        overview.pack_start(card(history_scroll, "surface", 0), True, True, 0)

        hint = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hint.pack_start(Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        hint.pack_start(label(u"历史记录保存在 EmberTCAD 本机数据目录。只有报告列出真实 SWB 节点和结果文件时，任务才具有数值结论。", "muted", True), True, True, 0)
        overview.pack_start(card(hint, "task-card", 12), False, False, 0)
        self.history_page_stack.add_named(overview, "projects")

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        detail_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        back = button(u"返回工程列表", "go-previous-symbolic")
        back.connect("clicked", self.on_close_history_project)
        detail_head.pack_start(back, False, False, 0)
        self.history_project_title_label = label(u"工程档案", "title-medium", True)
        detail_head.pack_start(self.history_project_title_label, True, True, 0)
        open_task = button(u"查看任务详情", "document-open-symbolic", "primary")
        open_task.connect("clicked", self.on_open_history_task)
        detail_head.pack_end(open_task, False, False, 0)
        detail.pack_start(detail_head, False, False, 0)
        self.history_detail_status_label = label(u"选择一条记录，在任务历史内查看完整详情与报告。", "small-muted", True)
        detail.pack_start(self.history_detail_status_label, False, False, 0)
        detail_view = Gtk.TreeView(model=self.history_detail_store)
        detail_view.set_headers_visible(True)
        detail_view.get_selection().connect("changed", self.on_history_selected)
        detail_view.connect("row-activated", lambda *_args: self.on_open_history_task())
        for title, column, width in ((u"时间", 0, 105), (u"类型", 1, 90), (u"状态", 2, 90), (u"目标与结论", 3, 280), (u"报告", 4, 100)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            item = Gtk.TreeViewColumn(title, renderer, text=column)
            item.set_min_width(width)
            if column == 3:
                item.set_expand(True)
            detail_view.append_column(item)
        detail_scroll = scroller(detail_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scroll.set_min_content_height(420)
        detail.pack_start(card(detail_scroll, "surface", 0), True, True, 0)
        self.history_storage_label = label(u"正在确认本机保存位置…", "terminal-label", True)
        detail.pack_start(self.history_storage_label, False, False, 0)
        self.history_page_stack.add_named(detail, "detail")

        task_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        task_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        task_back = button(u"返回任务列表", "go-previous-symbolic")
        task_back.connect("clicked", self.on_close_history_task)
        task_head.pack_start(task_back, False, False, 0)
        self.history_task_title_label = label(u"任务详情", "title-medium", True)
        task_head.pack_start(self.history_task_title_label, True, True, 0)
        self.history_task_continue_button = button(u"继续此任务", "go-next-symbolic")
        self.history_task_continue_button.connect("clicked", self.on_continue_history_task)
        task_head.pack_end(self.history_task_continue_button, False, False, 0)
        self.history_task_report_button = button(u"生成 / 更新报告", "document-save-symbolic", "primary")
        self.history_task_report_button.connect("clicked", self.on_generate_history_report)
        task_head.pack_end(self.history_task_report_button, False, False, 0)
        task_page.pack_start(task_head, False, False, 0)
        self.history_task_status_label = label(u"正在读取完整任务记录…", "small-muted", True)
        task_page.pack_start(self.history_task_status_label, False, False, 0)

        task_tabs = Gtk.Notebook()
        task_tabs.set_hexpand(True)
        task_tabs.set_vexpand(True)
        self.history_task_buffer = Gtk.TextBuffer()
        task_view = Gtk.TextView(buffer=self.history_task_buffer)
        task_view.set_editable(False)
        task_view.set_cursor_visible(False)
        task_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        task_view.set_left_margin(14)
        task_view.set_right_margin(14)
        task_tabs.append_page(scroller(task_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC), label(u"任务详情"))
        self.history_report_buffer = Gtk.TextBuffer()
        report_view = Gtk.TextView(buffer=self.history_report_buffer)
        report_view.set_editable(False)
        report_view.set_cursor_visible(False)
        report_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        report_view.set_left_margin(14)
        report_view.set_right_margin(14)
        task_tabs.append_page(scroller(report_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC), label(u"任务报告"))
        self.history_task_tabs = task_tabs
        task_page.pack_start(card(task_tabs, "surface", 0), True, True, 0)
        self.history_page_stack.add_named(task_page, "task")
        self.history_page_stack.set_visible_child_name("projects")
        root.pack_start(self.history_page_stack, True, True, 0)
        return self.page_container(root)

    def build_new_project_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        root.pack_start(label(u"从零创建工程", "title-large"), False, False, 0)
        root.pack_start(label(u"只需告诉 AI 想研究什么。AI 会查找依据、提出必要问题，给出可审查的工程与源文件方案；批准后才创建和试运行。", "muted", True), False, False, 0)
        workflow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.new_project_workflow_labels = []
        for index, text in enumerate((u"1 描述需求", u"2 依据与澄清", u"3 审查方案", u"4 创建与验证", u"5 交付")):
            item = Gtk.Button(label=text)
            add_class(item, "workflow-step")
            add_class(item, "workflow-pending")
            item.set_hexpand(True)
            item.connect("clicked", self.on_new_project_workflow_clicked, index)
            workflow.pack_start(item, True, True, 0)
            self.new_project_workflow_labels.append(item)
        root.pack_start(workflow, False, False, 0)
        self.update_new_project_workflow_styles()

        self.new_project_status_label = label(u"请描述你想创建的器件、物理现象或待解决的问题。", "small-muted", True)
        root.pack_start(self.new_project_status_label, False, False, 0)
        self.new_project_stage_stack = Gtk.Stack()
        self.new_project_stage_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.new_project_stage_stack.set_transition_duration(180)

        basic = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        basic.pack_start(label(u"你想让这个新工程做什么？", "title-medium"), False, False, 0)
        self.new_project_prompt_buffer = Gtk.TextBuffer()
        prompt_view = Gtk.TextView(buffer=self.new_project_prompt_buffer)
        prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        prompt_view.set_left_margin(15)
        prompt_view.set_right_margin(15)
        prompt_scroll = scroller(prompt_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        prompt_scroll.set_min_content_height(150)
        prompt_scroll.set_size_request(-1, 150)
        basic.pack_start(card(prompt_scroll, "surface", 8), False, False, 0)
        basic.pack_start(label(u"例如：建立二维 PN 结，研究不同反向偏压下的电场与击穿行为；请给出合理假设和可验证的结果。", "muted", True), False, False, 0)
        reference_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        reference_box.pack_start(label(u"参考 PDF（可选）", "title-small"), False, False, 0)
        reference_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.new_project_reference_label = label(u"未添加。可选择论文、报告或器件说明书。", "small-muted", True)
        reference_row.pack_start(self.new_project_reference_label, True, True, 0)
        self.new_project_reference_remove_button = button(u"移除", "edit-delete-symbolic")
        self.new_project_reference_remove_button.set_sensitive(False)
        self.new_project_reference_remove_button.connect("clicked", self.on_remove_generation_pdf)
        reference_row.pack_end(self.new_project_reference_remove_button, False, False, 0)
        self.new_project_reference_button = button(u"添加 PDF", "document-open-symbolic")
        self.new_project_reference_button.connect("clicked", self.on_choose_generation_pdf)
        reference_row.pack_end(self.new_project_reference_button, False, False, 0)
        reference_box.pack_start(reference_row, False, False, 0)
        reference_box.pack_start(label(
            u"PDF 会先在本机按页提取；规划时仅把相关页摘要发送给已配置模型。"
            u"纯扫描件若没有可提取文字，会明确提示需要 OCR。",
            "small-muted", True), False, False, 0)
        basic.pack_start(card(reference_box, "surface", 10), False, False, 0)
        self.new_project_plan_button = button(u"让 AI 查找依据并设计工程", "go-next-symbolic", "primary")
        self.new_project_plan_button.connect("clicked", self.on_plan_project_create)
        basic.pack_start(self.new_project_plan_button, False, False, 0)
        basic.pack_start(label(u"不用找模板或先取名字。AI 只把示例当参考；没有足够依据时会明确说明。", "terminal-label", True), False, False, 0)
        self.new_project_stage_stack.add_named(basic, "basic")

        research = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.new_project_research_title = label(u"AI 正在理解你的需求", "title-medium")
        research.pack_start(self.new_project_research_title, False, False, 0)
        progress_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.new_project_phase_label = label(u"步骤 1/3 · 理解器件与目标", "status-badge", True)
        self.new_project_elapsed_label = label(u"已用时 00:00", "small-muted")
        progress_head.pack_start(self.new_project_phase_label, True, True, 0)
        progress_head.pack_end(self.new_project_elapsed_label, False, False, 0)
        research.pack_start(progress_head, False, False, 0)
        self.new_project_research_progress = Gtk.ProgressBar()
        add_class(self.new_project_research_progress, "ai-research-progress")
        self.new_project_research_progress.set_show_text(True)
        self.new_project_research_progress.set_text(u"准备调用 AI")
        research.pack_start(self.new_project_research_progress, False, False, 0)
        self.new_project_research_buffer = Gtk.TextBuffer()
        research_view = Gtk.TextView(buffer=self.new_project_research_buffer)
        research_view.set_editable(False)
        research_view.set_cursor_visible(False)
        research_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        research_scroll = scroller(research_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        research_scroll.set_size_request(-1, 185)
        research.pack_start(card(research_scroll, "surface", 8), False, False, 0)
        self.new_project_clarification = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.new_project_clarification.set_no_show_all(True)
        self.new_project_clarification.pack_start(label(u"请补充下面的关键条件，再继续生成方案：", "muted", True), False, False, 0)
        self.new_project_questions = label(u"", "muted", True)
        self.new_project_clarification.pack_start(self.new_project_questions, False, False, 0)
        self.new_project_answer_buffer = Gtk.TextBuffer()
        answer_view = Gtk.TextView(buffer=self.new_project_answer_buffer)
        answer_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        answer_scroll = scroller(answer_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        answer_scroll.set_min_content_height(85)
        self.new_project_clarification.pack_start(card(answer_scroll), False, False, 0)
        answer_button = button(u"补充并继续", "go-next-symbolic", "primary")
        answer_button.connect("clicked", self.on_answer_project_create)
        self.new_project_clarification.pack_start(answer_button, False, False, 0)
        research.pack_start(self.new_project_clarification, False, False, 0)
        research_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        research_back = button(u"返回修改需求", "go-previous-symbolic")
        research_back.connect("clicked", lambda *_args: self.show_new_project_stage("basic"))
        self.new_project_stop_planning_button = button(u"停止本次分析", "process-stop-symbolic")
        self.new_project_stop_planning_button.connect("clicked", self.on_stop_project_planning)
        self.new_project_stop_planning_button.set_sensitive(False)
        research_controls.pack_start(research_back, False, False, 0)
        research_controls.pack_end(self.new_project_stop_planning_button, False, False, 0)
        research.pack_start(research_controls, False, False, 0)
        self.new_project_clarification.hide()
        self.new_project_stage_stack.add_named(research, "research")

        blueprint = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        blueprint.pack_start(label(u"工程蓝图", "title-medium"), False, False, 0)
        blueprint.pack_start(label(u"此时还没有创建工程。请核对物理假设、依据、Tool 链和文件内容。", "muted", True), False, False, 0)
        name_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name_row.pack_start(label(u"工程名称（可改）"), False, False, 0)
        self.new_project_name_entry = Gtk.Entry()
        self.new_project_name_entry.set_hexpand(True)
        name_row.pack_start(self.new_project_name_entry, True, True, 0)
        blueprint.pack_start(name_row, False, False, 0)
        directory_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        directory_row.pack_start(label(u"创建位置"), False, False, 0)
        self.new_project_directory_entry = Gtk.Entry()
        self.new_project_directory_entry.set_hexpand(True)
        self.new_project_directory_entry.set_editable(False)
        self.new_project_directory_entry.set_placeholder_text(u"默认放在当前工作区的 aitcad_workspaces 下")
        directory_row.pack_start(self.new_project_directory_entry, True, True, 0)
        choose_directory = button(u"选择目录", "folder-open-symbolic")
        choose_directory.connect("clicked", self.on_choose_new_project_parent)
        directory_row.pack_end(choose_directory, False, False, 0)
        blueprint.pack_start(directory_row, False, False, 0)
        self.new_project_plan_buffer = Gtk.TextBuffer()
        plan_view = Gtk.TextView(buffer=self.new_project_plan_buffer)
        plan_view.set_editable(False)
        plan_view.set_cursor_visible(False)
        plan_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        plan_scroll = scroller(plan_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        plan_scroll.set_min_content_height(260)
        blueprint.pack_start(card(plan_scroll, "surface", 8), True, True, 0)
        blueprint_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        blueprint_back = button(u"返回修改", "go-previous-symbolic")
        blueprint_back.connect("clicked", lambda *_args: self.show_new_project_stage("basic"))
        self.new_project_review_button = button(u"继续审查文件", "go-next-symbolic", "primary")
        self.new_project_review_button.set_sensitive(False)
        self.new_project_review_button.connect("clicked", self.on_review_project_files)
        blueprint_controls.pack_start(blueprint_back, False, False, 0)
        blueprint_controls.pack_end(self.new_project_review_button, False, False, 0)
        blueprint.pack_start(blueprint_controls, False, False, 0)
        self.new_project_stage_stack.add_named(blueprint, "blueprint")

        review = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        review.pack_start(label(u"文件审查", "title-medium"), False, False, 0)
        review.pack_start(label(u"以下是 AI 生成的完整源文件，包含代码和证据。批准后将重新核对内容指纹。", "muted", True), False, False, 0)
        self.new_project_file_buffer = Gtk.TextBuffer()
        file_view = Gtk.TextView(buffer=self.new_project_file_buffer)
        file_view.set_editable(False)
        file_view.set_cursor_visible(False)
        file_view.set_monospace(True)
        file_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        file_scroll = scroller(file_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        file_scroll.set_min_content_height(300)
        review.pack_start(card(file_scroll, "surface", 8), True, True, 0)
        validation_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        validation_row.pack_start(label(u"交付前验证", "title-small"), False, False, 0)
        self.new_project_validation_combo = Gtk.ComboBoxText()
        self.new_project_validation_combo.append("preflight", u"快速预检（不运行仿真）")
        self.new_project_validation_combo.append("baseline", u"标准验收（推荐：最短代表性依赖路径）")
        self.new_project_validation_combo.append("full", u"完整验收（运行全部叶节点，可能很久）")
        self.new_project_validation_combo.set_active_id("baseline")
        self.new_project_validation_combo.set_hexpand(True)
        validation_row.pack_start(self.new_project_validation_combo, False, False, 0)
        validation_row.pack_start(label(
            u"复杂工程无需等待全量扫描：快速预检只核对文件、宏和依赖；标准验收只跑一条最短可运行链；"
            u"完整验收才运行所有叶节点。只有真实节点完成并产生结果时才标记为仿真已验证。",
            "small-muted", True), False, False, 0)
        review.pack_start(card(validation_row, "surface", 10), False, False, 0)
        review_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        review_back = button(u"返回蓝图", "go-previous-symbolic")
        review_back.connect("clicked", lambda *_args: self.show_new_project_stage("blueprint"))
        self.new_project_create_button = button(u"批准并创建", "folder-new-symbolic", "primary")
        self.new_project_create_button.set_sensitive(False)
        self.new_project_create_button.connect("clicked", self.on_apply_project_create)
        review_controls.pack_start(review_back, False, False, 0)
        review_controls.pack_end(self.new_project_create_button, False, False, 0)
        review.pack_start(review_controls, False, False, 0)
        self.new_project_stage_stack.add_named(review, "review")

        execution = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        execution.pack_start(label(u"正在创建并验证", "title-medium"), False, False, 0)
        self.new_project_execution_progress = Gtk.ProgressBar()
        execution.pack_start(self.new_project_execution_progress, False, False, 0)
        self.new_project_execution_buffer = Gtk.TextBuffer()
        execution_view = Gtk.TextView(buffer=self.new_project_execution_buffer)
        execution_view.set_editable(False)
        execution_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        execution.pack_start(card(scroller(execution_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC), "surface", 8), True, True, 0)
        stop_button = button(u"停止运行", "process-stop-symbolic")
        stop_button.connect("clicked", self.on_stop_project_create)
        execution.pack_start(stop_button, False, False, 0)
        self.new_project_stage_stack.add_named(execution, "executing")

        complete = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        complete.set_valign(Gtk.Align.CENTER)
        complete.pack_start(logo_image(64), False, False, 0)
        self.new_project_complete_label = label(u"正在创建并验证工程…", "welcome-subtitle", True, 0.5)
        complete.pack_start(self.new_project_complete_label, False, False, 0)
        self.new_project_failure_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.new_project_failure_box.set_no_show_all(True)
        self.new_project_failure_title = label(u"验证未通过", "title-medium", True, 0.5)
        self.new_project_failure_box.pack_start(self.new_project_failure_title, False, False, 0)
        self.new_project_failure_summary = label(u"", "muted", True, 0.5)
        self.new_project_failure_box.pack_start(self.new_project_failure_summary, False, False, 0)
        failure_expander = Gtk.Expander(label=u"查看技术细节")
        self.new_project_failure_buffer = Gtk.TextBuffer()
        failure_view = Gtk.TextView(buffer=self.new_project_failure_buffer)
        failure_view.set_editable(False)
        failure_view.set_cursor_visible(False)
        failure_view.set_monospace(True)
        failure_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        failure_scroll = scroller(failure_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        failure_scroll.set_min_content_height(150)
        failure_expander.add(failure_scroll)
        self.new_project_failure_box.pack_start(failure_expander, False, False, 0)
        complete.pack_start(card(self.new_project_failure_box, "task-card", 12), False, False, 0)
        self.new_project_open_button = button(u"打开并让 AI 理解", "document-open-symbolic", "primary")
        self.new_project_open_button.set_halign(Gtk.Align.CENTER)
        self.new_project_open_button.set_sensitive(False)
        self.new_project_open_button.connect("clicked", self.on_open_created_project)
        complete.pack_start(self.new_project_open_button, False, False, 0)
        new_again = button(u"创建另一个工程", "list-add-symbolic")
        new_again.set_halign(Gtk.Align.CENTER)
        new_again.connect("clicked", self.on_reset_project_create)
        complete.pack_start(new_again, False, False, 0)
        self.new_project_stage_stack.add_named(complete, "complete")
        self.new_project_stage_stack.set_visible_child_name("basic")
        root.pack_start(self.new_project_stage_stack, True, True, 0)
        return self.page_container(root)

    @staticmethod
    def new_project_stage_index(stage):
        return {
            "basic": 0, "research": 1, "blueprint": 2, "review": 2,
            "executing": 3, "complete": 4,
        }.get(stage, 0)

    def on_new_project_workflow_clicked(self, _button, index):
        if index > getattr(self, "new_project_unlocked_index", 0):
            return
        if index == 0:
            stage = "basic"
        elif index == 1:
            stage = "research"
        elif index == 2:
            stage = "review" if "review" in self.new_project_reached_stages else "blueprint"
        elif index == 3:
            stage = "executing"
        else:
            stage = "complete"
        if stage in self.new_project_reached_stages:
            self.show_new_project_stage(stage)

    def show_new_project_stage(self, stage):
        """Review a reached page without changing the live creation state."""
        if stage not in getattr(self, "new_project_reached_stages", set(["basic"])):
            return
        self.new_project_view_stage = stage
        self.new_project_stage_stack.set_visible_child_name(stage)
        self.update_new_project_workflow_styles()

    def set_new_project_stage(self, stage):
        """Advance the real workflow and display that stage."""
        self.new_project_active_stage = stage
        self.new_project_view_stage = stage
        self.new_project_reached_stages.add(stage)
        self.new_project_unlocked_index = max(
            getattr(self, "new_project_unlocked_index", 0), self.new_project_stage_index(stage),
        )
        self.new_project_stage_stack.set_visible_child_name(stage)
        self.update_new_project_workflow_styles()

    def update_new_project_workflow_styles(self):
        classes = (
            "workflow-pending", "workflow-review", "workflow-complete",
            "workflow-blocked", "workflow-viewing",
        )
        active_index = self.new_project_stage_index(getattr(self, "new_project_active_stage", "basic"))
        viewed_index = self.new_project_stage_index(getattr(self, "new_project_view_stage", "basic"))
        unlocked = getattr(self, "new_project_unlocked_index", 0)
        for index, widget in enumerate(self.new_project_workflow_labels):
            context = widget.get_style_context()
            for name in classes:
                context.remove_class(name)
            if index < active_index:
                context.add_class("workflow-complete")
            elif index == active_index:
                context.add_class("workflow-review")
            else:
                context.add_class("workflow-pending")
            if index == viewed_index and viewed_index != active_index:
                context.add_class("workflow-viewing")
            widget.set_sensitive(index <= unlocked)

    def build_code_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.pack_start(label(u"通用 TCAD 模型研究与改写", "title-large"), False, False, 0)
        title_box.pack_start(label(u"面向任意物理模型：先选择 Tool，检索当前版本依据，再由大模型生成动态参数表与最小代码差异。", "muted", True), False, False, 0)
        top.pack_start(title_box, True, True, 0)
        self.code_task_id_label = label(u"NEW RESEARCH TASK", "planned-pill")
        top.pack_end(self.code_task_id_label, False, False, 0)
        root.pack_start(top, False, False, 0)

        workflow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.code_workflow_labels = []
        for index, text in enumerate((u"1 选择 Tool", u"2 检索依据", u"3 审查差异", u"4 执行验证")):
            item = label(text, "workflow-pending", True, 0.5)
            item.set_hexpand(True)
            workflow.pack_start(item, True, True, 0)
            self.code_workflow_labels.append(item)
        root.pack_start(workflow, False, False, 0)
        self.code_stage_stack = Gtk.Stack()
        self.code_stage_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.code_stage_stack.set_transition_duration(180)

        select_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        request_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        request_box.pack_start(label(u"研究目标", "eyebrow"), False, False, 0)
        self.code_prompt_buffer = Gtk.TextBuffer()
        self.code_prompt_buffer.set_text(u"请根据当前版本 manual、tutorial 和相关文章，为这个工程加入所需物理模型；列出每个参数的含义、单位、依据与待标定项，修改前给出差异和风险，验证后生成报告。")
        prompt_view = Gtk.TextView(buffer=self.code_prompt_buffer)
        prompt_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        prompt_scroll = scroller(prompt_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        prompt_scroll.set_min_content_height(92)
        request_box.pack_start(prompt_scroll, False, False, 0)
        request_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        request_controls.pack_start(label(u"目标 Tool", "small-muted"), False, False, 0)
        self.code_tool_combo = Gtk.ComboBoxText()
        for tool_id, tool_name in (("auto", u"自动识别"), ("sdevice", "SDevice"), ("sprocess", "SProcess"), ("sde", "SDE"), ("inspect", "Inspect")):
            self.code_tool_combo.append(tool_id, tool_name)
        self.code_tool_combo.set_active_id("auto")
        request_controls.pack_start(self.code_tool_combo, False, False, 0)
        self.code_include_articles = Gtk.CheckButton(label=u"检索在线论文题录/摘要")
        self.code_include_articles.set_active(True)
        request_controls.pack_start(self.code_include_articles, False, False, 0)
        self.code_research_spinner = Gtk.Spinner()
        request_controls.pack_end(self.code_research_spinner, False, False, 0)
        self.code_research_button = button(u"检索证据并生成方案", "system-search-symbolic", "primary")
        self.code_research_button.connect("clicked", self.on_plan_research_task)
        request_controls.pack_end(self.code_research_button, False, False, 0)
        request_box.pack_start(request_controls, False, False, 0)
        select_page.pack_start(card(request_box, "task-card", 14), False, False, 0)
        select_page.pack_start(label(u"先明确要修改的物理目标和 Tool。检索和方案生成不会写入源文件，也不会启动节点。", "terminal-label", True), False, False, 0)
        self.code_stage_stack.add_named(select_page, "select")

        evidence_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        evidence_page.pack_start(label(u"正在检索依据并建立工程上下文", "title-medium"), False, False, 0)
        self.code_evidence_progress = Gtk.ProgressBar()
        self.code_evidence_progress.set_pulse_step(0.08)
        evidence_page.pack_start(self.code_evidence_progress, False, False, 0)
        summary = Gtk.Grid(column_spacing=10, row_spacing=10)
        self.code_status_label = label(u"等待研究目标", "title-medium", True)
        self.code_evidence_label = label(u"Manual 0 · Tutorial 0 · 论文 0", "muted", True)
        self.code_interface_label = label(u"目标 Tool / 文件：尚未识别", "muted", True)
        for dynamic_label in (self.code_status_label, self.code_evidence_label, self.code_interface_label):
            dynamic_label.set_max_width_chars(96)
            dynamic_label.set_hexpand(True)
        summary.attach(label(u"状态", "small-muted"), 0, 0, 1, 1)
        summary.attach(self.code_status_label, 1, 0, 1, 1)
        summary.attach(label(u"证据", "small-muted"), 0, 1, 1, 1)
        summary.attach(self.code_evidence_label, 1, 1, 1, 1)
        summary.attach(label(u"对象", "small-muted"), 0, 2, 1, 1)
        summary.attach(self.code_interface_label, 1, 2, 1, 1)
        evidence_page.pack_start(card(summary), False, False, 0)
        evidence_page.pack_start(label(u"依次完成：识别真实 Tool 文件 → 检索版本匹配 Manual/Tutorial → 可选论文检索 → AI 生成结构化差异。", "muted", True), False, False, 0)
        self.code_stage_stack.add_named(evidence_page, "evidence")

        review_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        notebook = Gtk.Notebook()
        self.code_assumptions_buffer = Gtk.TextBuffer()
        assumptions_view = Gtk.TextView(buffer=self.code_assumptions_buffer)
        assumptions_view.set_editable(False)
        assumptions_view.set_cursor_visible(False)
        assumptions_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        assumptions_scroll = scroller(assumptions_view, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        assumptions_scroll.set_min_content_height(220)
        notebook.append_page(assumptions_scroll, label(u"依据与物理假设"))

        self.code_diff_buffer = Gtk.TextBuffer()
        diff_view = Gtk.TextView(buffer=self.code_diff_buffer)
        diff_view.set_editable(False)
        diff_view.set_cursor_visible(False)
        diff_view.set_monospace(True)
        diff_view.set_wrap_mode(Gtk.WrapMode.NONE)
        diff_scroll = scroller(diff_view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        diff_scroll.set_min_content_height(330)
        notebook.append_page(diff_scroll, label(u"统一差异"))
        review_page.pack_start(card(notebook, "surface", 7), True, True, 0)

        model_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        model_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        model_head.pack_start(label(u"模型参数与证据绑定", "title-medium"), True, True, 0)
        model_head.pack_end(label(u"随模型动态生成", "planned-pill"), False, False, 0)
        model_box.pack_start(model_head, False, False, 0)
        model_box.pack_start(label(u"不同模型显示各自的参数、单位、作用、来源和置信状态；没有可靠依据的值必须标记为待标定。", "muted", True), False, False, 0)
        parameter_view = Gtk.TreeView(model=self.model_param_store)
        parameter_view.set_headers_visible(True)
        for title, column, width in ((u"参数", 0, 170), (u"建议值/表达式", 1, 170), (u"单位", 2, 120), (u"作用与依据", 3, 480), (u"状态", 4, 130)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            item = Gtk.TreeViewColumn(title, renderer, text=column)
            item.set_min_width(width)
            if column == 3:
                item.set_expand(True)
            parameter_view.append_column(item)
        parameter_scroll = scroller(parameter_view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        parameter_scroll.set_min_content_height(180)
        model_box.pack_start(parameter_scroll, False, False, 0)
        validation_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        validation_row.pack_start(label(u"验证节点（可选）", "small-muted"), False, False, 0)
        self.code_validation_node_entry = Gtk.Entry()
        self.code_validation_node_entry.set_width_chars(10)
        self.code_validation_node_entry.set_placeholder_text(u"自动")
        validation_row.pack_start(self.code_validation_node_entry, False, False, 0)
        self.code_run_status_label = label(u"先生成并应用代码方案。", "small-muted", True)
        validation_row.pack_start(self.code_run_status_label, True, True, 0)
        self.code_run_button = button(u"运行 Tool 验证", "media-playback-start-symbolic", "primary")
        self.code_run_button.set_sensitive(False)
        self.code_run_button.connect("clicked", self.on_run_model_task)
        validation_row.pack_end(self.code_run_button, False, False, 0)
        review_page.pack_start(card(model_box), False, False, 0)

        review_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        revise = button(u"返回修改目标", "go-previous-symbolic")
        revise.connect("clicked", lambda *_args: self.set_code_stage("select"))
        open_manual = button(u"查看全部证据", "help-browser-symbolic")
        open_manual.connect("clicked", lambda *_args: self.show_page("manuals"))
        self.code_report_button = button(u"生成阶段报告", "document-save-symbolic")
        self.code_report_button.set_sensitive(False)
        self.code_report_button.connect("clicked", self.on_generate_current_report)
        self.code_apply_button = button(u"批准并应用代码", "document-save-symbolic", "primary")
        self.code_apply_button.set_sensitive(False)
        self.code_apply_button.connect("clicked", self.on_apply_research_plan)
        review_actions.pack_start(revise, False, False, 0)
        review_actions.pack_start(open_manual, False, False, 0)
        review_actions.pack_end(self.code_apply_button, False, False, 0)
        review_page.pack_start(review_actions, False, False, 0)
        self.code_stage_stack.add_named(review_page, "review")

        validation_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        validation_page.pack_start(label(u"执行验证", "title-medium"), False, False, 0)
        validation_page.pack_start(label(u"代码变更已经建立可恢复备份。选择节点后运行真实 Tool，并把状态、日志和报告保存到工程档案。", "muted", True), False, False, 0)
        validation_page.pack_start(card(validation_row), False, False, 0)
        validation_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back_review = button(u"返回查看差异", "go-previous-symbolic")
        back_review.connect("clicked", lambda *_args: self.set_code_stage("review"))
        validation_actions.pack_start(back_review, False, False, 0)
        validation_actions.pack_end(self.code_report_button, False, False, 0)
        validation_page.pack_start(validation_actions, False, False, 0)
        self.code_stage_stack.add_named(validation_page, "validation")
        self.code_stage_stack.set_visible_child_name("select")
        root.pack_start(self.code_stage_stack, True, True, 0)
        root.pack_start(label(u"通用安全边界：大模型只生成待审查方案。所有数值必须绑定证据或明确标为待标定；写入前检查源 SHA，创建备份，运行前再次确认 Tool 节点。", "terminal-label", True), False, False, 0)
        return self.page_container(root)

    def set_code_stage(self, stage):
        self.code_stage_stack.set_visible_child_name(stage)
        states = {
            "select": ["review", "pending", "pending", "pending"],
            "evidence": ["complete", "review", "pending", "pending"],
            "review": ["complete", "complete", "review", "pending"],
            "validation": ["complete", "complete", "complete", "review"],
        }
        self.set_code_workflow_states(states.get(stage, states["select"]))

    def build_manuals_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.pack_start(label(u"证据与手册中心", "title-large"), False, False, 0)
        title_box.pack_start(label(u"优先检索设置中所选 Sentaurus 版本的本机 Manual 和 Applications Library；论文结果明确标注题录、摘要或全文级别。", "muted", True), False, False, 0)
        header.pack_start(title_box, True, True, 0)
        self.manual_index_label = label(u"正在检查本机文档…", "planned-pill")
        header.pack_end(self.manual_index_label, False, False, 0)
        root.pack_start(header, False, False, 0)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.manual_tool_combo = Gtk.ComboBoxText()
        for tool_id, tool_name in (("sdevice", "SDevice"), ("sprocess", "SProcess"), ("sde", "SDE"), ("inspect", "Inspect")):
            self.manual_tool_combo.append(tool_id, tool_name)
        self.manual_tool_combo.set_active_id("sdevice")
        search_row.pack_start(self.manual_tool_combo, False, False, 0)
        self.manual_search_entry = Gtk.Entry()
        self.manual_search_entry.set_placeholder_text(u"例如：mobility、SRH、quantum potential、implant、mesh refinement")
        self.manual_search_entry.set_text(u"physical model syntax parameters calibration validation")
        self.manual_search_entry.connect("activate", self.on_manual_search)
        search_row.pack_start(self.manual_search_entry, True, True, 0)
        self.manual_include_articles = Gtk.CheckButton(label=u"论文")
        self.manual_include_articles.set_active(True)
        search_row.pack_start(self.manual_include_articles, False, False, 0)
        search_button = button(u"检索证据", "system-search-symbolic", "primary")
        search_button.connect("clicked", self.on_manual_search)
        search_row.pack_end(search_button, False, False, 0)
        root.pack_start(search_row, False, False, 0)

        self.manual_status_label = label(u"首次检索会在本机建立页级索引，通常只需几秒。", "small-muted", True)
        root.pack_start(self.manual_status_label, False, False, 0)
        view = Gtk.TreeView(model=self.manual_store)
        view.set_headers_visible(True)
        view.get_selection().connect("changed", self.on_manual_result_selected)
        for title, column, width in ((u"类型", 0, 85), (u"来源", 1, 300), (u"位置", 2, 265), (u"命中摘要", 3, 500)):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            item = Gtk.TreeViewColumn(title, renderer, text=column)
            item.set_min_width(width)
            if column == 3:
                item.set_expand(True)
            view.append_column(item)
        result_scroll = scroller(view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        result_scroll.set_min_content_height(310)
        root.pack_start(card(result_scroll, "surface", 0), True, True, 0)

        self.manual_detail_buffer = Gtk.TextBuffer()
        detail = Gtk.TextView(buffer=self.manual_detail_buffer)
        detail.set_editable(False)
        detail.set_cursor_visible(False)
        detail.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        detail_scroll = scroller(detail, Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        detail_scroll.set_min_content_height(165)
        root.pack_start(card(detail_scroll, "surface", 8), False, False, 0)
        footer = label(u"证据级别：Manual=版本语法；Tutorial=官方可运行示例；论文题录/摘要=研究线索。只有实际读取且适用于当前器件的内容才能进入数值标定。", "terminal-label", True)
        root.pack_start(footer, False, False, 0)
        return self.page_container(root)

    def build_placeholder_page(self, heading, description, bullets, footer):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        root.pack_start(label(heading, "title-large"), False, False, 0)
        root.pack_start(label(description, "muted", True), False, False, 0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.pack_start(label(u"设计原则", "eyebrow"), False, False, 0)
        for index, text in enumerate(bullets):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            number = label(str(index + 1), "brand-mark", xalign=0.5)
            number.set_size_request(30, 30)
            row.pack_start(number, False, False, 0)
            row.pack_start(label(text, "title-medium", True), True, True, 0)
            content.pack_start(row, False, False, 0)
        root.pack_start(card(content, "placeholder-card", 18), False, False, 0)
        root.pack_start(label(footer, "terminal-label", True), False, False, 0)
        return self.page_container(root)

    @staticmethod
    def text_buffer_value(buffer):
        start, end = buffer.get_bounds()
        return as_text(buffer.get_text(start, end, True)).strip()

    def load_research_status_worker(self):
        try:
            result = core.research_rpc({"action": "status"})
            GLib.idle_add(self.apply_research_status, result)
        except Exception as error:
            GLib.idle_add(self.apply_research_status_error, error_text(error))

    def apply_research_status(self, result):
        self.research_status = result
        manual = result.get("manual") or {}
        tutorials = result.get("tutorials") or {}
        tools = result.get("tools") or {}
        if tools:
            indexed_tools = [item for item in tools.values() if item.get("indexed")]
            manual_tools = [item for item in tools.values() if item.get("manualExists")]
            tutorial_count = sum(int(item.get("tutorialFiles") or 0) for item in tools.values())
            text = u"● %s · Manual %d/%d 已索引 · Tutorial %d 文件" % (
                as_text(result.get("release") or u"当前版本"), len(indexed_tools), len(manual_tools), tutorial_count,
            )
            css = "ready-pill" if indexed_tools else "planned-pill"
        elif manual.get("indexed"):
            text = u"● %s · %s 页 · Tutorial %s 文件" % (
                as_text(result.get("release") or u"当前版本"),
                manual.get("pages") or 0,
                tutorials.get("files") or 0,
            )
            css = "ready-pill"
        elif manual.get("exists"):
            text = u"○ %s · 首次检索时建立索引" % as_text(result.get("release") or u"当前版本")
            css = "planned-pill"
        else:
            text = u"● 未找到当前版本手册"
            css = "error-text"
        self.manual_index_label.set_text(text)
        for name in ("ready-pill", "planned-pill", "error-text"):
            self.manual_index_label.get_style_context().remove_class(name)
        self.manual_index_label.get_style_context().add_class(css)
        if hasattr(self, "history_storage_label"):
            self.history_storage_label.set_text(u"任务库：%s\n报告目录：%s" % (
                as_text(result.get("databasePath") or u"—"),
                as_text(result.get("reportRoot") or u"—"),
            ))
        return False

    def apply_research_status_error(self, message):
        self.manual_index_label.set_text(u"● 研究服务不可用")
        self.manual_index_label.get_style_context().add_class("error-text")
        self.manual_status_label.set_text(as_text(message))
        self.history_status_label.set_text(u"研究任务库不可用：%s" % as_text(message))
        return False

    def on_manual_search(self, *_args):
        query = as_text(self.manual_search_entry.get_text()).strip()
        if len(query) < 2:
            self.show_message(u"检索词至少需要 2 个字符。")
            return
        if self.research_busy:
            self.show_message(u"当前研究请求仍在进行，请稍候。")
            return
        self.research_busy = True
        release = as_text(core.sentaurus_runtime_status().get("selectedRelease") or u"当前版本")
        self.manual_status_label.set_text(u"正在检索 %s Manual、Applications Library%s…" % (release, u"和在线论文题录/摘要" if self.manual_include_articles.get_active() else u""))
        start_thread(self.manual_search_worker, (query, self.manual_include_articles.get_active(), self.manual_tool_combo.get_active_id() or "sdevice"))

    def manual_search_worker(self, query, include_articles, tool):
        try:
            result = core.research_rpc({"action": "search", "query": query, "includeArticles": bool(include_articles), "tool": tool})
            GLib.idle_add(self.apply_manual_search, result)
        except Exception as error:
            GLib.idle_add(self.apply_manual_search_error, error_text(error))

    def research_result_rows(self, result):
        return (
            list(result.get("manualResults") or []) +
            list(result.get("tutorialResults") or []) +
            list(result.get("articleResults") or [])
        )

    def render_manual_results(self, result):
        self.manual_store.clear()
        kind_names = {"manual": u"Manual", "tutorial": u"Tutorial", "article": u"论文"}
        rows = self.research_result_rows(result)
        for item in rows:
            self.manual_store.append((
                kind_names.get(item.get("kind"), as_text(item.get("kind") or u"证据")),
                as_text(item.get("title") or u"未命名来源"),
                as_text(item.get("location") or item.get("path") or u"—"),
                as_text(item.get("snippet") or u"—"),
                json.dumps(item, ensure_ascii=True),
            ))
        return rows

    def apply_manual_search(self, result):
        self.research_busy = False
        rows = self.render_manual_results(result)
        self.manual_detail_buffer.set_text(u"选择一条证据查看路径、页码、DOI 与命中摘要。")
        counts = (len(result.get("manualResults") or []), len(result.get("tutorialResults") or []), len(result.get("articleResults") or []))
        status = u"找到 Manual %d · Tutorial %d · 论文 %d" % counts
        if result.get("articlesError"):
            status += u"；在线检索不可用：%s" % as_text(result.get("articlesError"))
        self.manual_status_label.set_text(status)
        start_thread(self.load_research_status_worker)
        if not rows:
            self.manual_detail_buffer.set_text(u"没有命中。可尝试英文模型名、SDevice 关键字或缩短检索词。")
        return False

    def apply_manual_search_error(self, message):
        self.research_busy = False
        self.manual_status_label.set_text(u"检索失败：%s" % as_text(message))
        return False

    def on_manual_result_selected(self, selection):
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        try:
            item = json.loads(model.get_value(iterator, 4))
        except ValueError:
            return
        lines = [
            as_text(item.get("title") or u"证据"),
            as_text(item.get("location") or u""),
        ]
        if item.get("path"):
            lines.append(u"本机路径：%s" % as_text(item.get("path")))
        if item.get("doi"):
            lines.append(u"DOI：%s" % as_text(item.get("doi")))
        if item.get("url"):
            lines.append(u"链接：%s" % as_text(item.get("url")))
        if item.get("evidenceLevel"):
            lines.append(u"证据级别：%s" % as_text(item.get("evidenceLevel")))
        lines.extend([u"", as_text(item.get("snippet") or u"没有可用摘要。")])
        self.manual_detail_buffer.set_text(u"\n".join(lines))

    def on_plan_research_task(self, *_args):
        question = self.text_buffer_value(self.code_prompt_buffer)
        if not question:
            self.show_message(u"请先填写希望研究和修改的物理模型目标。")
            return
        if self.research_busy:
            self.show_message(u"当前正在执行证据检索或方案生成，请等待状态更新。")
            return
        try:
            project = self.project_absolute_path()
        except Exception as error:
            self.show_message(error_text(error), Gtk.MessageType.ERROR)
            return
        self.research_busy = True
        self.research_plan = None
        self.code_research_button.set_sensitive(False)
        self.code_research_spinner.start()
        self.code_apply_button.set_sensitive(False)
        self.code_report_button.set_sensitive(False)
        self.code_status_label.set_text(u"正在定位真实 Tool 源文件并检索当前版本依据…")
        self.code_diff_buffer.set_text(u"等待生成可审查的统一差异。")
        self.code_evidence_progress.set_fraction(0.0)
        self.set_code_stage("evidence")
        GLib.timeout_add(180, self.pulse_code_evidence)
        tool = self.code_tool_combo.get_active_id() or "auto"
        start_thread(self.research_plan_worker, (project, question, self.code_include_articles.get_active(), tool))

    def pulse_code_evidence(self):
        if not self.research_busy or self.code_stage_stack.get_visible_child_name() != "evidence":
            return False
        self.code_evidence_progress.pulse()
        return True

    def research_plan_worker(self, project, question, include_articles, tool):
        try:
            result = core.research_rpc({
                "action": "plan-task",
                "project": project,
                "message": question,
                "includeArticles": bool(include_articles),
                "tool": tool,
            })
            research = result.get("research") or {}
            target = result.get("target") or {}
            GLib.idle_add(self.apply_code_evidence_phase, u"已识别 %s；已收集 Manual %d、Tutorial %d、论文 %d。" % (
                as_text(target.get("relativePath") or result.get("toolLabel") or u"目标 Tool"),
                len(research.get("manual") or []), len(research.get("tutorials") or []),
                len(research.get("articles") or []),
            ))
            synthesis = result.get("modelSynthesis") or {}
            if result.get("kind") == "tool-model" and synthesis.get("ready"):
                try:
                    GLib.idle_add(self.apply_code_evidence_phase, u"版本证据和真实源文件已就绪；AI 正在生成物理假设、动态参数与完整差异…")
                    modeled = core.ai_rpc({"action": "synthesize-research-task", "taskId": result.get("taskId")})
                    result = modeled.get("task") or result
                    result["providerModel"] = modeled.get("model")
                except Exception as model_error:
                    result["synthesisError"] = error_text(model_error)
                    result["summary"] = u"证据检索已完成，但大模型代码方案未生成：%s" % as_text(model_error)
            GLib.idle_add(self.render_research_plan, result)
        except Exception as error:
            GLib.idle_add(self.apply_research_plan_error, error_text(error))

    def apply_code_evidence_phase(self, message):
        if self.research_busy and self.code_stage_stack.get_visible_child_name() == "evidence":
            self.code_status_label.set_text(as_text(message))
        return False

    def set_code_workflow_states(self, states):
        classes = ("workflow-pending", "workflow-review", "workflow-complete", "workflow-blocked")
        for index, widget in enumerate(self.code_workflow_labels):
            context = widget.get_style_context()
            for name in classes:
                context.remove_class(name)
            state = states[index] if index < len(states) else "pending"
            context.add_class("workflow-" + (state if state in ("pending", "review", "complete", "blocked") else "pending"))

    def render_research_plan(self, result):
        self.research_busy = False
        self.code_research_spinner.stop()
        self.code_evidence_progress.set_fraction(1.0)
        self.code_research_button.set_sensitive(True)
        self.research_plan = result
        self.code_task_id_label.set_text(as_text(result.get("taskId") or u"RESEARCH TASK"))
        self.code_status_label.set_text(as_text(result.get("summary") or result.get("status") or u"方案已生成"))
        research = result.get("research") or {}
        manual = research.get("manual") or []
        tutorials = research.get("tutorials") or []
        articles = research.get("articles") or []
        self.code_evidence_label.set_text(u"Manual %d · Tutorial %d · 论文 %d%s" % (
            len(manual), len(tutorials), len(articles),
            u" · 在线检索失败" if result.get("articlesError") else u"",
        ))
        target = result.get("target") or {}
        interface = result.get("interface") or {}
        if target:
            self.code_interface_label.set_text(u"目标：%s · %s · SWB step %s" % (
                as_text(result.get("toolLabel") or target.get("tool") or u"Tool"),
                as_text(target.get("relativePath") or target.get("error") or u"未找到源文件"),
                as_text(target.get("step") if target.get("step") is not None else u"—"),
            ))
            if target.get("tool"):
                self.code_tool_combo.set_active_id(as_text(target.get("tool")))
        else:
            self.code_interface_label.set_text(u"目标界面：%s · 置信度 %s" % (
                as_text(interface.get("interface") or u"尚未识别"),
                as_text(interface.get("confidence") or u"—"),
            ))
        workflow_states = dict((item.get("id"), item.get("state") or "pending") for item in (result.get("workflow") or []))
        review_states = [workflow_states.get(key, "pending") for key in ("model", "parameters", "patch")]
        if "blocked" in review_states:
            review_state = "blocked"
        elif "review" in review_states:
            review_state = "review"
        elif review_states and all(value == "complete" for value in review_states):
            review_state = "complete"
        else:
            review_state = "pending"
        stages = [
            "complete", workflow_states.get("evidence", "pending"), review_state,
            workflow_states.get("validation", "pending"),
        ]
        self.set_code_workflow_states(stages)

        lines = [as_text(result.get("summary") or u""), u""]
        boundary = result.get("researchBoundary")
        if boundary:
            lines.extend([u"证据边界", as_text(boundary), u""])
        if result.get("synthesisError"):
            lines.extend([u"大模型生成状态", as_text(result.get("synthesisError")), u""])
        if result.get("physicalModel"):
            lines.extend([u"物理模型", as_text(result.get("physicalModel")), u""])
        if result.get("assumptions"):
            lines.append(u"物理假设")
            lines.extend(u"• " + as_text(value) for value in result.get("assumptions") or [])
            lines.append(u"")
        if result.get("fidelityOptions"):
            lines.append(u"建模层级")
            for option in result.get("fidelityOptions") or []:
                lines.append(u"• %s [%s] — %s" % (
                    as_text(option.get("label")), as_text(option.get("state")), as_text(option.get("description")),
                ))
            lines.append(u"")
        calibration = result.get("calibration") or {}
        if calibration.get("warning"):
            lines.extend([u"剂量标定门", as_text(calibration.get("warning")), u""])
        if result.get("validationPlan"):
            lines.append(u"建议验证步骤")
            lines.extend(u"• " + as_text(value) for value in result.get("validationPlan") or [])
            lines.append(u"")
        lines.append(u"证据清单")
        for item in manual + tutorials + articles:
            lines.append(u"• %s — %s" % (as_text(item.get("title") or u"来源"), as_text(item.get("location") or item.get("doi") or u"")))
        self.code_assumptions_buffer.set_text(u"\n".join(lines))
        modification = result.get("modification") or {}
        self.code_diff_buffer.set_text(as_text(modification.get("diff") or u"当前任务尚未生成可写入差异。"))
        self.model_param_store.clear()
        for item in result.get("modelParameters") or result.get("parameterPlan") or []:
            evidence_text = as_text(item.get("role") or u"")
            if item.get("evidence"):
                evidence_text += (u" · " if evidence_text else u"") + as_text(item.get("evidence"))
            status_text = as_text(item.get("confidence") or (u"待用户输入" if item.get("userInputRequired") else u"待审查"))
            self.model_param_store.append((
                as_text(item.get("name")), as_text(item.get("value")), as_text(item.get("unit")), evidence_text, status_text,
            ))
        self.code_apply_button.set_sensitive(bool(modification.get("content")) and result.get("status") not in ("code-applied", "completed"))
        self.code_report_button.set_sensitive(bool(result.get("taskId")))
        can_run = result.get("kind") == "tool-model" and result.get("status") in ("code-applied", "completed")
        self.code_run_button.set_sensitive(can_run)
        self.code_run_status_label.set_text(
            u"代码已应用；可指定节点或自动选择该 Tool 的最近节点。" if can_run else u"先生成并应用代码方案。"
        )
        applied = result.get("status") in ("code-applied", "completed")
        self.set_code_stage("validation" if applied else "review")
        if not applied:
            self.set_code_workflow_states(stages)
        elif result.get("status") == "completed":
            self.set_code_workflow_states(["complete", "complete", "complete", "complete"])
        self.render_manual_results({"manualResults": manual, "tutorialResults": tutorials, "articleResults": articles})
        self.manual_status_label.set_text(u"当前研究任务已收集 Manual %d · Tutorial %d · 论文 %d。" % (len(manual), len(tutorials), len(articles)))
        if self.current_workspace_execution_id:
            workspace_task_id = self.current_workspace_execution_id
            self.current_workspace_execution_id = None
            start_thread(self.record_workspace_outcome_worker, (
                workspace_task_id,
                u"总体方案已确认，已进入 Tool 证据、参数与代码差异的详细审查。",
                True, result.get("taskId"), result.get("reportPath"), False,
            ))
        return False

    def apply_research_plan_error(self, message):
        self.research_busy = False
        self.code_research_spinner.stop()
        self.code_evidence_progress.set_fraction(0.0)
        self.code_research_button.set_sensitive(True)
        self.code_status_label.set_text(u"方案生成失败")
        self.code_assumptions_buffer.set_text(as_text(message))
        if self.research_plan:
            modification = self.research_plan.get("modification") or {}
            self.code_apply_button.set_sensitive(bool(modification.get("content")) and self.research_plan.get("status") not in ("code-applied", "completed"))
            self.code_report_button.set_sensitive(bool(self.research_plan.get("taskId")))
            self.set_code_stage("review")
        else:
            self.set_code_stage("select")
            self.set_code_workflow_states(["complete", "blocked", "pending", "pending"])
        return False

    # ------------------------------------------------------------------
    # Product-level project understanding and task workspace.

    def on_new_workspace_task(self, *_args):
        if self.workspace_busy or self.ai_busy or self.project_report_loading:
            self.show_message(u"当前任务仍在工作。可以先停止执行，或在任务历史中保存后再开始新任务。", Gtk.MessageType.WARNING)
            return
        self.project_session_started = False
        self.pending_project_read = None
        self.project_report = None
        self.project_report_task_id = None
        self.current_workspace_task = None
        self.selected_workspace_task_id = None
        self.current_workspace_execution_id = None
        self.current_run_id = None
        self.workspace_report_button.set_sensitive(False)
        self.workspace_report_spinner.stop()
        self.workspace_report_status_label.set_text(u"任务记录保存后即可生成报告。")
        self.workspace_complete_advanced_button.set_sensitive(False)
        self.workspace_resume_button.set_sensitive(False)
        self.workspace_unlocked_index = 1
        self.workspace_goal_buffer.set_text(u"")
        self.chat_buffer.set_text(u"")
        self.ai_history = []
        self.set_workspace_stage("select")

    def on_start_onboarding_project(self, *_args):
        index = self.onboarding_project_combo.get_active()
        if index < 0 or index >= len(self.onboarding_project_paths):
            self.show_message(u"请先从列表选择一个 SWB Project。")
            return
        self.start_project_session(self.onboarding_project_paths[index])

    def start_project_session(self, relative):
        if not relative:
            return
        is_new = relative != self.active_project or not self.project_session_started
        self.active_project = relative
        self.project_session_started = True
        self.pending_project_read = relative
        self.last_project_report_name = None
        self.project_report = None
        self.project_report_task_id = None
        self.dashboard_state_stack.set_visible_child_name("session")
        self.workspace_unlocked_index = 1
        self.reset_flow_progress(self.workspace_read_steps)
        self.reset_flow_progress(self.workspace_plan_steps)
        self.set_workspace_stage("reading", unlock=True)
        self.project_understanding_label.set_text(u"等待开始")
        self.project_coverage_label.set_text(u"AI 尚未阅读")
        self.project_coverage_bar.set_fraction(0.03)
        self.project_live_message_label.set_text(u"正在准备工程连接；马上开始清点文件、Tool、参数、节点和已有结果。")
        self.project_elapsed_label.set_text(u"准备中")
        self.workspace_request_revealer.set_reveal_child(False)
        self.workspace_tasks_revealer.set_reveal_child(False)
        self.workspace_plan_button.set_sensitive(False)
        for entry in self.chat_entries:
            entry.set_sensitive(False)
        for send in self.send_buttons:
            send.set_sensitive(False)
        self.assistant_phase_label.set_text(u"准备读取工程")
        if is_new:
            self.chat_buffer.set_text(u"")
            self.ai_history = []
        self.append_chat(u"工程连接", u"已选择 %s；正在建立只读工程会话。" % as_text(relative))
        if relative in self.project_paths:
            self.project_combo.handler_block_by_func(self.on_project_changed)
            self.project_combo.set_active(self.project_paths.index(relative))
            self.project_combo.handler_unblock_by_func(self.on_project_changed)
        try:
            self.dashboard_page_scroll.get_vadjustment().set_value(0.0)
        except Exception:
            pass
        self.refresh(False)

    def on_choose_workspace_project(self, *_args):
        current_status = as_text((self.current_workspace_task or {}).get("status") or u"")
        unfinished = current_status in (
            "planning", "approval-required", "execution-ready", "running", "stopping",
            "specialist-review", "review-required", "evidence-ready", "code-applied",
        )
        if self.workspace_busy or self.ai_busy or self.project_report_loading or unfinished:
            self.show_message(
                u"当前工程仍有未结束的任务。请先完成或停止它；如确实要离开，请使用“结束当前会话并新建”。当前任务不会被静默丢弃。",
                Gtk.MessageType.WARNING,
            )
            return
        dialog = Gtk.FileChooserDialog(
            title=u"选择 SWB 工程目录", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OPEN, Gtk.ResponseType.OK),
        )
        if self.workspace_root and os.path.isdir(self.workspace_root):
            dialog.set_current_folder(self.workspace_root)
        response = dialog.run()
        selected = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not selected:
            return
        root = os.path.realpath(self.workspace_root or "")
        selected = os.path.realpath(selected)
        if not root or (selected != root and not selected.startswith(root + os.sep)):
            self.show_message(u"当前初版只接管受管 STDB 内的工程。请把工程放在 %s 下。" % as_text(root or u"当前工作区"), Gtk.MessageType.WARNING)
            return
        if not os.path.isfile(os.path.join(selected, "gtree.dat")):
            self.show_message(u"所选目录没有 gtree.dat，不是可识别的 SWB 工程。", Gtk.MessageType.WARNING)
            return
        relative = os.path.relpath(selected, root).replace(os.sep, "/")
        if relative not in self.project_paths:
            self.project_paths.append(relative)
            self.project_combo.append_text(relative)
        self.start_project_session(relative)

    def on_read_workspace_project(self, *_args):
        if self.project_report_loading:
            return False
        try:
            project = self.project_absolute_path()
        except Exception as error:
            self.show_message(as_text(error), Gtk.MessageType.ERROR)
            return False
        self.project_report_loading = True
        self.project_read_started_at = time.time()
        self.set_workspace_stage("reading", unlock=True)
        self.reset_flow_progress(self.workspace_read_steps)
        # project_absolute_path() has already verified the managed path and the
        # selected directory's gtree.dat.  This is a fast deterministic check,
        # not an indeterminate model operation.
        self.set_flow_progress(self.workspace_read_steps, "connect", 1.0, u"完成", u"工程路径、gtree.dat 与当前 SWB 会话已经确认。")
        self.set_flow_progress(self.workspace_read_steps, "inventory", 0.08, u"清点中", u"正在统计文件、参数、节点和已有结果。")
        self.project_analysis_buffer.set_text(u"工程连接\n✓ 已确认受管工程路径和 gtree.dat。\n\n接下来\n正在建立真实文件、节点和参数清单。")
        self.project_read_button.set_sensitive(False)
        self.workspace_plan_button.set_sensitive(False)
        for entry in self.chat_entries:
            entry.set_sensitive(False)
        for send in self.send_buttons:
            send.set_sensitive(False)
        self.project_ai_spinner.start()
        self.assistant_phase_label.set_text(u"本机扫描中")
        self.project_understanding_label.set_text(u"本机扫描中")
        self.project_coverage_label.set_text(u"AI 尚未完成")
        self.project_coverage_bar.set_fraction(0.08)
        self.project_live_message_label.set_text(u"正在清点工程文件、Tool 链、参数、节点和已有结果…")
        self.workspace_request_revealer.set_reveal_child(False)
        self.workspace_tasks_revealer.set_reveal_child(False)
        self.append_chat(u"工程读取", u"正在清点工程文件、参数、节点和已有结果…")
        GLib.timeout_add(1000, self.update_project_read_elapsed)
        start_thread(self.project_read_worker, (project, dict(self.current_live_state)))
        return False

    def update_project_read_elapsed(self):
        if not self.project_report_loading or self.project_read_started_at is None:
            return False
        elapsed = max(0, int(time.time() - self.project_read_started_at))
        self.project_elapsed_label.set_text(u"已用时 %02d:%02d" % (elapsed // 60, elapsed % 60))
        # Only genuinely indeterminate operations pulse. Local connection and
        # inventory use deterministic states and never show a misleading loop.
        for key in ("source", "analysis", "report"):
            row = self.workspace_read_steps.get(key)
            if row and as_text(row["status"].get_text()) not in (u"等待", u"完成"):
                self.set_flow_progress(self.workspace_read_steps, key, pulse=True)
                break
        return True

    def reveal_workspace_goal(self):
        try:
            adjustment = self.dashboard_page_scroll.get_vadjustment()
            bottom = max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size())
            adjustment.set_value(bottom)
            self.workspace_goal_view.grab_focus()
        except Exception:
            pass
        return False

    def project_read_worker(self, project, live_state):
        try:
            local_result = core.research_rpc({"action": "project-report", "project": project, "liveState": live_state})
            GLib.idle_add(self.apply_project_local_scan, local_result)
            task = local_result.get("task") or {}
            result = core.ai_rpc(
                {"action": "analyze-project", "taskId": task.get("taskId")},
                lambda event: GLib.idle_add(self.apply_workspace_ai_event, event),
            )
            GLib.idle_add(self.apply_project_read_report, result)
        except Exception as error:
            GLib.idle_add(self.apply_project_read_error, error_text(error))

    def apply_project_local_scan(self, result):
        self.project_report = result.get("report") or {}
        task = result.get("task") or {}
        self.project_report_task_id = task.get("taskId")
        coverage = max(0, min(100, int(self.project_report.get("coverage") or 0)))
        counts = self.project_report.get("counts") or {}
        self.set_flow_progress(self.workspace_read_steps, "connect", 1.0, u"完成", u"工程路径和 SWB 状态已确认。")
        self.set_flow_progress(
            self.workspace_read_steps, "inventory", 1.0, u"完成",
            u"已清点 %d 个文件、%d 个用户源文件、%d 个参数、%d 个节点。" % (
                int(counts.get("files") or 0), int(counts.get("sourceFiles") or 0),
                int(counts.get("parameters") or 0), int(counts.get("nodes") or 0),
            ),
        )
        self.set_flow_progress(self.workspace_read_steps, "source", 0.08, u"读取中", u"正在逐个读取可维护的 Tool 源文件并建立依赖关系。")
        self.project_understanding_label.set_text(u"本机扫描完成 · AI 阅读中")
        self.project_coverage_label.set_text(u"工程清点 %d%% · AI 尚未完成" % coverage)
        self.project_coverage_bar.set_fraction(0.35)
        self.project_live_message_label.set_text(
            u"本机清点完成：%d 个文件、%d 个用户源文件、%d 个参数、%d 个节点。正在调用 AI 深度阅读。" % (
                int(counts.get("files") or 0), int(counts.get("sourceFiles") or 0),
                int(counts.get("parameters") or 0), int(counts.get("nodes") or 0),
            )
        )
        self.append_chat(u"工程读取", u"本机扫描完成：%d 个文件、%d 个用户源文件、%d 个参数、%d 个节点。现在开始调用 AI 深度阅读。" % (
            int(counts.get("files") or 0), int(counts.get("sourceFiles") or 0),
            int(counts.get("parameters") or 0), int(counts.get("nodes") or 0),
        ))
        self.append_project_analysis(u"本机清点", u"✓ %d 个文件 · %d 个用户源文件 · %d 个参数 · %d 个节点。" % (
            int(counts.get("files") or 0), int(counts.get("sourceFiles") or 0),
            int(counts.get("parameters") or 0), int(counts.get("nodes") or 0),
        ))
        return False

    def apply_workspace_ai_event(self, event):
        kind = as_text(event.get("event") or u"progress")
        message = as_text(event.get("message") or u"")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not message:
            return False
        names = {
            "project_scan": u"本机扫描", "project_source": u"源文件阅读",
            "analysis_scope": u"分析清单", "analysis_findings": u"结构化判断",
            "thinking": u"深度思考", "reasoning": u"推理摘要", "evidence": u"证据检索",
            "project_ready": u"工程接手完成", "plan": u"任务方案",
        }
        self.append_chat(names.get(kind, u"AI 进度"), message)
        self.assistant_phase_label.set_text(names.get(kind, u"AI 正在工作"))
        if self.workspace_flow_stage == "plan":
            if kind == "evidence":
                self.set_flow_progress(self.workspace_plan_steps, "goal", 1.0, u"完成")
                self.set_flow_progress(self.workspace_plan_steps, "evidence", pulse=True, status=u"检索中", detail=message)
            elif kind == "thinking":
                self.set_flow_progress(self.workspace_plan_steps, "goal", 1.0, u"完成")
                if as_text(self.workspace_plan_steps["evidence"]["status"].get_text()) == u"等待":
                    self.set_flow_progress(self.workspace_plan_steps, "evidence", pulse=True, status=u"检查中", detail=message)
            elif kind == "reasoning":
                self.set_flow_progress(self.workspace_plan_steps, "evidence", 1.0, u"完成")
                self.set_flow_progress(self.workspace_plan_steps, "parameters", pulse=True, status=u"识别中", detail=message)
                self.set_flow_progress(self.workspace_plan_steps, "route", pulse=True, status=u"规划中")
            elif kind == "plan":
                for key in ("goal", "evidence", "parameters", "route", "risk"):
                    self.set_flow_progress(self.workspace_plan_steps, key, 1.0, u"完成")
            return False
        if kind == "analysis_scope":
            items = data.get("items") if isinstance(data.get("items"), list) else []
            detail = message
            if items:
                detail += u"\n" + u"\n".join(u"• " + as_text(value) for value in items)
            self.append_project_analysis(u"提交给模型的分析清单", detail)
        elif kind == "analysis_findings":
            self.append_project_analysis(
                u"模型返回的结构化检查",
                u"文件角色 %s · Tool 链 %s · 参数 %s · 已有结果 %s · 风险 %s · 未知项 %s\n%s" % (
                    data.get("fileRoles") or 0, data.get("toolchain") or 0,
                    data.get("parameters") or 0, data.get("existingResults") or 0,
                    data.get("risks") or 0, data.get("unknowns") or 0, message,
                ),
            )
        elif kind == "reasoning":
            token_text = u" · reasoning %s tokens" % data.get("reasoningTokens") if data.get("reasoningTokens") else u""
            self.append_project_analysis(u"可审计推理摘要" + token_text, message)
        self.project_live_message_label.set_text(message)
        progress = {
            "project_scan": 0.40, "project_source": 0.55,
            "thinking": 0.72, "reasoning": 0.90, "project_ready": 0.98,
        }.get(kind)
        if progress is not None:
            self.project_coverage_bar.set_fraction(progress)
        if kind == "project_scan":
            self.set_flow_progress(self.workspace_read_steps, "inventory", 1.0, u"完成")
            self.set_flow_progress(self.workspace_read_steps, "source", 0.12, u"读取中", message)
        elif kind == "project_source":
            self.set_flow_progress(self.workspace_read_steps, "source", pulse=True, status=u"读取中", detail=message)
        if kind == "thinking":
            self.project_understanding_label.set_text(u"AI 深度思考中")
            self.set_flow_progress(self.workspace_read_steps, "source", 1.0, u"完成", u"Tool 源文件和依赖关系已送入模型上下文。")
            self.set_flow_progress(self.workspace_read_steps, "analysis", pulse=True, status=u"深度分析中", detail=message)
        elif kind == "reasoning":
            used = bool(data.get("reasoningUsed"))
            tokens = data.get("reasoningTokens")
            if used:
                self.assistant_phase_label.set_text(u"深度思考已使用%s" % (u" · %s tokens" % tokens if tokens else u""))
            else:
                self.assistant_phase_label.set_text(u"模型未返回深度思考标记")
            self.set_flow_progress(self.workspace_read_steps, "analysis", 0.92, u"整理结论", message)
        elif kind == "project_ready":
            self.assistant_phase_label.set_text(u"AI 工程阅读完成")
            self.set_flow_progress(self.workspace_read_steps, "analysis", 1.0, u"完成", message)
            self.set_flow_progress(self.workspace_read_steps, "report", pulse=True, status=u"生成中", detail=u"正在组织工程概况、器件意图、参数、结果与未知项。")
        return False

    def project_report_text(self, report):
        counts = report.get("counts") or {}
        analysis = report.get("aiAnalysis") or {}
        if analysis:
            lines = [as_text(analysis.get("overview") or u"AI 工程阅读完成。"), u""]
            if analysis.get("deviceIntent"):
                lines.extend([u"器件与工程意图", as_text(analysis.get("deviceIntent")), u""])
            lines.append(u"AI 确认的 Tool 链")
            lines.extend(u"• " + as_text(value) for value in analysis.get("toolchain") or [])
            if analysis.get("unknowns"):
                lines.extend([u"", u"仍然未知"])
                lines.extend(u"○ " + as_text(value) for value in analysis.get("unknowns") or [])
            return u"\n".join(lines)
        lines = [as_text(report.get("summary") or u"本机工程扫描完成，AI 尚未阅读。"), u""]
        lines.append(u"本机已清点")
        lines.extend(u"✓ " + as_text(value) for value in report.get("understood") or [])
        lines.append(u"")
        tools = report.get("tools") or []
        lines.append(u"Tool 链")
        if tools:
            for item in tools:
                lines.append(u"• %s：%s" % (as_text(item.get("label")), u"、".join(as_text(value) for value in (item.get("files") or [])[:4])))
        else:
            lines.append(u"• 尚未从源文件中识别")
        lines.extend([u"", u"当前快照"])
        lines.append(u"• %d 个源文件 · %d 个参数 · %d 个节点 · %d 个已有结果/生成文件" % (
            int(counts.get("sourceFiles") or 0), int(counts.get("parameters") or 0),
            int(counts.get("nodes") or 0), int(counts.get("resultFiles") or 0),
        ))
        if report.get("warnings"):
            lines.extend([u"", u"需要注意"])
            lines.extend(u"! " + as_text(value) for value in report.get("warnings") or [])
        lines.extend([u"", u"还需要你告诉我"])
        lines.extend(u"○ " + as_text(value) for value in report.get("notYetUnderstood") or [])
        return u"\n".join(lines)

    def apply_project_read_report(self, result):
        elapsed = max(0, int(time.time() - self.project_read_started_at)) if self.project_read_started_at else 0
        self.project_report_loading = False
        self.project_ai_spinner.stop()
        self.project_read_button.set_sensitive(True)
        self.project_report = result.get("report") or {}
        task = result.get("task") or {}
        self.project_report_task_id = task.get("taskId")
        self.last_project_report_name = self.active_project
        local_coverage = max(0, min(100, int(self.project_report.get("coverage") or 0)))
        source_coverage = max(0, min(100, int(self.project_report.get("aiSourceCoverage") or 0)))
        reasoning_used = bool(result.get("reasoningUsed") or (task.get("reasoning") or {}).get("used"))
        reasoning_tokens = result.get("reasoningTokens") or (task.get("reasoning") or {}).get("tokens")
        self.project_understanding_label.set_text(u"AI 深度阅读完成")
        reasoning_text = u"深度思考已使用" if reasoning_used else u"模型未返回思考标记"
        if reasoning_used and reasoning_tokens:
            reasoning_text += u" · %s tokens" % reasoning_tokens
        self.project_coverage_label.set_text(u"源文件阅读 %d%% · 工程清点 %d%% · %s" % (source_coverage, local_coverage, reasoning_text))
        self.project_coverage_bar.set_fraction(1.0)
        self.project_live_message_label.set_text(
            u"工程阅读完成：AI 已分析 %d%% 的已识别源文件。现在可以在下方描述任务，我会先生成方案供你确认。" % source_coverage
        )
        self.project_report_buffer.set_text(self.project_report_text(self.project_report))
        self.set_flow_progress(self.workspace_read_steps, "connect", 1.0, u"完成")
        self.set_flow_progress(self.workspace_read_steps, "inventory", 1.0, u"完成")
        self.set_flow_progress(self.workspace_read_steps, "source", 1.0, u"完成")
        self.set_flow_progress(self.workspace_read_steps, "analysis", 1.0, u"完成")
        self.set_flow_progress(self.workspace_read_steps, "report", 1.0, u"完成", u"工程接手报告已经生成。")
        self.populate_understanding_summary(self.project_report)
        analysis = result.get("analysis") or self.project_report.get("aiAnalysis") or {}
        lines = [as_text(analysis.get("overview") or u"AI 已完成工程阅读。")]
        if analysis.get("deviceIntent"):
            lines.extend([u"", u"器件与工程意图：", as_text(analysis.get("deviceIntent"))])
        if analysis.get("toolchain"):
            lines.extend([u"", u"Tool 链："] + [u"• " + as_text(value) for value in analysis.get("toolchain") or []])
        if analysis.get("reasoningSummary"):
            lines.extend([u"", u"可审计推理摘要：", as_text(analysis.get("reasoningSummary"))])
        if analysis.get("unknowns"):
            lines.extend([u"", u"我仍然不知道："] + [u"• " + as_text(value) for value in analysis.get("unknowns") or []])
        if analysis.get("suggestedQuestions"):
            lines.extend([u"", u"你现在可以继续告诉我："] + [u"• " + as_text(value) for value in analysis.get("suggestedQuestions") or []])
        self.append_chat(u"EmberTCAD · 工程接手报告", u"\n".join(lines))
        self.assistant_phase_label.set_text(u"等待你的任务")
        self.project_elapsed_label.set_text(u"阅读完成 · %02d:%02d" % (elapsed // 60, elapsed % 60))
        self.workspace_plan_button.set_sensitive(True)
        self.workspace_request_revealer.set_reveal_child(True)
        self.workspace_tasks_revealer.set_reveal_child(False)
        for entry in self.chat_entries:
            entry.set_sensitive(True)
        for send in self.send_buttons:
            send.set_sensitive(True)
        self.on_refresh_workspace_tasks()
        self.set_workspace_stage("goal", unlock=True)
        if self.pending_repair_goal:
            self.workspace_goal_buffer.set_text(as_text(self.pending_repair_goal))
            self.project_live_message_label.set_text(u"已把刚才的真实 Tool 错误带入任务框；请核对后生成修复方案。")
            self.pending_repair_goal = None
        GLib.timeout_add(220, self.reveal_workspace_goal)
        return False

    def apply_project_read_error(self, message):
        elapsed = max(0, int(time.time() - self.project_read_started_at)) if self.project_read_started_at else 0
        self.project_report_loading = False
        self.project_ai_spinner.stop()
        self.project_read_button.set_sensitive(True)
        self.workspace_plan_button.set_sensitive(False)
        for entry in self.chat_entries:
            entry.set_sensitive(False)
        for send in self.send_buttons:
            send.set_sensitive(False)
        self.project_understanding_label.set_text(u"AI 工程阅读未完成")
        self.project_coverage_label.set_text(u"请检查模型连接后重试")
        self.assistant_phase_label.set_text(u"读取失败")
        self.project_elapsed_label.set_text(u"停止于 %02d:%02d" % (elapsed // 60, elapsed % 60))
        self.project_live_message_label.set_text(u"AI 阅读失败：%s" % as_text(message))
        for key in ("report", "analysis", "source", "inventory", "connect"):
            row = self.workspace_read_steps.get(key)
            if row and as_text(row["status"].get_text()) not in (u"等待", u"完成"):
                self.set_flow_progress(self.workspace_read_steps, key, status=u"失败", detail=as_text(message))
                break
        self.workspace_request_revealer.set_reveal_child(False)
        self.workspace_tasks_revealer.set_reveal_child(False)
        self.project_report_buffer.set_text(u"本机扫描可能已经完成，但 AI 没有完成工程理解：\n%s" % as_text(message))
        self.append_chat(u"EmberTCAD · 读取失败", u"AI 没有完成工程阅读，因此不会把本机扫描冒充为 AI 理解。\n%s\n请检查“设置”，然后点击“重新阅读工程”。" % as_text(message))
        self.mark_workspace_stage_failed("reading")
        return False

    def on_create_workspace_task(self, *_args):
        if self.workspace_busy:
            return
        if not self.project_report or self.project_report.get("aiStatus") != "complete":
            self.show_message(u"AI 还没有完成工程阅读。请等待完成，或点击“重新阅读工程”。", Gtk.MessageType.WARNING)
            return
        goal = self.text_buffer_value(self.workspace_goal_buffer)
        if not goal:
            self.show_message(u"请先描述希望 AI 完成的任务。")
            return
        try:
            project = self.project_absolute_path()
        except Exception as error:
            self.show_message(as_text(error), Gtk.MessageType.ERROR)
            return
        self.workspace_busy = True
        self.set_workspace_stage("plan", unlock=True)
        self.workspace_plan_mode_stack.set_visible_child_name("planning")
        self.reset_flow_progress(self.workspace_plan_steps)
        self.set_flow_progress(self.workspace_plan_steps, "goal", 0.18, u"解析中", u"正在识别目标指标、变量、固定条件与验收标准。")
        self.workspace_plan_button.set_sensitive(False)
        self.conversation_approve_button.set_sensitive(False)
        self.workspace_plan_spinner.start()
        self.assistant_phase_label.set_text(u"正在理解你的任务")
        self.project_live_message_label.set_text(u"正在结合工程接手报告理解你的目标，并生成可审查方案…")
        self.append_chat(u"你", goal)
        self.append_chat(u"EmberTCAD", u"我会先结合工程接手报告进行深度分析并给出总体方案。确认前不会修改或运行工程。")
        start_thread(self.workspace_plan_worker, (project, goal, self.project_report_task_id, dict(self.current_live_state)))

    def workspace_plan_worker(self, project, goal, report_task_id, live_state):
        try:
            created = core.research_rpc({
                "action": "create-workspace-task", "project": project, "goal": goal,
                "projectReportTaskId": report_task_id, "liveState": live_state,
            })
            task = created.get("task") or {}
            result = core.ai_rpc(
                {"action": "plan-workspace-task", "taskId": task.get("taskId")},
                lambda event: GLib.idle_add(self.apply_workspace_ai_event, event),
            )
            GLib.idle_add(self.apply_workspace_plan, result.get("task") or task)
        except Exception as error:
            GLib.idle_add(self.apply_workspace_plan_error, error_text(error))

    def apply_workspace_plan(self, task):
        self.workspace_busy = False
        self.workspace_plan_spinner.stop()
        self.workspace_plan_button.set_sensitive(True)
        self.assistant_phase_label.set_text(u"方案等待确认")
        self.project_live_message_label.set_text(u"总体方案已经生成。工程尚未修改，请先审查方案和风险，再决定是否执行。")
        self.selected_workspace_task_id = task.get("taskId")
        for key in ("goal", "evidence", "parameters", "route", "risk"):
            self.set_flow_progress(self.workspace_plan_steps, key, 1.0, u"完成")
        self.render_workspace_task_detail(task)
        self.workspace_plan_mode_stack.set_visible_child_name("review")
        self.set_workspace_stage("plan", unlock=True)
        self.on_refresh_workspace_tasks()
        lines = [as_text(task.get("summary") or u"总体方案已生成。"), u""]
        if task.get("planSteps"):
            lines.append(u"执行步骤")
            lines.extend(u"%d. %s" % (index + 1, as_text(value)) for index, value in enumerate(task.get("planSteps") or []))
        if task.get("expectedOutputs"):
            lines.extend([u"", u"预计交付"] + [u"• " + as_text(value) for value in task.get("expectedOutputs") or []])
        if task.get("risks"):
            lines.extend([u"", u"风险与审批门"] + [u"• " + as_text(value) for value in task.get("risks") or []])
        lines.extend([u"", u"工程尚未修改。请审查后点击“确认方案并开始执行”。"])
        self.append_chat(u"EmberTCAD · 总体方案", u"\n".join(lines))
        return False

    def apply_workspace_plan_error(self, message):
        self.workspace_busy = False
        self.workspace_plan_spinner.stop()
        self.workspace_plan_button.set_sensitive(True)
        self.assistant_phase_label.set_text(u"方案生成失败")
        for key in ("risk", "route", "parameters", "evidence", "goal"):
            row = self.workspace_plan_steps.get(key)
            if row and as_text(row["status"].get_text()) not in (u"等待", u"完成"):
                self.set_flow_progress(self.workspace_plan_steps, key, status=u"失败", detail=as_text(message))
                break
        self.append_chat(u"EmberTCAD · 方案失败", as_text(message))
        self.show_message(u"任务方案生成失败：%s" % as_text(message), Gtk.MessageType.ERROR)
        self.mark_workspace_stage_failed("plan")
        self.on_refresh_workspace_tasks()
        return False

    def on_refresh_workspace_tasks(self, *_args):
        start_thread(self.workspace_tasks_worker)

    def workspace_tasks_worker(self):
        try:
            result = core.research_rpc({"action": "history", "limit": 100})
            GLib.idle_add(self.apply_workspace_tasks, result.get("tasks") or [])
        except Exception as error:
            GLib.idle_add(self.apply_workspace_tasks_error, error_text(error))

    @staticmethod
    def workspace_status_name(value):
        return {
            "planning": u"规划中", "approval-required": u"待确认", "execution-ready": u"准备执行",
            "running": u"执行中", "stopping": u"停止中", "cancelled": u"已停止",
            "interrupted": u"已中断",
            "specialist-review": u"详细审查", "review-required": u"待审查", "evidence-ready": u"证据就绪",
            "code-applied": u"代码已应用", "completed": u"已完成", "failed": u"失败",
            "understood": u"工程已理解",
        }.get(as_text(value), as_text(value or u"—"))

    def apply_workspace_tasks(self, tasks):
        self.workspace_tasks = tasks
        self.workspace_task_store.clear()
        self.report_task_store.clear()
        active_statuses = ("planning", "approval-required", "execution-ready", "running", "stopping", "specialist-review", "review-required", "evidence-ready", "code-applied")
        task_rows = [
            item for item in tasks
            if item.get("kind") != "project-understanding" and item.get("status") in active_statuses
        ]
        for item in task_rows:
            self.workspace_task_store.append((
                self.workspace_status_name(item.get("status")),
                as_text(item.get("title") or item.get("question") or item.get("summary") or u"EmberTCAD 任务"),
                as_text(item.get("project") or u"—"), as_text(item.get("stage") or u"—"),
                u"%d%%" % int(item.get("progress") or 0), as_text(item.get("taskId") or u""),
            ))
        for item in tasks:
            updated = as_text(item.get("updatedAt") or item.get("createdAt") or u"").replace("T", " ").replace("Z", "")[:16]
            self.report_task_store.append((
                self.workspace_status_name(item.get("status")),
                as_text(item.get("title") or item.get("question") or item.get("summary") or u"EmberTCAD 成果"),
                as_text(item.get("project") or u"—"), as_text(item.get("stage") or item.get("kind") or u"—"),
                updated, as_text(item.get("taskId") or u""),
            ))
        running_count = len([item for item in task_rows if item.get("status") in ("running", "stopping")])
        self.workspace_active_count_label.set_text(u"%d 个运行任务" % running_count)
        self.workspace_project_count_label.set_text(as_text(len(self.project_paths)))
        self.workspace_report_count_label.set_text(as_text(len([item for item in tasks if item.get("status") == "completed" or item.get("reportPath")])))
        reading_complete = bool(self.project_report and self.project_report.get("aiStatus") == "complete")
        self.workspace_tasks_revealer.set_reveal_child(False)
        return False

    def apply_workspace_tasks_error(self, message):
        self.project_report_buffer.set_text(u"任务库读取失败：%s" % as_text(message))
        return False

    def on_workspace_task_selected(self, selection):
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        self.selected_workspace_task_id = model.get_value(iterator, 5)
        start_thread(self.workspace_task_detail_worker, (self.selected_workspace_task_id, False))

    def workspace_task_detail_worker(self, task_id, for_results=False):
        try:
            result = core.research_rpc({"action": "task", "taskId": task_id})
            callback = self.render_result_task if for_results else self.render_workspace_task_detail
            GLib.idle_add(callback, result.get("task") or {})
        except Exception as error:
            GLib.idle_add(self.show_message, as_text(error), Gtk.MessageType.ERROR)

    def add_workspace_detail_card(self, heading, lines, css="card"):
        values = [as_text(value) for value in lines if as_text(value).strip()]
        if not values:
            return
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        box.pack_start(label(heading, "small-muted"), False, False, 0)
        box.pack_start(label(u"\n".join(values), "muted", True), False, False, 0)
        self.workspace_detail_cards.pack_start(card(box, css, 10), False, False, 0)

    def render_workspace_task_detail(self, task):
        self.current_workspace_task = task
        self.selected_workspace_task_id = task.get("taskId")
        self.workspace_detail_title_label.set_text(as_text(task.get("title") or task.get("question") or u"EmberTCAD 任务"))
        self.workspace_detail_status_label.set_text(
            u"工程专属 · %s" % self.workspace_status_name(task.get("status"))
        )
        self.workspace_detail_progress.set_fraction(max(0.0, min(1.0, float(task.get("progress") or 0) / 100.0)))
        for child in self.workspace_detail_cards.get_children():
            self.workspace_detail_cards.remove(child)
        self.add_workspace_detail_card(u"当前结论", [task.get("summary") or u"等待任务信息。"], "task-card")
        workflow_lines = []
        stage_names = {
            "evidence": u"证据检索", "model": u"物理模型", "parameters": u"参数与来源",
            "patch": u"代码差异", "validation": u"运行验证", "calibration": u"标定",
            "simulation": u"仿真", "read": u"理解工程", "goal": u"明确目标",
            "plan": u"生成方案", "approval": u"用户确认", "execute": u"执行与验证",
            "report": u"成果归档",
        }
        for item in task.get("workflow") or []:
            mark = {"complete": u"✓", "active": u"●", "review": u"●", "pending": u"○", "blocked": u"!"}.get(item.get("status") or item.get("state"), u"○")
            stage_id = as_text(item.get("id") or u"")
            workflow_lines.append(u"%s %s" % (mark, as_text(item.get("name") or item.get("label") or stage_names.get(stage_id) or stage_id)))
        self.add_workspace_detail_card(u"任务过程", workflow_lines)
        self.add_workspace_detail_card(u"执行方案", [u"%d. %s" % (index + 1, as_text(value)) for index, value in enumerate(task.get("planSteps") or [])])
        self.add_workspace_detail_card(u"预计交付", [u"• " + as_text(value) for value in task.get("expectedOutputs") or []])
        self.add_workspace_detail_card(u"假设与风险", [u"• " + as_text(value) for value in (task.get("assumptions") or []) + (task.get("risks") or [])], "task-card")
        specialist_preview = task.get("specialistPreview") or {}
        if specialist_preview:
            preview_lines = []
            if specialist_preview.get("physicalModel"):
                preview_lines.append(as_text(specialist_preview.get("physicalModel")))
            if specialist_preview.get("relativePath"):
                preview_lines.append(u"目标文件：%s" % as_text(specialist_preview.get("relativePath")))
            preview_lines.extend(u"参数：%s" % as_text(value) for value in specialist_preview.get("parameters") or [])
            self.add_workspace_detail_card(u"物理模型与动态参数", preview_lines, "task-card")
            if specialist_preview.get("diff"):
                diff_text = as_text(specialist_preview.get("diff"))
                if len(diff_text) > 6000:
                    diff_text = diff_text[:6000] + u"\n…完整差异可从高级详情查看"
                self.add_workspace_detail_card(u"待批准代码差异", [diff_text])
            self.add_workspace_detail_card(u"证据索引", [u"• " + as_text(value) for value in specialist_preview.get("evidence") or []])
        for section in task.get("adaptiveSections") or []:
            self.add_workspace_detail_card(as_text(section.get("title") or u"工程专属信息"), [u"• " + as_text(value) for value in section.get("items") or []], "task-card" if section.get("type") == "warning" else "card")
        self.workspace_detail_cards.show_all()
        self.render_workspace_plan_inputs(task)
        can_execute = task.get("kind") == "workspace-task" and task.get("status") in ("approval-required", "execution-ready") and not self.workspace_busy
        self.workspace_approve_button.set_sensitive(can_execute)
        self.conversation_approve_button.set_sensitive(can_execute)
        self.workspace_specialist_button.set_sensitive(False)
        return False

    def open_selected_workspace_task(self, *_args):
        task = self.current_workspace_task or {}
        if task.get("kind") in ("tool-model", "tool-research", "sdevice-tid"):
            self.open_history_task_result(task)
            return
        task_type = task.get("taskType")
        if task_type == "project-create" and task.get("projectPath"):
            self.show_page("dashboard")
            self.start_project_session(as_text(task.get("project")))
        elif task_type == "tool-change" or task.get("linkedTaskId"):
            self.append_chat(u"EmberTCAD", u"代码差异、依据和执行状态保留在当前工作台；完整记录可从任务历史查看。")
        elif task:
            self.append_chat(u"EmberTCAD", as_text(task.get("summary") or u"任务详情已载入。"))

    def on_approve_workspace_task(self, *_args):
        task = self.current_workspace_task or {}
        if self.workspace_busy or task.get("status") not in ("approval-required", "execution-ready"):
            return
        self.workspace_busy = True
        self.workspace_approve_button.set_sensitive(False)
        self.conversation_approve_button.set_sensitive(False)
        self.assistant_phase_label.set_text(u"方案已确认 · 准备执行")
        self.append_chat(u"你", u"确认总体方案，开始进入执行与验证。")
        start_thread(self.approve_workspace_task_worker, (task.get("taskId"), self.collect_workspace_plan_inputs()))

    def approve_workspace_task_worker(self, task_id, approved_inputs):
        try:
            result = core.research_rpc({
                "action": "approve-workspace-task", "taskId": task_id,
                "approvedInputs": approved_inputs,
            })
            GLib.idle_add(self.apply_workspace_task_approval, result.get("task") or {})
        except Exception as error:
            GLib.idle_add(self.apply_workspace_plan_error, error_text(error))

    def apply_workspace_task_approval(self, task):
        self.render_workspace_task_detail(task)
        self.workspace_busy = True
        self.assistant_phase_label.set_text(u"正在取得独占执行权")
        start_thread(self.begin_workspace_execution_worker, (task.get("taskId"),))
        return False

    def begin_workspace_execution_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "begin-workspace-execution", "taskId": task_id})
            GLib.idle_add(self.apply_workspace_execution_started, result.get("task") or {})
        except Exception as error:
            GLib.idle_add(self.apply_workspace_plan_error, error_text(error))

    def apply_workspace_execution_started(self, task):
        self.workspace_busy = False
        self.ai_busy = True
        self.execution_stopping = False
        self.current_workspace_task = task
        self.selected_workspace_task_id = task.get("taskId")
        self.current_workspace_execution_id = task.get("taskId")
        self.current_run_id = task.get("runId")
        self.workspace_stop_button.set_sensitive(True)
        self.workspace_resume_button.set_sensitive(False)
        self.workspace_complete_advanced_button.set_sensitive(False)
        question = as_text(task.get("question") or u"")
        self.reset_task_dashboard(question)
        self.set_workspace_stage("execute", unlock=True)
        self.set_task_state(u"执行已启动", "success")
        self.set_task_detail(u"已取得唯一执行槽；可以随时点击“停止运行”立即终止 AI Helper 与当前 gsub 进程组。")
        self.set_task_progress(text=u"STARTING", pulse=True)
        if self.execution_pulse_source is None:
            self.execution_pulse_source = GLib.timeout_add(180, self.pulse_workspace_execution)
        self.assistant_phase_label.set_text(u"执行与验证")
        self.append_chat(u"EmberTCAD · 执行", u"任务已启动，runId=%s。没有固定迭代次数上限。" % as_text(self.current_run_id))
        for send in self.send_buttons:
            send.set_sensitive(False)
        if task.get("taskType") == "tool-change" and task.get("linkedTaskId"):
            start_thread(self.workspace_tool_change_worker, (dict(task),))
        else:
            start_thread(self.ai_chat_worker, (
                question, list(self.ai_history), task.get("taskId"), task.get("runId"),
                dict(task.get("approvedInputs") or {}),
            ))
        self.on_refresh_workspace_tasks()
        return False

    def pulse_workspace_execution(self):
        if not self.ai_busy or self.workspace_flow_stage != "execute":
            self.execution_pulse_source = None
            return False
        for progress in self.task_progress_bars:
            progress.pulse()
        for row in self.execution_phase_widgets.values():
            if as_text(row["status"].get_text()) == u"运行中":
                row["bar"].pulse()
        return True

    def workspace_tool_change_worker(self, workspace_task):
        task_id = workspace_task.get("taskId")
        run_id = workspace_task.get("runId")
        try:
            loaded = core.research_rpc({"action": "task", "taskId": workspace_task.get("linkedTaskId")})
            plan = loaded.get("task") or {}
            if plan.get("status") not in ("code-applied", "completed"):
                GLib.idle_add(self.apply_ai_event, {
                    "event": "step", "message": u"正在校验源文件 SHA-256 并创建可恢复备份。",
                    "data": {"phase": "materialize"},
                })
                modification = plan.get("modification") or {}
                if not modification.get("content"):
                    raise RuntimeError("详细方案没有可应用的代码差异")
                modifications = plan.get("modifications") or [modification]
                write_plans = []
                for item in modifications:
                    write_plan = core.rpc("file.planWrite", {
                        "relativePath": item.get("relativePath"), "content": item.get("content"),
                    })
                    if write_plan.get("originalSha256") != item.get("sourceSha256"):
                        raise RuntimeError("%s 在方案生成后发生变化；没有应用任何代码，请重新规划" % item.get("relativePath"))
                    write_plans.append(write_plan)
                project = self.project_absolute_path()
                live_state = core.read_live_swb_state(project)
                existing = dict((item.get("name"), item) for item in live_state.get("parameters") or [])
                approved_inputs = workspace_task.get("approvedInputs") or {}
                added = []
                already = []
                for item in plan.get("parameterPlan") or plan.get("modelParameters") or []:
                    name = str(item.get("name") or "")
                    if not name or not item.get("bindToSwb", True):
                        continue
                    if name in existing:
                        already.append(name)
                        continue
                    step = item.get("step")
                    if step is None:
                        raise RuntimeError("无法确认 %s 参数所属 Tool 步骤；没有修改工程" % name)
                    value = approved_inputs.get(name, item.get("value"))
                    core.live_action("add-parameter", project, name=name, value=str(value if value not in (None, "") else "0"), step=int(step))
                    added.append(name)
                write_results = []
                try:
                    for item, write_plan in zip(modifications, write_plans):
                        write_results.append(core.rpc("file.writeText", {
                            "relativePath": item.get("relativePath"), "content": item.get("content"),
                            "approvalToken": write_plan.get("approvalToken"), "confirm": True,
                        }))
                except Exception as write_error:
                    rollback_errors = []
                    for prior in reversed(write_results):
                        try:
                            restore = core.rpc("file.planRestore", {"backupId": prior.get("backupId")})
                            if restore.get("currentSha256") != prior.get("updatedSha256"):
                                raise RuntimeError("文件已被其他操作修改，拒绝自动回滚")
                            core.rpc("file.restoreBackup", {"backupId": prior.get("backupId"),
                                      "approvalToken": restore.get("approvalToken"), "confirm": True})
                        except Exception as rollback_error:
                            rollback_errors.append(u"%s：%s" % (as_text(prior.get("relativePath")), error_text(rollback_error)))
                    raise RuntimeError(u"多文件写入失败；已回滚成功写入的文件。%s%s" % (
                        error_text(write_error), u" 未能自动回滚：" + u"；".join(rollback_errors) if rollback_errors else u""))
                core.live_action("refresh-swb", project)
                recorded = core.research_rpc({
                    "action": "record-apply", "taskId": plan.get("taskId"),
                    "writeResult": write_results[0], "writeResults": write_results, "parametersAdded": added,
                    "parametersExisting": already,
                })
                plan = recorded.get("task") or plan
                GLib.idle_add(self.apply_ai_event, {
                    "event": "result",
                    "message": u"代码已原子写入并建立可恢复备份；开始运行真实 Tool 节点。",
                    "data": {"phase": "materialize"},
                })
            result = core.ai_rpc({
                "action": "run-generic-research-task", "taskId": plan.get("taskId"),
                "project": self.project_absolute_path(), "node": "",
                "workspaceTaskId": task_id, "runId": run_id,
            }, lambda event: GLib.idle_add(self.apply_ai_event, event), self.set_active_ai_process)
            GLib.idle_add(self.apply_ai_response, workspace_task.get("question") or u"Tool 修改任务", result, task_id, run_id)
        except Exception as error:
            GLib.idle_add(self.apply_ai_error, error_text(error), task_id, run_id)

    def record_workspace_outcome_worker(self, task_id, summary, specialist, linked_task_id, report_path, failed, run_id=None):
        try:
            result = core.research_rpc({
                "action": "record-workspace-outcome", "taskId": task_id,
                "summary": summary, "specialistReview": bool(specialist),
                "linkedTaskId": linked_task_id, "reportPath": report_path, "failed": bool(failed),
                "runId": run_id,
            })
            GLib.idle_add(self.apply_recorded_workspace_outcome, result.get("task") or {})
            GLib.idle_add(self.on_refresh_workspace_tasks)
        except Exception as error:
            GLib.idle_add(self.apply_recorded_workspace_outcome_error, task_id, error_text(error))

    def apply_recorded_workspace_outcome(self, task):
        if task.get("taskId") and (
            not self.current_workspace_task or
            task.get("taskId") == self.current_workspace_task.get("taskId")
        ):
            self.current_workspace_task = task
            if self.workspace_flow_stage == "complete":
                self.workspace_report_spinner.stop()
                self.workspace_report_button.set_sensitive(True)
                self.workspace_report_status_label.set_text(
                    u"任务记录已保存。报告会写入 EmberTCAD 本机数据目录。"
                )
        return False

    def apply_recorded_workspace_outcome_error(self, task_id, message):
        if self.current_workspace_task and task_id == self.current_workspace_task.get("taskId"):
            self.workspace_report_spinner.stop()
            self.workspace_report_button.set_sensitive(False)
            self.workspace_report_status_label.set_text(u"任务记录保存失败：%s" % as_text(message))
        return False

    def on_refresh_history(self, *_args):
        self.history_status_label.set_text(u"正在读取本机任务库…")
        start_thread(self.history_worker)

    def on_clear_history(self, *_args):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=u"清空 EmberTCAD 任务历史？",
        )
        dialog.format_secondary_text(
            u"将删除任务记录及 EmberTCAD 生成的报告。不会删除 SWB 工程、源文件、模型设置或手册索引，"
            u"也不会停止仍有真实进程的任务。此操作无法撤销。"
        )
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not approved:
            return
        self.history_clear_button.set_sensitive(False)
        self.history_status_label.set_text(u"正在清理任务记录与报告…")
        start_thread(self.clear_history_worker)

    def clear_history_worker(self):
        try:
            cleared = core.research_rpc({"action": "clear-history"})
            current = core.research_rpc({"action": "project-history"})
            current.update(cleared)
            GLib.idle_add(self.apply_cleared_history, current)
        except Exception as error:
            GLib.idle_add(self.apply_clear_history_error, error_text(error))

    def apply_cleared_history(self, result):
        self.history_clear_button.set_sensitive(True)
        self.selected_history_project = None
        self.selected_history_task_id = None
        self.history_selected_task = None
        self.history_page_stack.set_visible_child_name("projects")
        self.apply_history(result)
        deleted = int(result.get("deletedTasks") or 0)
        reports = int(result.get("deletedReports") or 0)
        kept = int(result.get("keptActive") or 0)
        message = u"已清空 %d 条任务记录和 %d 个报告目录。SWB 工程与设置未改动。" % (deleted, reports)
        if kept:
            message += u" %d 个仍有活动进程的任务已保留。" % kept
        if result.get("reportErrors"):
            message += u" 个别报告目录未能删除，请查看本机文件权限。"
        self.history_status_label.set_text(message)
        return False

    def apply_clear_history_error(self, message):
        self.history_clear_button.set_sensitive(True)
        self.history_status_label.set_text(u"清空失败：%s" % as_text(message))
        return False

    def history_worker(self):
        try:
            result = core.research_rpc({"action": "project-history"})
            GLib.idle_add(self.apply_history, result)
        except Exception as error:
            GLib.idle_add(self.apply_history_error, error_text(error))

    def apply_history(self, result):
        self.history_projects = result.get("projects") or []
        self.render_history(self.history_projects)
        data_path = as_text(result.get("databasePath") or u"—")
        report_path = as_text(result.get("reportRoot") or u"—")
        self.history_storage_label.set_text(u"任务库：%s\n报告目录：%s" % (data_path, report_path))
        return False

    def on_history_filter_changed(self, *_args):
        self.render_history(self.history_projects)

    def render_history(self, projects):
        self.history_store.clear()
        filter_id = self.history_filter_combo.get_active_id() if hasattr(self, "history_filter_combo") else "all"
        if filter_id == "active":
            visible_projects = [item for item in projects if int(item.get("activeCount") or 0) > 0]
        elif filter_id == "pending":
            visible_projects = [item for item in projects if int(item.get("pendingCount") or 0) > 0]
        elif filter_id == "completed":
            visible_projects = [item for item in projects if int(item.get("completedCount") or 0) > 0]
        elif filter_id == "interrupted":
            visible_projects = [item for item in projects if int(item.get("interruptedCount") or 0) > 0]
        elif filter_id == "cancelled":
            visible_projects = [item for item in projects if int(item.get("cancelledCount") or 0) > 0]
        elif filter_id == "failed":
            visible_projects = [item for item in projects if int(item.get("failedCount") or 0) > 0]
        else:
            visible_projects = list(projects)
        status_names = {
            "active": u"进行中", "pending": u"待处理", "completed": u"已完成",
            "interrupted": u"已中断", "cancelled": u"已停止", "failed": u"失败",
            "archived": u"已有记录",
        }
        for item in visible_projects:
            updated = as_text(item.get("updatedAt") or u"").replace("T", " ").replace("Z", "")[:16]
            counts = u"%d 条任务 · %d 份报告" % (
                int(item.get("taskCount") or 0), int(item.get("reportCount") or 0),
            )
            project = as_text(item.get("project") or u"未命名工程")
            self.history_store.append((
                project,
                status_names.get(item.get("displayStatus"), as_text(item.get("displayStatus") or u"已有记录")),
                as_text(item.get("latestTitle") or u"—"),
                counts,
                updated,
                project,
            ))
        self.history_status_label.set_text(u"显示 %d / %d 个工程。双击进入工程档案；每个工程只占一行。" % (
            len(visible_projects), len(projects),
        ))

    def on_history_project_selected(self, selection):
        model, iterator = selection.get_selected()
        self.selected_history_project = model.get_value(iterator, 5) if iterator is not None else None

    def on_open_history_project(self, *_args):
        if not self.selected_history_project:
            self.show_message(u"请先选择一个工程。")
            return
        self.history_detail_status_label.set_text(u"正在读取这个工程的全部任务记录…")
        start_thread(self.history_project_worker, (self.selected_history_project,))

    def history_project_worker(self, project):
        try:
            result = core.research_rpc({"action": "project-tasks", "project": project, "limit": 500})
            GLib.idle_add(self.apply_history_project, project, result)
        except Exception as error:
            GLib.idle_add(self.apply_history_project_error, error_text(error))

    def apply_history_project(self, project, result):
        self.history_project_tasks = result.get("tasks") or []
        self.selected_history_task_id = None
        self.history_selected_task = None
        self.history_project_title_label.set_text(u"工程档案 · %s" % as_text(project))
        self.render_history_detail(self.history_project_tasks)
        self.history_page_stack.set_visible_child_name("detail")
        return False

    def apply_history_project_error(self, message):
        self.history_detail_status_label.set_text(u"工程档案读取失败：%s" % as_text(message))
        return False

    def on_close_history_project(self, *_args):
        self.history_page_stack.set_visible_child_name("projects")
        self.selected_history_task_id = None

    def on_close_history_task(self, *_args):
        self.history_page_stack.set_visible_child_name("detail")
        self.history_selected_task = None

    def render_history_detail(self, tasks):
        self.history_detail_store.clear()
        kind_names = {
            "workspace-task": u"工作台任务", "project-understanding": u"工程阅读",
            "sdevice-tid": u"SDevice · TID", "tool-research": u"Tool 研究", "tool-model": u"通用模型",
        }
        status_names = {
            "review-required": u"待审查", "evidence-ready": u"证据就绪",
            "code-applied": u"代码已应用", "completed": u"已完成",
            "planning": u"规划中", "approval-required": u"待确认", "execution-ready": u"准备执行",
            "running": u"执行中", "stopping": u"停止中", "cancelled": u"已停止",
            "interrupted": u"已中断", "specialist-review": u"详细审查",
            "understood": u"工程已理解", "failed": u"失败",
        }
        for item in tasks:
            updated = as_text(item.get("updatedAt") or item.get("createdAt") or u"").replace("T", " ").replace("Z", "")[:16]
            self.history_detail_store.append((
                updated,
                kind_names.get(item.get("kind"), as_text(item.get("kind") or u"研究")),
                status_names.get(item.get("status"), as_text(item.get("status") or u"—")),
                as_text(item.get("question") or item.get("summary") or u"—"),
                as_text(item.get("reportPath") or u"—"),
                as_text(item.get("taskId") or u""),
            ))
        self.history_detail_status_label.set_text(u"共 %d 条完整记录。双击可在这里查看详情、事件与报告。" % len(tasks))

    def apply_history_error(self, message):
        self.history_status_label.set_text(u"任务历史读取失败：%s" % as_text(message))
        return False

    def on_history_selected(self, selection):
        model, iterator = selection.get_selected()
        self.selected_history_task_id = model.get_value(iterator, 5) if iterator is not None else None

    def on_open_history_task(self, *_args):
        if not self.selected_history_task_id:
            self.show_message(u"请先选择一个任务。")
            return
        start_thread(self.open_history_task_worker, (self.selected_history_task_id,))

    def open_history_task_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "task", "taskId": task_id})
            GLib.idle_add(self.open_history_task_result, result.get("task") or {})
        except Exception as error:
            GLib.idle_add(self.show_message, error_text(error), Gtk.MessageType.ERROR)

    def open_history_task_result(self, task):
        self.history_selected_task = task
        task_id = as_text(task.get("taskId") or u"未编号")
        title = as_text(task.get("title") or task.get("question") or u"任务详情")
        self.history_task_title_label.set_text(u"任务详情 · %s" % title)
        self.history_task_status_label.set_text(u"任务 %s · %s · 最后更新 %s" % (
            task_id,
            self.history_status_text(task.get("status")),
            as_text(task.get("updatedAt") or task.get("createdAt") or u"—").replace("T", " ").replace("Z", ""),
        ))
        self.history_task_buffer.set_text(self.history_task_text(task))
        report = as_text(task.get("reportContent") or u"")
        if report:
            self.history_report_buffer.set_text(report)
        elif task.get("reportPath"):
            self.history_report_buffer.set_text(u"报告已保存在：\n%s\n\n点击“生成 / 更新报告”可重新汇总当前完整记录。" %
                                                as_text(task.get("reportPath")))
        else:
            self.history_report_buffer.set_text(u"这个任务还没有报告。点击“生成 / 更新报告”即可在这里生成并查看。")
        self.history_task_report_button.set_sensitive(bool(task.get("taskId")))
        self.history_task_continue_button.set_sensitive(self.history_task_can_continue(task))
        self.history_task_tabs.set_current_page(0)
        self.history_page_stack.set_visible_child_name("task")
        return False

    def history_status_text(self, status):
        return {
            "review-required": u"待审查", "evidence-ready": u"证据就绪", "code-applied": u"代码已应用",
            "completed": u"已完成", "planning": u"规划中", "approval-required": u"待确认",
            "execution-ready": u"准备执行", "running": u"执行中", "stopping": u"停止中",
            "interrupted": u"已中断", "cancelled": u"已停止",
            "specialist-review": u"详细审查", "understood": u"工程已理解",
            "failed": u"失败", "clarification-required": u"等待补充条件",
        }.get(status, as_text(status or u"—"))

    def history_task_can_continue(self, task):
        return task.get("status") in (
            "clarification-required", "approval-required", "execution-ready", "running", "stopping",
            "failed", "cancelled", "interrupted", "specialist-review", "planning",
        )

    def history_task_text(self, task):
        lines = []
        def section(title, values):
            clean = [as_text(value).strip() for value in values if as_text(value).strip()]
            if clean:
                lines.extend([title, u"─" * 38] + clean + [u""])

        section(u"任务概况", [
            u"状态：%s" % self.history_status_text(task.get("status")),
            u"类型：%s" % as_text(task.get("taskType") or task.get("kind") or u"—"),
            u"工程：%s" % as_text(task.get("project") or u"草稿任务"),
            u"工程路径：%s" % as_text(task.get("projectPath") or u"尚未建立 / 未记录"),
            u"运行 ID：%s" % as_text(task.get("runId") or u"—"),
            u"当前阶段：%s" % as_text(task.get("currentPhase") or task.get("stage") or task.get("workflowStage") or u"—"),
        ])
        section(u"用户目标", [as_text(task.get("question") or u"—")])
        section(u"当前结论", [as_text(task.get("summary") or u"尚无结论")])

        approved = task.get("approvedInputs") or {}
        if isinstance(approved, dict) and approved:
            section(u"已确认参数", [u"• %s：%s" % (as_text(key), as_text(value))
                                  for key, value in sorted(approved.items())])
        plan = task.get("plan") or task.get("workspacePlan") or {}
        if isinstance(plan, dict) and plan:
            section(u"执行方案", [as_text(plan.get("summary") or u"")] +
                    [u"%d. %s" % (index + 1, as_text(value)) for index, value in enumerate(plan.get("steps") or [])] +
                    [u"风险：%s" % as_text(value) for value in (plan.get("risks") or [])])
        blueprint = task.get("blueprint") or {}
        if isinstance(blueprint, dict) and blueprint:
            chain = [u"%s (%s)" % (as_text(item.get("step")), as_text(item.get("tool")))
                     for item in (blueprint.get("toolChain") or []) if isinstance(item, dict)]
            files = [as_text(item.get("path")) for item in (blueprint.get("files") or []) if isinstance(item, dict)]
            section(u"工程蓝图", [
                u"名称：%s" % as_text(blueprint.get("approvedName") or blueprint.get("name") or u"—"),
                u"目录：%s" % as_text(blueprint.get("approvedParent") or blueprint.get("directory") or u"—"),
                u"Tool 链：%s" % (u" → ".join(chain) or u"—"),
                u"文件：%s" % (u"、".join(files) or u"—"),
            ] + [u"假设：%s" % as_text(value) for value in (blueprint.get("assumptions") or [])] +
                [u"风险：%s" % as_text(value) for value in (blueprint.get("risks") or [])])
        validation = task.get("validation") or {}
        if isinstance(validation, dict) and validation:
            section(u"验证结果", [
                u"验证模式：%s" % as_text(validation.get("mode") or validation.get("validationMode") or u"—"),
                u"仿真已验证：%s" % (u"是" if validation.get("simulationVerified") else u"否"),
                u"摘要：%s" % as_text(validation.get("summary") or validation.get("diagnostic") or u"—"),
            ])
        events = task.get("events") or []
        if not isinstance(events, list):
            events = []
        section(u"完整事件记录（%d 条）" % len(events), [
            u"• %s  %s" % (as_text(item.get("at") or item.get("timestamp") or u""),
                           as_text(item.get("message") or item.get("type") or u"事件"))
            for item in events
        ])
        references = task.get("references") or []
        if not isinstance(references, list):
            references = []
        section(u"依据与附件", [u"• %s" % as_text(item.get("title") or item.get("originalName") or
                                                     item.get("location") or item.get("path") or u"资料")
                               for item in references if isinstance(item, dict)])
        section(u"本机保存", [u"报告：%s" % as_text(task.get("reportPath") or u"尚未生成")])
        return u"\n".join(lines).strip() or u"任务记录为空。"

    def on_generate_history_report(self, *_args):
        task = self.history_selected_task or {}
        if not task.get("taskId"):
            return
        self.history_task_report_button.set_sensitive(False)
        self.history_task_status_label.set_text(u"正在汇总完整任务记录并生成报告…")
        start_thread(self.generate_history_report_worker, (task.get("taskId"),))

    def generate_history_report_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "report", "taskId": task_id})
            current = core.research_rpc({"action": "task", "taskId": task_id}).get("task") or {}
            GLib.idle_add(self.apply_history_report, current, result)
        except Exception as error:
            GLib.idle_add(self.apply_history_report_error, error_text(error))

    def apply_history_report(self, task, result):
        self.history_selected_task = task
        self.history_task_report_button.set_sensitive(True)
        self.history_task_status_label.set_text(u"报告已保存：%s" % as_text(result.get("path") or u"—"))
        self.history_report_buffer.set_text(as_text(result.get("content") or task.get("reportContent") or u"报告已生成。"))
        self.history_task_buffer.set_text(self.history_task_text(task))
        self.history_task_tabs.set_current_page(1)
        return False

    def apply_history_report_error(self, message):
        self.history_task_report_button.set_sensitive(True)
        self.history_task_status_label.set_text(u"报告生成失败：%s" % as_text(message))
        return False

    def on_continue_history_task(self, *_args):
        if self.history_selected_task:
            self.continue_history_task_result(self.history_selected_task)

    def continue_history_task_result(self, task):
        if task.get("taskType") == "project-create":
            self.generation_task_id = task.get("taskId")
            self.generation_run_id = task.get("runId")
            self.generation_project_path = task.get("projectPath")
            self.created_project_relative = task.get("project") if task.get("projectPath") else None
            self.new_project_prompt_buffer.set_text(as_text(task.get("question") or u""))
            self.project_create_plan = task.get("blueprint")
            references = task.get("references") or []
            self.generation_reference = references[0] if references else None
            if self.generation_reference:
                self.new_project_reference_label.set_text(u"已关联 %s · %d 页 · %s" % (
                    as_text(self.generation_reference.get("originalName")),
                    int(self.generation_reference.get("pageCount") or 0),
                    as_text(self.generation_reference.get("sha256") or u"")[:12]))
                self.new_project_reference_button.set_label(u"替换 PDF")
                self.new_project_reference_remove_button.set_sensitive(True)
            self.show_page("new-project")
            status = task.get("status")
            if status == "clarification-required":
                self.new_project_questions.set_text(u"\n".join(u"%d. %s" % (index + 1, as_text(question))
                                                          for index, question in enumerate(task.get("clarificationQuestions") or [])))
                self.set_new_project_stage("research")
                self.generation_busy = False
                self.new_project_research_title.set_text(u"AI 已完成第一轮分析，需要你确认关键条件")
                self.new_project_phase_label.set_text(u"等待你的回答")
                self.new_project_research_progress.set_fraction(1.0)
                self.new_project_research_progress.set_text(u"第一轮分析完成")
                self.new_project_stop_planning_button.set_sensitive(False)
                self.new_project_clarification.set_no_show_all(False)
                self.new_project_clarification.show_all()
            elif status in ("approval-required", "execution-ready") and self.project_create_plan:
                start_thread(self.restore_generation_review_worker, (self.project_create_plan,))
            elif status in ("running", "stopping"):
                self.new_project_execution_buffer.set_text(u"\n".join(as_text(item.get("message")) for item in (task.get("events") or [])[-35:]))
                self.new_project_status_label.set_text(u"任务仍在执行；可以停止或在任务历史查看最新事件。")
                self.set_new_project_stage("executing")
            elif status == "interrupted":
                self.new_project_status_label.set_text(u"上次创建会话已中断。需求已恢复，你可以检查后重新让 AI 规划。")
                self.set_new_project_stage("basic")
            elif status in ("completed", "failed", "cancelled"):
                self.new_project_complete_label.set_text(u"%s\n%s\n报告：%s" % (
                    as_text(task.get("summary") or status), as_text(task.get("projectPath") or u"尚未建立工程"),
                    as_text(task.get("reportPath") or u"请在任务历史中查看")))
                self.new_project_open_button.set_sensitive(bool(self.created_project_relative))
                self.new_project_open_button.set_label(
                    u"进入任务工作台继续修复" if status == "failed" and self.created_project_relative
                    else u"打开并让 AI 理解")
                validation = task.get("validation") or {}
                if status == "failed" and validation.get("diagnostic"):
                    self.show_project_validation_failure(validation)
                self.set_new_project_stage("complete")
            else:
                self.set_new_project_stage("basic")
            return False
        if task.get("kind") == "workspace-task":
            self.active_project = as_text(task.get("project") or self.active_project)
            self.project_session_started = True
            self.project_report = task.get("projectReport") or {}
            self.project_report_task_id = task.get("projectReportTaskId")
            self.dashboard_project_name_label.set_text(self.active_project)
            self.populate_understanding_summary(self.project_report)
            self.workspace_request_revealer.set_reveal_child(True)
            self.workspace_goal_buffer.set_text(as_text(task.get("question") or u""))
            self.show_page("dashboard")
            self.render_workspace_task_detail(task)
            status = task.get("status")
            if status in ("approval-required", "execution-ready"):
                self.workspace_plan_mode_stack.set_visible_child_name("review")
                self.set_workspace_stage("plan", unlock=True)
            elif status in ("running", "stopping"):
                self.current_workspace_execution_id = task.get("taskId")
                self.current_run_id = task.get("runId")
                self.workspace_stop_button.set_sensitive(status == "running" and bool(self.current_run_id))
                self.reset_task_dashboard(task.get("question") or task.get("title") or u"EmberTCAD 任务")
                self.execution_iteration_label.set_text(u"第 %d 次迭代 · 无固定次数上限" % int(task.get("currentIteration") or 0))
                self.set_task_detail(task.get("summary") or task.get("stage") or u"正在执行")
                self.set_workspace_stage("execute", unlock=True)
            elif status in ("completed", "failed", "cancelled", "interrupted", "specialist-review"):
                self.current_workspace_task = task
                self.workspace_complete_title.set_text({
                    "completed": u"任务已完成", "failed": u"任务执行失败",
                    "cancelled": u"任务已停止", "interrupted": u"任务已中断",
                    "specialist-review": u"方案与代码差异已生成",
                }.get(status, u"任务结果"))
                self.workspace_complete_summary.set_text(as_text(task.get("summary") or task.get("stage") or u"任务记录已载入。"))
                event_lines = [u"• %s" % as_text(item.get("message")) for item in (task.get("events") or [])[-8:] if item.get("message")]
                self.workspace_complete_details.set_text(u"\n".join(event_lines) or u"完整记录保存在本机任务库。")
                self.workspace_resume_button.set_sensitive(status in ("failed", "cancelled", "interrupted"))
                self.workspace_complete_advanced_button.set_sensitive(bool(task.get("linkedTaskId")))
                self.workspace_report_button.set_sensitive(bool(task.get("taskId")))
                self.workspace_report_spinner.stop()
                self.workspace_report_status_label.set_text(
                    u"已有报告：%s" % as_text(task.get("reportPath"))
                    if task.get("reportPath") else u"任务记录已载入，可以生成完整报告。"
                )
                self.set_workspace_stage("complete", unlock=True)
            else:
                self.workspace_plan_mode_stack.set_visible_child_name("planning")
                self.set_workspace_stage("plan", unlock=True)
            return False
        if task.get("kind") == "project-understanding":
            self.show_page("dashboard")
            self.project_report = task.get("projectReport") or {}
            self.project_report_task_id = task.get("taskId")
            self.project_report_buffer.set_text(self.project_report_text(self.project_report))
            return False
        self.show_page("dashboard")
        self.append_chat(u"EmberTCAD · 历史", as_text(task.get("summary") or task.get("question") or u"历史记录已载入。"))
        if task.get("kind") in ("tool-model", "tool-research", "sdevice-tid"):
            self.render_research_plan(task)
        return False

    def restore_generation_review_worker(self, blueprint):
        try:
            blueprint = dict(blueprint or {})
            default_parent = os.path.join(as_text(self.workspace_root or u""), "aitcad_workspaces")
            if blueprint.get("approvedDirectory", blueprint.get("directory")):
                default_parent = os.path.join(
                    default_parent, as_text(blueprint.get("approvedDirectory", blueprint.get("directory"))))
            requested = as_text(blueprint.get("approvedName") or blueprint.get("name"))
            safe_name = available_project_name(default_parent, requested)
            if safe_name != requested:
                blueprint["name"] = safe_name
            preflight = core.rpc("project.planGenerated", {
                "name": safe_name,
                "directory": blueprint.get("approvedDirectory", blueprint.get("directory")) or "",
                "toolChain": blueprint.get("toolChain"), "parameters": blueprint.get("parameters") or [],
                "files": blueprint.get("files"),
            })
            GLib.idle_add(self.apply_project_create_plan, blueprint, preflight)
        except Exception as error:
            GLib.idle_add(self.apply_project_create_error, error_text(error))

    def on_choose_generation_pdf(self, *_args):
        if self.generation_busy or self.generation_reference_busy:
            return
        dialog = Gtk.FileChooserDialog(
            title=u"选择作为建模依据的 PDF", parent=self,
            action=Gtk.FileChooserAction.OPEN,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, u"读取此 PDF", Gtk.ResponseType.OK),
        )
        pdf_filter = Gtk.FileFilter()
        pdf_filter.set_name(u"PDF 文档")
        pdf_filter.add_pattern("*.pdf")
        pdf_filter.add_pattern("*.PDF")
        dialog.add_filter(pdf_filter)
        response = dialog.run()
        selected = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not selected:
            return
        self.generation_reference_busy = True
        self.new_project_reference_button.set_sensitive(False)
        self.new_project_plan_button.set_sensitive(False)
        self.new_project_reference_label.set_text(u"正在本机读取并建立页码索引：%s" % as_text(os.path.basename(selected)))
        start_thread(self.ingest_generation_pdf_worker, (selected,))

    def ingest_generation_pdf_worker(self, path):
        try:
            result = core.research_rpc({"action": "ingest-reference-pdf", "path": path})
            GLib.idle_add(self.apply_generation_pdf, result.get("reference") or {})
        except Exception as error:
            GLib.idle_add(self.apply_generation_pdf_error, error_text(error))

    def apply_generation_pdf(self, reference):
        self.generation_reference_busy = False
        self.generation_reference = reference
        self.new_project_reference_button.set_sensitive(True)
        self.new_project_reference_button.set_label(u"替换 PDF")
        self.new_project_reference_remove_button.set_sensitive(True)
        self.new_project_plan_button.set_sensitive(True)
        if reference.get("textAvailable"):
            self.new_project_reference_label.set_text(
                u"已读取 %s · %d 页 · %s。相关页摘要会随需求送入 AI。" % (
                    as_text(reference.get("originalName")), int(reference.get("pageCount") or 0),
                    as_text(reference.get("sha256") or u"")[:12]))
        else:
            self.new_project_reference_label.set_text(
                u"%s 没有可提取文字，可能是扫描件；当前系统未安装 OCR，规划会停下而不会猜测内容。" %
                as_text(reference.get("originalName")))
        return False

    def apply_generation_pdf_error(self, message):
        self.generation_reference_busy = False
        self.generation_reference = None
        self.new_project_reference_button.set_sensitive(True)
        self.new_project_reference_button.set_label(u"添加 PDF")
        self.new_project_reference_remove_button.set_sensitive(False)
        self.new_project_plan_button.set_sensitive(True)
        self.new_project_reference_label.set_text(u"PDF 读取失败：%s" % as_text(message))
        return False

    def on_remove_generation_pdf(self, *_args):
        if self.generation_reference_busy:
            return
        self.generation_reference = None
        self.new_project_reference_label.set_text(u"未添加。可选择论文、报告或器件说明书。")
        self.new_project_reference_button.set_label(u"添加 PDF")
        self.new_project_reference_remove_button.set_sensitive(False)

    def on_plan_project_create(self, *_args):
        goal = self.text_buffer_value(self.new_project_prompt_buffer).strip()
        if len(goal) < 12:
            self.show_message(u"请用一句话描述器件、研究目标与希望验证的结果。")
            return
        if self.generation_busy:
            return
        if self.generation_reference_busy:
            self.show_message(u"参考 PDF 仍在本机读取，请等页码索引完成后再开始。")
            return
        self.generation_busy = True
        self.generation_cancel_requested = False
        self.generation_started_at = time.time()
        self.generation_last_event_at = self.generation_started_at
        self.generation_task_id = None
        self.project_create_plan = None
        self.new_project_create_button.set_sensitive(False)
        self.new_project_review_button.set_sensitive(False)
        self.new_project_status_label.set_text(u"正在理解需求与查找当前版本依据…")
        self.new_project_research_buffer.set_text(u"开始分析需求…\n")
        self.new_project_research_title.set_text(u"AI 正在理解你的需求")
        self.new_project_phase_label.set_text(u"步骤 1/3 · 理解器件与目标")
        self.new_project_elapsed_label.set_text(u"已用时 00:00")
        self.new_project_research_progress.set_fraction(0)
        self.new_project_research_progress.set_text(u"AI 请求进行中，可随时停止")
        self.new_project_stop_planning_button.set_sensitive(True)
        self.new_project_clarification.set_no_show_all(True)
        self.new_project_clarification.hide()
        self.set_new_project_stage("research")
        start_thread(self.project_create_plan_worker, (goal, None))

    def on_answer_project_create(self, *_args):
        answer = self.text_buffer_value(self.new_project_answer_buffer).strip()
        if not answer or self.generation_busy:
            self.show_message(u"请回答上面的关键问题。")
            return
        self.generation_busy = True
        self.generation_cancel_requested = False
        self.generation_started_at = time.time()
        self.generation_last_event_at = self.generation_started_at
        self.new_project_research_title.set_text(u"AI 正在根据补充条件生成方案")
        self.new_project_phase_label.set_text(u"步骤 1/3 · 重新确认需求")
        self.new_project_research_progress.set_fraction(0)
        self.new_project_research_progress.set_text(u"AI 请求进行中，可随时停止")
        self.new_project_stop_planning_button.set_sensitive(True)
        self.new_project_clarification.set_no_show_all(True)
        self.new_project_clarification.hide()
        append_buffer_text(self.new_project_research_buffer, u"\n你补充了关键条件，正在重新生成方案…\n")
        start_thread(self.project_create_plan_worker, (None, answer))

    def project_create_plan_worker(self, goal, answer):
        try:
            if answer:
                task = core.research_rpc({"action": "revise-generation-goal", "taskId": self.generation_task_id, "answer": answer})["task"]
            else:
                task = core.research_rpc({"action": "create-generation-task", "goal": goal,
                                          "references": [self.generation_reference] if self.generation_reference else []})["task"]
            self.generation_task_id = task["taskId"]
            def planning_event(event):
                self.generation_last_event_at = time.time()
                GLib.idle_add(self.apply_generation_event, event)
                try:
                    core.research_rpc({"action": "record-generation-progress", "taskId": task["taskId"],
                                       "phase": (event.get("data") or {}).get("phase"),
                                       "message": event.get("message")})
                except Exception:
                    pass
            result = core.ai_rpc({"action": "plan-generated-project", "taskId": task["taskId"]},
                                 planning_event,
                                 self.set_generation_process)
            if self.generation_cancel_requested:
                return
            if result.get("clarification"):
                recorded = core.research_rpc({"action": "record-generation-plan", "taskId": task["taskId"],
                                              "clarification": result["clarification"]})
                GLib.idle_add(self.apply_generation_clarification, recorded["task"])
                return
            blueprint = result["blueprint"]
            default_parent = os.path.join(as_text(self.workspace_root or u""), "aitcad_workspaces")
            if blueprint.get("directory"):
                default_parent = os.path.join(default_parent, as_text(blueprint.get("directory")))
            original_name = as_text(blueprint.get("name"))
            safe_name = available_project_name(default_parent, original_name)
            if safe_name != original_name:
                blueprint["name"] = safe_name
                planning_event({"message": u"同名工程 %s 已存在；为保留旧工程，本次建议使用 %s。你仍可在审查页修改。" % (
                    original_name, safe_name), "data": {"phase": "review"}})
            parameters = {"name": blueprint.get("name"), "directory": blueprint.get("directory") or "",
                          "toolChain": blueprint.get("toolChain"),
                          "parameters": blueprint.get("parameters") or [], "files": blueprint.get("files")}
            preflight = core.rpc("project.planGenerated", parameters)
            core.research_rpc({"action": "record-generation-plan", "taskId": task["taskId"], "blueprint": blueprint})
            GLib.idle_add(self.apply_project_create_plan, blueprint, preflight)
        except Exception as error:
            if self.generation_cancel_requested:
                GLib.idle_add(self.apply_generation_planning_stopped)
                return
            try:
                if self.generation_task_id:
                    core.research_rpc({"action": "record-workspace-outcome", "taskId": self.generation_task_id,
                                       "summary": error_text(error), "failed": True})
            except Exception:
                pass
            GLib.idle_add(self.apply_project_create_error, error_text(error))

    def set_generation_process(self, process):
        self.generation_ai_process = process

    def pulse_generation(self):
        now = time.time()
        elapsed = 0
        active_stage = getattr(self, "new_project_active_stage", "basic")
        if self.generation_started_at and active_stage == "research":
            elapsed = max(0, int(now - self.generation_started_at))
            self.new_project_elapsed_label.set_text(u"已用时 %02d:%02d" % (elapsed // 60, elapsed % 60))
        if self.generation_busy:
            stage = active_stage
            if stage == "research":
                self.new_project_research_progress.pulse()
                self.new_project_research_progress.set_text(u"AI 正在处理 · %02d:%02d" % (elapsed // 60, elapsed % 60))
                if (self.generation_task_id and self.generation_ai_process is None and
                        not self.generation_reconcile_busy and now - self.generation_last_reconcile_at >= 2.5):
                    self.generation_reconcile_busy = True
                    self.generation_last_reconcile_at = now
                    start_thread(self.generation_reconcile_worker, (self.generation_task_id,))
            elif stage == "executing":
                self.new_project_execution_progress.pulse()
        return True

    def generation_reconcile_worker(self, task_id):
        try:
            task = core.research_rpc({"action": "task", "taskId": task_id}).get("task") or {}
            GLib.idle_add(self.apply_generation_reconciled, task_id, task)
        except Exception:
            GLib.idle_add(self.finish_generation_reconcile)

    def finish_generation_reconcile(self):
        self.generation_reconcile_busy = False
        return False

    def apply_generation_reconciled(self, task_id, task):
        self.generation_reconcile_busy = False
        if task_id != self.generation_task_id or self.generation_cancel_requested:
            return False
        status = task.get("status")
        if status == "clarification-required":
            self.apply_generation_clarification(task)
        elif status in ("approval-required", "execution-ready") and task.get("blueprint"):
            self.project_create_plan = task.get("blueprint")
            start_thread(self.restore_generation_review_worker, (self.project_create_plan,))
        elif status in ("failed", "cancelled"):
            self.generation_busy = False
            self.new_project_stop_planning_button.set_sensitive(False)
            self.new_project_research_progress.set_fraction(0)
            self.new_project_research_progress.set_text(u"任务已%s" % (u"停止" if status == "cancelled" else u"失败"))
            self.new_project_status_label.set_text(as_text(task.get("summary") or task.get("stage") or status))
        return False

    def apply_generation_event(self, event):
        message = as_text(event.get("message") or u"")
        if not message:
            return False
        # Route live events by the real workflow, not by whichever completed
        # page the user is reviewing at this moment.
        if getattr(self, "new_project_active_stage", "basic") == "executing":
            buffer = self.new_project_execution_buffer
        else:
            buffer = self.new_project_research_buffer
        append_buffer_text(buffer, u"• %s\n" % message, 14000)
        phase = as_text((event.get("data") or {}).get("phase") or u"")
        phase_labels = {
            "clarify": u"步骤 1/3 · 理解器件与目标",
            "evidence": u"步骤 2/3 · 检索当前版本依据",
            "generate": u"步骤 3/3 · 生成工程与源文件方案",
            "repair": u"步骤 3/3 · 安全预检与自动修订",
            "review": u"步骤 3/3 · 方案已生成",
        }
        if phase in phase_labels:
            self.new_project_phase_label.set_text(phase_labels[phase])
        self.new_project_status_label.set_text(message)
        return False

    def apply_generation_clarification(self, task):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_stop_planning_button.set_sensitive(False)
        self.new_project_research_title.set_text(u"AI 已完成第一轮分析，需要你确认关键条件")
        self.new_project_phase_label.set_text(u"等待你的回答")
        self.new_project_research_progress.set_fraction(1.0)
        self.new_project_research_progress.set_text(u"第一轮分析完成")
        self.new_project_questions.set_text(u"\n".join(u"%d. %s" % (index + 1, as_text(question)) for index, question in enumerate(task.get("clarificationQuestions") or [])))
        # The box starts with no-show-all so the initial page remains clean.
        # Disable that flag before revealing it; show_all alone intentionally
        # skips no-show-all widgets and was the reason completed AI questions
        # remained invisible in 0.17.1.
        self.new_project_clarification.set_no_show_all(False)
        self.new_project_clarification.show_all()
        self.new_project_clarification.queue_resize()
        self.new_project_status_label.set_text(u"需要你补充关键条件，AI 才能提出可靠方案。")
        return False

    def apply_project_create_plan(self, blueprint, preflight):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_stop_planning_button.set_sensitive(False)
        self.project_create_plan = blueprint
        self.new_project_name_entry.set_text(as_text(blueprint.get("name")))
        self.generation_target_parent = None
        suggested_parts = [u"aitcad_workspaces"]
        if blueprint.get("directory"):
            suggested_parts.append(as_text(blueprint.get("directory")))
        suggested_parent = os.path.join(as_text(self.workspace_root or u""), *suggested_parts)
        self.new_project_directory_entry.set_text(as_text(suggested_parent))
        self.new_project_status_label.set_text(u"工程方案已生成；尚未写入任何文件。")
        evidence = blueprint.get("evidence") or []
        self.new_project_plan_buffer.set_text(u"\n".join((
            u"目标：%s" % as_text(blueprint.get("summary")),
            u"建议目录：%s" % as_text(preflight.get("targetRelativePath")),
            u"Tool 链：%s" % u" → ".join(u"%s (%s)" % (as_text(item.get("step")), as_text(item.get("tool"))) for item in blueprint.get("toolChain") or []),
            u"SWB 参数：%s" % (u"、".join(u"%s=%s (Tool %s)" % (as_text(item.get("name")), as_text(item.get("value")), as_text(item.get("step"))) for item in blueprint.get("parameters") or []) or u"无"),
            u"文件：%d 个 · %d bytes" % (int(preflight.get("fileCount") or 0), int(preflight.get("totalBytes") or 0)),
            u"\n物理假设：\n%s" % u"\n".join(u"• " + as_text(value) for value in blueprint.get("assumptions") or []),
            u"\n验证方法：\n%s" % u"\n".join(u"• " + as_text(value) for value in blueprint.get("validationPlan") or []),
            u"\n风险与未知项：\n%s" % u"\n".join(u"• " + as_text(value) for value in blueprint.get("risks") or []),
            u"\n依据（当前版本）：\n%s" % u"\n".join(u"• %s · %s" % (as_text(item.get("title")), as_text(item.get("location"))) for item in evidence),
        )))
        self.new_project_file_buffer.set_text(u"\n\n".join(
            u"========== %s =========\n%s" % (as_text(item.get("path")), as_text(item.get("content")))
            for item in blueprint.get("files") or []))
        self.new_project_review_button.set_sensitive(True)
        self.set_new_project_stage("blueprint")
        return False

    def on_choose_new_project_parent(self, *_args):
        root = os.path.realpath(self.workspace_root or "")
        if not root or not os.path.isdir(root):
            self.show_message(u"尚未识别当前工作区根目录，请先刷新连接状态。", Gtk.MessageType.WARNING)
            return
        dialog = Gtk.FileChooserDialog(
            title=u"选择新 Project 的上级目录", parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            buttons=(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, u"选择此目录", Gtk.ResponseType.OK),
        )
        current = self.new_project_directory_entry.get_text().strip()
        dialog.set_current_folder(current if current and os.path.isdir(current) else root)
        response = dialog.run()
        selected = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()
        if not selected:
            return
        selected = os.path.realpath(selected)
        if selected != root and not selected.startswith(root + os.sep):
            self.show_message(u"为保护文件安全，新工程必须建立在当前工作区 %s 内。" % as_text(root), Gtk.MessageType.WARNING)
            return
        if os.path.isfile(os.path.join(selected, "gtree.dat")):
            self.show_message(u"请选择用于容纳新 Project 的上级目录，不要选择一个已有 SWB Project 本身。", Gtk.MessageType.WARNING)
            return
        relative = os.path.relpath(selected, root).replace(os.sep, "/")
        self.generation_target_parent = relative
        self.new_project_directory_entry.set_text(as_text(selected))
        current_name = self.new_project_name_entry.get_text().strip()
        safe_name = available_project_name(selected, current_name)
        if safe_name != current_name:
            self.new_project_name_entry.set_text(safe_name)
            self.new_project_status_label.set_text(u"该目录已有同名工程；已建议安全的新名称 %s，旧工程不会被覆盖。" % safe_name)
        else:
            self.new_project_status_label.set_text(u"已选择创建位置；Project 名称仍可在批准前修改。")

    def on_review_project_files(self, *_args):
        if not self.project_create_plan:
            return
        self.new_project_create_button.set_sensitive(True)
        self.new_project_status_label.set_text(u"逐文件核对后再批准。修改工程名会在执行前重新预检。")
        self.set_new_project_stage("review")

    def on_reset_project_create(self, *_args):
        if self.generation_busy:
            return
        self.project_create_plan = None
        self.generation_task_id = None
        self.generation_run_id = None
        self.generation_cancel_requested = False
        self.generation_project_path = None
        self.generation_target_parent = None
        self.generation_reference = None
        self.generation_reference_busy = False
        self.generation_started_at = None
        self.generation_last_event_at = None
        self.generation_reconcile_busy = False
        self.pending_repair_goal = None
        self.new_project_active_stage = "basic"
        self.new_project_view_stage = "basic"
        self.new_project_unlocked_index = 0
        self.new_project_reached_stages = set(["basic"])
        self.new_project_prompt_buffer.set_text(u"")
        self.new_project_reference_label.set_text(u"未添加。可选择论文、报告或器件说明书。")
        self.new_project_reference_button.set_label(u"添加 PDF")
        self.new_project_reference_button.set_sensitive(True)
        self.new_project_reference_remove_button.set_sensitive(False)
        self.new_project_plan_button.set_sensitive(True)
        self.new_project_name_entry.set_text(u"")
        self.new_project_directory_entry.set_text(u"")
        self.new_project_plan_buffer.set_text(u"")
        self.new_project_file_buffer.set_text(u"")
        self.new_project_open_button.set_sensitive(False)
        self.new_project_open_button.set_label(u"打开并让 AI 理解")
        self.new_project_failure_box.set_no_show_all(True)
        self.new_project_failure_box.hide()
        self.new_project_failure_buffer.set_text(u"")
        self.new_project_stop_planning_button.set_sensitive(False)
        self.new_project_research_title.set_text(u"AI 正在理解你的需求")
        self.new_project_phase_label.set_text(u"步骤 1/3 · 理解器件与目标")
        self.new_project_elapsed_label.set_text(u"已用时 00:00")
        self.new_project_research_progress.set_fraction(0)
        self.new_project_research_progress.set_text(u"准备调用 AI")
        self.new_project_clarification.set_no_show_all(True)
        self.new_project_clarification.hide()
        self.new_project_status_label.set_text(u"请描述新工程要解决的物理问题。")
        self.set_new_project_stage("basic")

    def apply_project_create_error(self, message):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_stop_planning_button.set_sensitive(False)
        self.new_project_status_label.set_text(u"创建计划失败：%s" % as_text(message))
        if getattr(self, "new_project_active_stage", "basic") == "executing":
            self.new_project_complete_label.set_text(u"创建或验证失败\n%s\n%s" % (
                as_text(message), u"工程已建立，可在任务历史查看失败原因并继续。" if self.generation_project_path else u"没有覆盖已有工程。"))
            self.new_project_open_button.set_sensitive(bool(self.generation_project_path))
            self.new_project_open_button.set_label(
                u"进入任务工作台继续修复" if self.generation_project_path else u"打开并让 AI 理解")
            self.set_new_project_stage("complete")
        else:
            self.new_project_research_title.set_text(u"本次分析没有完成")
            self.new_project_phase_label.set_text(u"可以返回修改需求后重试")
            self.new_project_research_progress.set_fraction(0)
            self.new_project_research_progress.set_text(u"分析失败")
            append_buffer_text(self.new_project_research_buffer, u"\n失败：%s\n" % as_text(message))
        return False

    def on_stop_project_planning(self, *_args):
        if not self.generation_busy or getattr(self, "new_project_active_stage", "basic") != "research":
            return
        self.generation_cancel_requested = True
        self.new_project_stop_planning_button.set_sensitive(False)
        self.new_project_status_label.set_text(u"正在停止 AI 分析…")
        self.new_project_research_progress.set_text(u"正在停止")
        start_thread(self.stop_project_planning_worker, (self.generation_task_id, self.generation_ai_process))

    def stop_project_planning_worker(self, task_id, process):
        try:
            if process and process.poll() is None:
                process.terminate()
                deadline = time.time() + 2.0
                while process.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                if process.poll() is None:
                    process.kill()
            if task_id:
                try:
                    core.research_rpc({"action": "cancel-workspace-task", "taskId": task_id,
                                       "message": u"用户停止了工程方案分析。"})
                except Exception:
                    pass
            GLib.idle_add(self.apply_generation_planning_stopped)
        except Exception as error:
            GLib.idle_add(self.apply_project_create_error, error_text(error))

    def apply_generation_planning_stopped(self):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_stop_planning_button.set_sensitive(False)
        self.new_project_research_title.set_text(u"本次分析已停止")
        self.new_project_phase_label.set_text(u"没有创建或修改任何工程文件")
        self.new_project_research_progress.set_fraction(0)
        self.new_project_research_progress.set_text(u"已停止")
        self.new_project_status_label.set_text(u"已停止本次分析；可以返回修改需求后重新开始。")
        append_buffer_text(self.new_project_research_buffer, u"\n• 用户已停止本次分析。\n")
        return False

    def on_apply_project_create(self, *_args):
        blueprint = self.project_create_plan
        if not blueprint or self.generation_busy:
            return
        name = self.new_project_name_entry.get_text().strip()
        target_parent = self.generation_target_parent
        directory = "" if target_parent is not None else as_text(blueprint.get("directory") or u"")
        target_display = self.new_project_directory_entry.get_text().strip()
        validation_mode = self.new_project_validation_combo.get_active_id() or "baseline"
        if not name:
            self.show_message(u"请保留或修改 AI 建议的工程名称。")
            return
        safe_name = available_project_name(target_display, name)
        if safe_name != name:
            self.new_project_name_entry.set_text(safe_name)
            self.new_project_status_label.set_text(
                u"%s 已存在。已改为 %s 以保护旧工程；请核对后再次点击批准。" % (name, safe_name))
            self.show_message(u"检测到同名工程，EmberTCAD 已提出不覆盖旧工程的新名称。请核对后再次批准。",
                              Gtk.MessageType.WARNING)
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=u"创建新的受管 SWB 工程？",
        )
        validation_labels = {"preflight": u"快速预检（不声明仿真成功）", "baseline": u"一条代表性依赖路径",
                             "full": u"全部叶节点"}
        dialog.format_secondary_text(u"将生成 %d 个源文件、建立 %d 个 SWB Tool 步骤。验证级别：%s。目标为 %s/%s；同名工程一律拒绝。若其他 SWB 正在使用不同工程，EmberTCAD 会打开独立 SWB 窗口，避免影响原工程。" % (
            len(blueprint.get("files") or []), len(blueprint.get("toolChain") or []),
            validation_labels.get(validation_mode, validation_mode),
            as_text(target_display), as_text(name)))
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not approved:
            return
        self.generation_busy = True
        self.generation_cancel_requested = False
        self.new_project_create_button.set_sensitive(False)
        self.new_project_status_label.set_text(u"正在重新预检并建立独占执行记录…")
        self.new_project_execution_buffer.set_text(u"正在复核批准的源文件与目标路径…\n")
        self.set_new_project_stage("executing")
        start_thread(self.project_create_worker, (blueprint, name, directory, target_parent, validation_mode))

    def project_create_worker(self, blueprint, name, directory, target_parent, validation_mode):
        task_id = self.generation_task_id
        run_id = None
        try:
            params = {"name": name, "directory": directory, "toolChain": blueprint.get("toolChain"),
                      "parameters": blueprint.get("parameters") or [], "files": blueprint.get("files")}
            if target_parent is not None:
                params["targetParent"] = target_parent
            preflight = core.rpc("project.planGenerated", params)
            core.research_rpc({"action": "approve-workspace-task", "taskId": task_id})
            running = core.research_rpc({"action": "begin-workspace-execution", "taskId": task_id})["task"]
            run_id = running["runId"]
            self.generation_run_id = run_id
            if self.generation_cancel_requested:
                core.research_rpc({"action": "cancel-workspace-task", "taskId": task_id, "runId": run_id})
                core.research_rpc({"action": "report", "taskId": task_id})
                GLib.idle_add(self.apply_project_create_error, u"用户停止；未创建工程。")
                return
            GLib.idle_add(self.apply_generation_event, {"message": u"已取得独占执行权；正在建立空白 SWB 工程树与源文件。"})
            created = core.rpc("project.createGenerated", dict(params, approvalToken=preflight["approvalToken"], confirm=True))
            self.generation_project_path = created["path"]
            self.created_project_relative = created["relativePath"]
            core.research_rpc({"action": "associate-generated-project", "taskId": task_id,
                               "runId": run_id, "path": created["path"]})
            GLib.idle_add(self.apply_generation_event, {
                "message": u"工程已原子发布；正在把它打开到 SWB，随后所有节点状态都会同步刷新。",
                "data": {"phase": "swb-link"},
            })
            try:
                linked = core.live_action("open-project", created["path"])
                if linked.get("opened"):
                    link_message = (u"SWB 已打开新工程；即将提交真实验证节点。" if linked.get("launched") else
                                    u"已连接现有 SWB 工程窗口并刷新；即将提交真实验证节点。")
                else:
                    link_message = u"SWB 启动请求已发出，但窗口尚未出现；仿真会继续，联动状态将在日志中保留。"
                GLib.idle_add(self.apply_generation_event, {
                    "message": link_message,
                    "data": {"phase": "swb-link", "swb": linked},
                })
            except Exception as link_error:
                # A display/license problem should be visible to the user but
                # must not turn a valid headless SWB validation into a failure.
                GLib.idle_add(self.apply_generation_event, {
                    "message": u"工程已创建，但 SWB 图形窗口未能自动打开：%s。将继续真实节点验证。" % error_text(link_error),
                    "data": {"phase": "swb-link", "warning": True},
                })
            current = core.research_rpc({"action": "task", "taskId": task_id})["task"]
            if current.get("status") in ("cancelled", "stopping"):
                core.research_rpc({"action": "report", "taskId": task_id})
                GLib.idle_add(self.apply_project_create_error, u"用户停止；新工程保留，但没有运行验证。")
                return
            GLib.idle_add(self.apply_generation_event, {"message": u"新工程已创建；正在核对 Tool 节点并试运行。"})
            def execution_event(event):
                GLib.idle_add(self.apply_generation_event, event)
                data = event.get("data") or {}
                try:
                    core.research_rpc({"action": "record-generation-progress", "taskId": task_id,
                                       "runId": run_id, "phase": data.get("phase"),
                                       "node": data.get("node"), "pid": data.get("pid"),
                                       "message": event.get("message")})
                except Exception:
                    pass
            verified = core.ai_rpc({"action": "validate-generated-project", "project": created["path"],
                                    "taskId": task_id, "runId": run_id,
                                    "validationMode": validation_mode},
                                   execution_event,
                                   self.set_generation_process)
            current = core.research_rpc({"action": "task", "taskId": task_id})["task"]
            if current.get("status") == "cancelled":
                GLib.idle_add(self.apply_project_create_error, u"用户停止；未完成产物不算验证成功。")
                return
            core.research_rpc({"action": "record-generation-validation", "taskId": task_id,
                               "runId": run_id, "validation": verified})
            if verified.get("validationPassed") is False:
                summary = u"工程 %s 已创建，但真实验证未通过：%s" % (
                    created["relativePath"], as_text(verified.get("summary") or u"未知 Tool 错误"))
                core.research_rpc({"action": "record-workspace-outcome", "taskId": task_id,
                                   "runId": run_id, "summary": summary, "failed": True})
                report = core.research_rpc({"action": "report", "taskId": task_id})
                GLib.idle_add(self.apply_project_validation_failed, created, verified,
                              report.get("path") or report.get("reportPath"))
                return
            if verified.get("simulationVerified"):
                summary = u"新工程 %s 已创建；代表性路径的 %d 个真实节点 done，发现 %d 个新增产物。" % (
                    created["relativePath"], len(verified.get("nodes") or []), len(verified.get("outputs") or []))
            else:
                summary = u"新工程 %s 已创建并通过快速静态预检；尚未运行仿真，不能视为数值验证成功。" % created["relativePath"]
            core.research_rpc({"action": "record-workspace-outcome", "taskId": task_id,
                               "runId": run_id, "summary": summary, "failed": False})
            report = core.research_rpc({"action": "report", "taskId": task_id})
            GLib.idle_add(self.apply_project_created, created, summary,
                          report.get("path") or report.get("reportPath"),
                          bool(verified.get("simulationVerified")))
        except Exception as error:
            if run_id:
                try:
                    current = core.research_rpc({"action": "task", "taskId": task_id})["task"]
                    if current.get("status") not in ("cancelled", "completed"):
                        core.research_rpc({"action": "record-workspace-outcome", "taskId": task_id,
                                           "runId": run_id, "summary": error_text(error), "failed": True})
                        core.research_rpc({"action": "report", "taskId": task_id})
                except Exception:
                    pass
            GLib.idle_add(self.apply_project_create_error, error_text(error))

    def apply_project_created(self, result, summary, report_path, simulation_verified):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_status_label.set_text(
            u"工程创建与真实节点验收完成。" if simulation_verified else
            u"工程创建与静态预检完成；尚未运行仿真。"
        )
        self.new_project_open_button.set_label(u"打开并让 AI 理解")
        self.new_project_failure_box.set_no_show_all(True)
        self.new_project_failure_box.hide()
        self.new_project_complete_label.set_text(u"%s\n%s\n报告：%s" % (
            as_text(summary), as_text(result.get("path")), as_text(report_path or u"任务历史")))
        self.new_project_open_button.set_sensitive(bool(self.created_project_relative))
        self.set_new_project_stage("complete")
        self.refresh(True)
        return False

    def show_project_validation_failure(self, validation):
        diagnostic = validation.get("diagnostic") or {}
        summary = as_text(diagnostic.get("summary") or validation.get("summary") or u"真实节点没有完成。")
        tool = as_text(diagnostic.get("tool") or u"Tool")
        node = as_text(diagnostic.get("node") or validation.get("failedNode") or u"—")
        self.new_project_failure_title.set_text(u"%s 节点 %s 验证未通过" % (tool, node))
        self.new_project_failure_summary.set_text(summary)
        detail_lines = [u"根因摘要：%s" % summary, u""]
        location = diagnostic.get("location") or {}
        if location:
            detail_lines.append(u"位置：%s:%s" % (as_text(location.get("file")), as_text(location.get("line"))))
        files = diagnostic.get("files") or []
        for item in files[:4]:
            detail_lines.extend([u"", u"===== %s =====" % as_text(item.get("path") or item.get("name")),
                                 as_text(item.get("tail") or u"")[-5000:]])
        if len(files) > 4:
            detail_lines.extend([u"", u"其余 %d 个诊断文件已保存在任务报告中。" % (len(files) - 4)])
        self.new_project_failure_buffer.set_text(u"\n".join(detail_lines))
        self.new_project_failure_box.set_no_show_all(False)
        self.new_project_failure_box.show_all()
        self.pending_repair_goal = (
            u"请修复刚创建工程的真实验证失败。先核对当前版本 Manual/Tutorial 和出错源文件，"
            u"给出根因、逐文件差异和最小复现；我确认后再写入并从失败节点继续验证。\n\n"
            u"已定位错误：%s（%s 节点 %s）。" % (summary, tool, node)
        )

    def apply_project_validation_failed(self, created, validation, report_path):
        self.generation_busy = False
        self.generation_ai_process = None
        self.new_project_status_label.set_text(u"工程已创建，但真实验证未通过；已保存根因和报告。")
        self.new_project_complete_label.set_text(u"工程文件已建立，没有被冒充为仿真成功。\n%s\n报告：%s" % (
            as_text(created.get("path")), as_text(report_path or u"任务历史")))
        self.created_project_relative = created.get("relativePath")
        self.new_project_open_button.set_sensitive(True)
        self.new_project_open_button.set_label(u"进入任务工作台生成修复方案")
        self.show_project_validation_failure(validation)
        self.set_new_project_stage("complete")
        self.refresh(True)
        return False

    def on_stop_project_create(self, *_args):
        if self.generation_cancel_requested:
            return
        self.generation_cancel_requested = True
        if not self.generation_run_id or not self.generation_task_id:
            self.new_project_status_label.set_text(u"正在等待执行记录建立，随后立即停止；尚未创建工程。")
            return
        self.new_project_status_label.set_text(u"正在停止当前节点与 AI 进程…")
        start_thread(self.stop_project_create_worker, (self.generation_task_id, self.generation_run_id,
                                                       self.generation_project_path, self.generation_ai_process))

    def stop_project_create_worker(self, task_id, run_id, project, process):
        try:
            core.research_rpc({"action": "cancel-workspace-task", "taskId": task_id, "runId": run_id})
            if project:
                try:
                    core.live_action("stop-run", project, run_id=run_id)
                except Exception:
                    pass
            if process and process.poll() is None:
                process.terminate()
                deadline = time.time() + 2
                while process.poll() is None and time.time() < deadline:
                    time.sleep(0.05)
                if process.poll() is None:
                    process.kill()
            core.research_rpc({"action": "report", "taskId": task_id})
            GLib.idle_add(self.apply_project_create_error, u"已停止；先前生成的工程保留，未完成节点不计入报告。")
        except Exception as error:
            GLib.idle_add(self.apply_project_create_error, error_text(error))

    def on_open_created_project(self, *_args):
        relative = getattr(self, "created_project_relative", None)
        if not relative:
            return
        self.show_page("dashboard")
        self.start_project_session(relative)

    def on_apply_research_plan(self, *_args):
        plan = self.research_plan
        modification = (plan or {}).get("modification") or {}
        if not modification.get("content"):
            self.show_message(u"当前任务还没有可应用的代码差异。请先完成大模型方案生成。")
            return
        if self.research_busy:
            self.show_message(u"当前研究请求仍在进行，请稍候。")
            return
        parameter_names = [as_text(item.get("name")) for item in plan.get("parameterPlan") or []]
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=u"批准应用这份 %s 差异？" % as_text(plan.get("toolLabel") or u"Tool"),
        )
        dialog.format_secondary_text(u"目标：%s\n将先确认源文件 SHA-256，再按方案加入 %d 个 SWB 参数，最后由 Connector 备份并原子写入。写入不会自动运行节点。" % (
            as_text(modification.get("relativePath")), len(parameter_names),
        ))
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not approved:
            return
        self.research_busy = True
        self.code_apply_button.set_sensitive(False)
        self.code_status_label.set_text(u"正在验证源文件、建立参数并写入可恢复变更…")
        start_thread(self.apply_research_plan_worker, (plan,))

    def apply_research_plan_worker(self, plan):
        try:
            modification = plan.get("modification") or {}
            write_plan = core.rpc("file.planWrite", {
                "relativePath": modification.get("relativePath"),
                "content": modification.get("content"),
            })
            if write_plan.get("originalSha256") != modification.get("sourceSha256"):
                raise RuntimeError("源文件在方案生成后发生变化；已拒绝写入，请重新生成差异")
            project = self.project_absolute_path()
            live_state = core.read_live_swb_state(project)
            existing = dict((item.get("name"), item) for item in live_state.get("parameters") or [])
            added = []
            already = []
            for item in plan.get("parameterPlan") or []:
                name = str(item.get("name") or "")
                if name in existing:
                    already.append(name)
                    continue
                step = item.get("step")
                if step is None:
                    raise RuntimeError("无法确认 SDevice 工具步骤；没有修改工程")
                core.live_action("add-parameter", project, name=name, value=str(item.get("value") or "0"), step=int(step))
                added.append(name)
            write_result = core.rpc("file.writeText", {
                "relativePath": modification.get("relativePath"),
                "content": modification.get("content"),
                "approvalToken": write_plan.get("approvalToken"),
                "confirm": True,
            })
            core.live_action("refresh-swb", project)
            record = core.research_rpc({
                "action": "record-apply", "taskId": plan.get("taskId"),
                "writeResult": write_result, "parametersAdded": added,
                "parametersExisting": already,
            })
            GLib.idle_add(self.apply_research_plan_success, record.get("task") or plan, write_result, added, already)
        except Exception as error:
            GLib.idle_add(self.apply_research_plan_error, error_text(error))

    def apply_research_plan_success(self, task, write_result, added, already):
        self.research_busy = False
        self.render_research_plan(task)
        self.code_apply_button.set_sensitive(False)
        self.code_status_label.set_text(u"代码已应用并备份；真实 Tool 验证尚未开始。")
        self.append_chat(u"EmberTCAD", u"%s 变更已应用。新增参数：%s；已存在：%s；备份 ID：%s。" % (
            as_text(task.get("toolLabel") or u"Tool"),
            u"、".join(as_text(value) for value in added) or u"无",
            u"、".join(as_text(value) for value in already) or u"无",
            as_text(write_result.get("backupId") or u"—"),
        ))
        self.refresh(True)
        start_thread(self.load_research_status_worker)
        return False

    def on_run_model_task(self, *_args):
        plan = self.research_plan
        if not plan or plan.get("kind") != "tool-model":
            self.show_message(u"请先生成并应用一个通用 Tool 模型任务。")
            return
        if plan.get("status") not in ("code-applied", "completed"):
            self.show_message(u"请先审查并应用代码差异。")
            return
        if self.research_busy or self.ai_busy:
            self.show_message(u"当前已有任务在运行，请等待完成。")
            return
        node_text = self.code_validation_node_entry.get_text().strip()
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=u"批准运行真实 %s 节点？" % as_text(plan.get("toolLabel") or u"Tool"),
        )
        dialog.format_secondary_text(u"验证节点：%s\nEmberTCAD 将调用 gsub 并等待真实 .sta/进程结果；失败日志和报告会写入本地任务记录。" % (
            as_text(node_text or u"自动选择该 Tool 的最近节点"),
        ))
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not approved:
            return
        self.research_busy = True
        self.ai_busy = True
        self.set_code_stage("validation")
        self.code_run_button.set_sensitive(False)
        self.code_apply_button.set_sensitive(False)
        self.code_run_status_label.set_text(u"正在提交并等待真实 Tool 节点…")
        self.set_task_title(u"%s 模型验证" % as_text(plan.get("toolLabel") or u"Tool"))
        self.set_task_detail(as_text(plan.get("question") or u"验证已批准的模型代码差异。"))
        self.set_task_state(u"Tool 验证运行中", "warning")
        self.set_task_progress(0.05, "SUBMITTING")
        request = {
            "action": "run-generic-research-task", "taskId": plan.get("taskId"),
            "project": self.project_absolute_path(), "node": node_text,
        }
        start_thread(self.model_run_worker, (request,))

    def model_run_worker(self, request):
        try:
            result = core.ai_rpc(request, lambda event: GLib.idle_add(self.apply_ai_event, event))
            GLib.idle_add(self.apply_model_run_result, result)
        except Exception as error:
            GLib.idle_add(self.apply_model_run_error, error_text(error))

    def apply_model_run_result(self, result):
        self.research_busy = False
        self.ai_busy = False
        task = result.get("task") if isinstance(result.get("task"), dict) else self.research_plan
        if task:
            self.render_research_plan(task)
        self.set_task_state(u"Tool 验证完成", "success")
        self.set_task_progress(1.0, "REPORT READY")
        self.code_run_status_label.set_text(as_text((result.get("validation") or {}).get("summary") or u"真实验证已完成。"))
        self.append_chat(u"EmberTCAD", as_text(result.get("text") or u"Tool 验证完成。"))
        report = result.get("report") or {}
        if report.get("content"):
            self.show_preview(report.get("path") or u"EmberTCAD report", report.get("content"))
        self.refresh(True)
        start_thread(self.load_research_status_worker)
        return False

    def apply_model_run_error(self, message):
        self.research_busy = False
        self.ai_busy = False
        self.code_run_button.set_sensitive(bool(self.research_plan and self.research_plan.get("status") in ("code-applied", "completed")))
        self.code_run_status_label.set_text(u"Tool 验证停止：%s" % as_text(message))
        self.set_task_state(u"Tool 验证停止", "error")
        self.set_task_progress(text="STOPPED")
        self.append_chat(u"EmberTCAD", u"Tool 验证失败：%s" % as_text(message))
        return False

    def on_run_tid_task(self, *_args):
        plan = self.research_plan
        if not plan or plan.get("kind") != "sdevice-tid" or self.research_busy or self.ai_busy:
            return
        values = {
            "dosePointsKrad": self.tid_doses_entry.get_text().strip(),
            "qox0": self.tid_qox0_entry.get_text().strip(),
            "qoxPerKrad": self.tid_qox_slope_entry.get_text().strip(),
            "dit0": self.tid_dit0_entry.get_text().strip(),
            "ditPerKrad": self.tid_dit_slope_entry.get_text().strip(),
            "eXsection": self.tid_exsection_entry.get_text().strip(),
            "hXsection": self.tid_hxsection_entry.get_text().strip(),
            "concentration": self.tid_concentration_entry.get_text().strip(),
            "drainBiasV": self.tid_drain_entry.get_text().strip(),
            "calibrationSource": self.tid_calibration_source_entry.get_text().strip(),
        }
        if not values["dosePointsKrad"] or not values["qoxPerKrad"] or not values["ditPerKrad"] or not values["calibrationSource"]:
            self.show_message(u"请填写剂量点、Qox/krad、Dit/krad 和标定来源。斜率可以填 0，但两者不能同时为 0。")
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text=u"批准使用这组标定并运行真实 SWB 节点？",
        )
        dialog.format_secondary_text(u"Dose=%s krad\nQox/krad=%s · Dit/krad=%s\n来源=%s\n\nEmberTCAD 将修改这些受管参数、创建剂量分支并逐个运行 SDevice；结果和日志会写入当前工程。" % (
            as_text(values["dosePointsKrad"]), as_text(values["qoxPerKrad"]),
            as_text(values["ditPerKrad"]), as_text(values["calibrationSource"]),
        ))
        approved = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not approved:
            return
        self.research_busy = True
        self.ai_busy = True
        self.code_run_button.set_sensitive(False)
        self.code_apply_button.set_sensitive(False)
        self.tid_run_status_label.set_text(u"正在配置剂量分支并运行 SDevice…")
        self.set_task_title(u"TID → NMOS Vth 剂量扫描")
        self.set_task_detail(u"使用已批准的 Qox(D)/Dit(D) 线性标定运行真实 SWB 节点。")
        self.set_task_state(u"TID 仿真运行中", "warning")
        self.set_task_progress(0.03, "MATERIALIZING")
        request = dict(values)
        request.update({
            "action": "run-research-task", "taskId": plan.get("taskId"),
            "project": self.project_absolute_path(),
        })
        start_thread(self.tid_run_worker, (request,))

    def tid_run_worker(self, request):
        try:
            result = core.ai_rpc(request, lambda event: GLib.idle_add(self.apply_ai_event, event))
            GLib.idle_add(self.apply_tid_run_result, result)
        except Exception as error:
            GLib.idle_add(self.apply_tid_run_error, error_text(error))

    def apply_tid_run_result(self, result):
        self.research_busy = False
        self.ai_busy = False
        task = result.get("task") if isinstance(result.get("task"), dict) else self.research_plan
        if task:
            self.render_research_plan(task)
        self.set_task_state(u"TID 仿真完成", "success")
        self.set_task_progress(1.0, "REPORT READY")
        self.tid_run_status_label.set_text(u"真实扫描已完成；报告已生成。")
        self.append_chat(u"EmberTCAD", as_text(result.get("text") or u"TID 扫描完成。"))
        report = result.get("report") or {}
        if report.get("content"):
            self.show_preview(report.get("path") or u"EmberTCAD report", report.get("content"))
        self.refresh(True)
        start_thread(self.load_research_status_worker)
        return False

    def apply_tid_run_error(self, message):
        self.research_busy = False
        self.ai_busy = False
        self.code_run_button.set_sensitive(bool(self.research_plan and self.research_plan.get("status") in ("code-applied", "completed")))
        self.tid_run_status_label.set_text(u"TID 运行停止：%s" % as_text(message))
        self.set_task_state(u"TID 任务停止", "error")
        self.set_task_progress(text="STOPPED")
        self.append_chat(u"EmberTCAD", u"TID 运行失败：%s" % as_text(message))
        return False

    def on_generate_current_report(self, *_args):
        if not self.research_plan or not self.research_plan.get("taskId"):
            self.show_message(u"请先在“研究、修改与仿真”中生成或打开一个研究任务。")
            return
        if self.research_busy:
            return
        self.research_busy = True
        self.code_status_label.set_text(u"正在生成带证据和限制说明的阶段报告…")
        start_thread(self.report_worker, (self.research_plan.get("taskId"),))

    def report_worker(self, task_id):
        try:
            result = core.research_rpc({"action": "report", "taskId": task_id})
            GLib.idle_add(self.apply_report_result, result)
        except Exception as error:
            GLib.idle_add(self.apply_research_plan_error, error_text(error))

    def apply_report_result(self, result):
        self.research_busy = False
        if self.research_plan:
            self.research_plan["reportPath"] = result.get("path")
        self.code_status_label.set_text(u"阶段报告已生成：%s" % as_text(result.get("path")))
        self.show_preview(result.get("path") or u"EmberTCAD report", result.get("content") or u"")
        start_thread(self.load_research_status_worker)
        return False

    def build_settings_page(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        root.pack_start(label(u"设置", "title-large"), False, False, 0)
        root.pack_start(label(u"连接你自己的 AI 模型，并选择本机实际安装的 Sentaurus 版本。设置只保存在当前 Linux 用户目录。", "muted", True), False, False, 0)

        form = Gtk.Grid(column_spacing=10, row_spacing=10)
        self.ai_provider_combo = Gtk.ComboBoxText()
        for provider, title, _base_url, _model, _api_style, _reasoning in MODEL_PROVIDERS:
            self.ai_provider_combo.append(provider, title)
        self.ai_provider_combo.set_active_id("deepseek")
        self.ai_provider_combo.connect("changed", self.on_ai_provider_changed)
        self.ai_base_entry = Gtk.Entry()
        self.ai_base_entry.set_text("https://api.deepseek.com")
        self.ai_base_entry.set_placeholder_text(u"例如 https://api.deepseek.com")
        self.ai_model_combo = Gtk.ComboBoxText.new_with_entry()
        self.ai_model_entry = self.ai_model_combo.get_child()
        self.populate_model_suggestions("deepseek", "deepseek-flash")
        self.ai_model_entry.set_placeholder_text(u"填写服务商文档中的精确模型 ID")
        self.ai_api_style_combo = Gtk.ComboBoxText()
        for style, title in API_STYLES:
            self.ai_api_style_combo.append(style, title)
        self.ai_api_style_combo.set_active_id("chat_completions")
        self.ai_api_style_combo.connect("changed", self.update_ai_config_explanation)
        self.ai_reasoning_combo = Gtk.ComboBoxText()
        self.populate_reasoning_options("deepseek", "auto")
        self.ai_reasoning_combo.connect("changed", self.update_ai_config_explanation)
        self.ai_key_entry = Gtk.Entry()
        self.ai_key_entry.set_visibility(False)
        self.ai_key_entry.set_placeholder_text(u"用于验证账户和使用 API 额度；留空保留已保存的 Key")
        form.attach(label(u"服务商 / 接口类型"), 0, 0, 1, 1)
        form.attach(self.ai_provider_combo, 1, 0, 2, 1)
        form.attach(label(u"API 服务地址"), 0, 1, 1, 1)
        form.attach(self.ai_base_entry, 1, 1, 2, 1)
        form.attach(label(u"模型 ID（可选或手输）"), 0, 2, 1, 1)
        form.attach(self.ai_model_combo, 1, 2, 2, 1)
        form.attach(label(u"接口协议"), 0, 3, 1, 1)
        form.attach(self.ai_api_style_combo, 1, 3, 2, 1)
        form.attach(label(u"推理强度"), 0, 4, 1, 1)
        form.attach(self.ai_reasoning_combo, 1, 4, 2, 1)
        form.attach(label("API Key"), 0, 5, 1, 1)
        form.attach(self.ai_key_entry, 1, 5, 2, 1)
        self.ai_base_entry.set_hexpand(True)
        self.ai_model_combo.set_hexpand(True)
        self.ai_config_explanation = label(u"", "info-strip", True)
        form.attach(self.ai_config_explanation, 1, 6, 2, 1)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status = label(u"尚未配置", "muted")
        self.ai_status_labels.append(status)
        save_button = button(u"保存")
        save_button.connect("clicked", lambda *_args: self.on_save_ai_config(False))
        test_button = button(u"保存并测试", "emblem-ok-symbolic", "primary")
        test_button.connect("clicked", lambda *_args: self.on_save_ai_config(True))
        action_row.pack_start(status, True, True, 0)
        action_row.pack_end(test_button, False, False, 0)
        action_row.pack_end(save_button, False, False, 0)

        form_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form_box.pack_start(label(u"AI 模型", "title-medium"), False, False, 0)
        form_box.pack_start(label(u"API Key 用于让服务商验证你的账户、权限和可用额度；它不会自动选择模型。真正调用哪个模型由“模型 ID”决定。", "small-muted", True), False, False, 0)
        form_box.pack_start(form, False, False, 0)
        form_box.pack_start(action_row, False, False, 0)
        root.pack_start(card(form_box, "surface", 16), False, False, 0)
        self.update_ai_config_explanation()

        runtime_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        runtime_box.pack_start(label(u"Sentaurus TCAD 版本", "title-medium"), False, False, 0)
        runtime_box.pack_start(label(u"EmberTCAD 会扫描本机安装。切换后，工程读取、手册检索、代码校验、节点运行和新开的 SWB 都使用所选版本；已经打开的 SWB 不会被关闭。", "small-muted", True), False, False, 0)
        runtime_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sentaurus_version_combo = Gtk.ComboBoxText()
        self.sentaurus_version_combo.set_hexpand(True)
        runtime_row.pack_start(self.sentaurus_version_combo, True, True, 0)
        scan_button = button(u"重新扫描", "view-refresh-symbolic")
        scan_button.connect("clicked", self.on_scan_sentaurus_versions)
        runtime_row.pack_end(scan_button, False, False, 0)
        self.sentaurus_apply_button = button(u"使用所选版本", "emblem-ok-symbolic", "primary")
        self.sentaurus_apply_button.connect("clicked", self.on_apply_sentaurus_version)
        runtime_row.pack_end(self.sentaurus_apply_button, False, False, 0)
        runtime_box.pack_start(runtime_row, False, False, 0)
        self.sentaurus_status_label = label(u"正在扫描本机 Sentaurus 安装…", "small-muted", True)
        runtime_box.pack_start(self.sentaurus_status_label, False, False, 0)
        root.pack_start(card(runtime_box, "surface", 16), False, False, 0)

        privacy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        privacy.pack_start(label(u"数据边界", "title-medium"), False, False, 0)
        privacy.pack_start(label(u"API Key 以 0600 权限保存在当前用户配置中，不显示在界面和日志。工程任务只把完成目标所需的源文件、版本信息与证据摘要发送给当前模型服务；模型调用本身不会写入或运行。人工批准后才会复核源文件指纹、备份并执行。", "muted", True), False, False, 0)
        root.pack_start(card(privacy, "task-card", 15), False, False, 0)
        GLib.idle_add(self.refresh_sentaurus_versions)
        page = self.page_container(root)
        # The settings row has two explicit action buttons.  Slightly narrower
        # side gutters keep the full application at a true 1000 px minimum
        # without hiding actions or introducing a horizontal scrollbar.
        root.set_margin_start(13)
        root.set_margin_end(13)
        return page

    def build_chat_view(self):
        view = WrappingTextView(buffer=self.chat_buffer)
        # Long model messages must wrap inside the viewport. GtkTextView's
        # content-derived minimum width otherwise grows the whole window.
        view.set_size_request(1, -1)
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_pixels_above_lines(2)
        view.set_pixels_below_lines(2)
        self.chat_views.append(view)
        return scroller(view)

    def apply_initial_mode(self):
        self.show_all()
        self.present()
        for argument in sys.argv:
            if argument.startswith("--page="):
                page = argument.split("=", 1)[1]
                if page in self.nav_buttons:
                    self.show_page(page)
                break
        return False

    def append_chat(self, author, text):
        author_text = as_text(author)
        body_text = clean_visible_text(text)
        if not body_text:
            return
        if self.chat_buffer.get_char_count():
            self.chat_buffer.insert(self.chat_buffer.get_end_iter(), u"\n")
        if author_text == u"你":
            tag = "author_user"
        elif author_text.startswith(u"EmberTCAD") or author_text.startswith(u"SPARK AI") or author_text.startswith(u"NOVA") or author_text.startswith(u"AITCAD"):
            tag = "author_ai"
        else:
            tag = "author_event"
        self.chat_buffer.insert_with_tags_by_name(self.chat_buffer.get_end_iter(), author_text + u"\n", tag)
        self.chat_buffer.insert_with_tags_by_name(self.chat_buffer.get_end_iter(), body_text + u"\n", "body")
        for view in self.chat_views:
            try:
                view.scroll_to_iter(self.chat_buffer.get_end_iter(), 0.0, False, 0.0, 1.0)
            except Exception:
                pass

    def add_recent_event(self, kind, message):
        symbols = {
            "thinking": u"◈",
            "reasoning": u"◆",
            "plan": u"●",
            "running": u"↻",
            "measurement": u"✓",
            "conclusion": u"✓",
            "error": u"!",
        }
        symbol = symbols.get(kind, u"•")
        compact = u" ".join(clean_visible_text(message).split())
        if len(compact) > 58:
            compact = compact[:57] + u"…"
        for event_list in self.event_lists:
            row = Gtk.ListBoxRow()
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            set_margins(content, 7, 2, 7, 2)
            symbol_label = label(symbol, "success-text", xalign=0.5)
            symbol_label.set_size_request(18, -1)
            content.pack_start(symbol_label, False, False, 0)
            event_text = label(compact, "muted", True)
            event_text.set_max_width_chars(32)
            content.pack_start(event_text, True, True, 0)
            content.pack_end(label(time.strftime("%H:%M"), "small-muted"), False, False, 0)
            row.add(content)
            event_list.add(row)
            children = event_list.get_children()
            while len(children) > 5:
                event_list.remove(children[0])
                children = event_list.get_children()
            event_list.show_all()

    def set_task_title(self, value):
        for item in self.task_title_labels:
            item.set_text(as_text(value))

    def set_task_detail(self, value):
        for item in self.task_detail_labels:
            item.set_text(clean_visible_text(value))

    def set_task_state(self, value, tone="success"):
        colors = {"success": "#047857", "warning": "#b7791f", "error": "#b42318", "blue": "#1d4ed8"}
        safe = GLib.markup_escape_text(as_text(value))
        if not isinstance(safe, text_type):
            safe = safe.decode("utf-8", "replace")
        markup = u"<span foreground='%s'><b>● %s</b></span>" % (colors.get(tone, colors["blue"]), safe)
        for item in self.task_state_labels:
            item.set_markup(markup)

    def set_task_progress(self, fraction=None, text=None, pulse=False):
        for progress in self.task_progress_bars:
            if pulse:
                progress.pulse()
            elif fraction is not None:
                progress.set_fraction(max(0.0, min(1.0, float(fraction))))
        if text is not None:
            for item in self.task_progress_text_labels:
                item.set_text(as_text(text))

    def set_summary(self, concentration=u"—", vth=u"—", status=u"等待任务"):
        for item in self.best_parameter_labels:
            item.set_text(as_text(concentration))
        for item in self.best_vth_labels:
            item.set_text(as_text(vth))
        for item in self.summary_status_labels:
            item.set_text(as_text(status))

    def refresh_charts(self):
        for chart_widget in self.charts:
            chart_widget.set_data(self.trajectory_points, self.task_target, self.task_tolerance)

    def reset_task_dashboard(self, question):
        self.iteration_store.clear()
        self.iteration_rows = {}
        self.current_iteration = None
        self.task_target = None
        self.task_tolerance = None
        self.task_max_runs = None
        self.trajectory_points = []
        self.refresh_charts()
        self.set_task_title(question)
        self.set_task_detail(u"正在理解目标并生成可执行的实验策略…")
        self.set_task_state(u"深度思考中", "warning")
        self.set_task_progress(0.01, "ANALYZING")
        self.set_summary(u"—", u"—", u"任务启动中")
        self.execution_iteration_label.set_text(u"第 0 次迭代 · 无固定次数上限")
        self.reset_flow_progress(self.execution_phase_widgets)
        for event_list in self.event_lists:
            for child in event_list.get_children():
                event_list.remove(child)

    def ensure_iteration_row(self, data):
        try:
            run = int(data.get("run"))
        except (TypeError, ValueError):
            return None
        iterator = self.iteration_rows.get(run)
        if iterator is None:
            concentration = as_text(data.get("concentrationText") or u"—")
            iterator = self.iteration_store.append((str(run), concentration, u"—", u"—", u"—", u"准备", u"—"))
            self.iteration_rows[run] = iterator
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
        concentration_text = data.get("concentrationText")
        if concentration_text:
            self.iteration_store.set_value(iterator, 1, as_text(concentration_text))
        if elapsed is not None:
            self.iteration_store.set_value(iterator, 6, u"%ss" % elapsed)
        if phase == "materialize":
            self.iteration_store.set_value(iterator, 5, u"创建分支")
        elif phase == "sde":
            text = u"n%s" % structure_node if structure_node is not None else u"SDE"
            if kind == "running" and elapsed is not None:
                text += u" · %ss" % elapsed
            self.iteration_store.set_value(iterator, 2, text)
            self.iteration_store.set_value(iterator, 5, u"SDE 运行")
        elif phase == "sde_done":
            self.iteration_store.set_value(iterator, 2, u"✓ n%s" % structure_node)
            self.iteration_store.set_value(iterator, 5, u"SDevice")
        elif phase == "sdevice":
            text = u"n%s" % device_node if device_node is not None else u"SDevice"
            if kind == "running" and elapsed is not None:
                text += u" · %ss" % elapsed
            self.iteration_store.set_value(iterator, 3, text)
            self.iteration_store.set_value(iterator, 5, u"仿真中")
        elif phase == "extract":
            self.iteration_store.set_value(iterator, 3, u"✓ n%s" % device_node)
            self.iteration_store.set_value(iterator, 5, u"提取 Vth")
        elif phase == "reused":
            self.iteration_store.set_value(iterator, 2, u"✓ n%s" % structure_node)
            self.iteration_store.set_value(iterator, 3, u"↻ n%s" % device_node)
            self.iteration_store.set_value(iterator, 5, u"复用结果")

        if kind == "measurement":
            self.iteration_store.set_value(iterator, 2, u"✓ n%s" % data.get("structureNode"))
            self.iteration_store.set_value(iterator, 3, u"%s n%s" % (u"↻" if data.get("reused") else u"✓", data.get("node")))
            try:
                vth = float(data.get("vth"))
                concentration = float(data.get("concentration"))
                self.iteration_store.set_value(iterator, 4, "%.6f" % vth)
                self.trajectory_points.append({
                    "run": run,
                    "concentration": concentration,
                    "concentrationText": as_text(data.get("concentrationText") or "%.8g" % concentration),
                    "vth": vth,
                    "withinTolerance": bool(data.get("withinTolerance")),
                    "structureNode": data.get("structureNode"),
                    "node": data.get("node"),
                })
                self.refresh_charts()
                self.update_best_summary()
            except (TypeError, ValueError):
                self.iteration_store.set_value(iterator, 4, u"—")
            self.iteration_store.set_value(iterator, 5, u"命中目标" if data.get("withinTolerance") else u"已测量")

    def update_best_summary(self):
        if not self.trajectory_points:
            self.set_summary()
            return
        if self.task_target is None:
            best = self.trajectory_points[-1]
            status = u"%d 组真实结果" % len(self.trajectory_points)
        else:
            best = min(self.trajectory_points, key=lambda item: abs(item["vth"] - float(self.task_target)))
            status = u"命中目标" if best.get("withinTolerance") else u"继续搜索"
        concentration = best.get("concentrationText") or "%.8g" % best["concentration"]
        self.set_summary(as_text(concentration) + u" cm⁻³", "%.6f V" % best["vth"], status)

    def on_quick_workspace_goal(self, source_entry):
        """Route every free-form entry through plan-before-execute."""
        question = source_entry.get_text().strip()
        if not question or self.workspace_busy:
            return
        for entry in self.chat_entries:
            entry.set_text("")
        self.workspace_goal_buffer.set_text(as_text(question))
        self.append_chat(u"你", question)
        self.append_chat(u"EmberTCAD", u"已建立任务草稿。现在先生成总体方案，确认后才会执行。")
        self.on_create_workspace_task()

    def on_send(self, source_entry):
        question = source_entry.get_text().strip()
        if not question or self.ai_busy:
            return
        for entry in self.chat_entries:
            entry.set_text("")
        self.append_chat(u"你", question)
        self.reset_task_dashboard(question)
        self.ai_busy = True
        for send in self.send_buttons:
            send.set_sensitive(False)
        self.set_ai_status(u"AI / TCAD 任务执行中…", "warning")
        start_thread(self.ai_chat_worker, (question, list(self.ai_history)))

    def set_active_ai_process(self, process):
        self.active_ai_process = process

    def ai_chat_worker(self, question, history, workspace_task_id=None, run_id=None, approved_inputs=None):
        try:
            result = core.ai_rpc({
                "action": "chat",
                "project": self.project_absolute_path(),
                "message": question,
                "history": history,
                "workspaceTaskId": workspace_task_id,
                "runId": run_id,
                "approvedInputs": approved_inputs or {},
            }, lambda event: GLib.idle_add(self.apply_ai_event, event), self.set_active_ai_process)
            GLib.idle_add(self.apply_ai_response, question, result, workspace_task_id, run_id)
        except Exception as error:
            GLib.idle_add(self.apply_ai_error, error_text(error), workspace_task_id, run_id)

    def persist_workspace_progress_worker(self, task_id, run_id, event):
        try:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            core.research_rpc({
                "action": "update-workspace-execution", "taskId": task_id, "runId": run_id,
                "currentIteration": data.get("run"),
                "currentPhase": data.get("phase") or event.get("event"),
                "node": data.get("node"), "pid": data.get("pid"),
                "message": event.get("message") or "",
            })
        except Exception:
            pass

    def apply_ai_event(self, event):
        kind = event.get("event") or "progress"
        message = event.get("message") or ""
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if not message:
            return False
        names = {
            "thinking": u"深度分析",
            "reasoning": u"思考摘要",
            "evidence": u"证据检索",
            "plan": u"任务计划",
            "approval": u"审批与标定门",
            "step": u"执行步骤",
            "trial": u"参数试验",
            "tid_trial": u"TID 剂量点",
            "tool": u"工具调用",
            "tool_result": u"工具结果",
            "node_started": u"节点已启动",
            "running": u"仿真进度",
            "measurement": u"测量结果",
            "tid_measurement": u"TID 测量结果",
            "result": u"执行结果",
            "conclusion": u"阶段结论",
        }
        self.append_chat(names.get(kind, u"任务进度"), message)
        self.add_recent_event(kind, message)
        self.set_ai_status(message[:62], "success")

        if self.current_workspace_execution_id and self.current_run_id:
            now = time.time()
            important = kind in ("trial", "tid_trial", "node_started", "measurement", "tid_measurement", "result", "conclusion", "approval")
            if important or now - self.last_workspace_progress_persist >= 5.0:
                self.last_workspace_progress_persist = now
                start_thread(self.persist_workspace_progress_worker, (
                    self.current_workspace_execution_id, self.current_run_id, dict(event),
                ))

        if kind == "thinking":
            self.set_task_state(u"深度思考中", "warning")
            self.set_task_progress(text="REASONING", pulse=True)
        elif kind == "reasoning":
            self.set_task_state(u"策略已生成", "blue")
            self.set_task_detail(message)
            self.set_task_progress(0.03, "PLAN READY")
        elif kind == "evidence":
            self.set_task_detail(message)
            self.set_task_state(u"证据已收集", "blue")
            self.set_task_progress(0.32, "EVIDENCE READY")
        elif kind == "plan" and data.get("researchTask"):
            self.set_task_detail(message)
            self.set_task_state(u"等待差异审查", "blue")
            self.set_task_progress(0.48, "PATCH REVIEW")
        elif kind == "plan":
            self.task_target = data.get("targetV")
            self.task_tolerance = data.get("toleranceV")
            self.task_max_runs = None
            if self.task_target is not None and self.task_tolerance is not None:
                self.set_task_title(u"Vth = %.4g ± %.4g V" % (float(self.task_target), float(self.task_tolerance)))
            self.set_task_detail(message)
            self.set_task_state(u"闭环执行中", "success")
            self.set_task_progress(text=u"准备第 1 次迭代", pulse=True)
            self.refresh_charts()
        elif kind in ("tid_trial", "tid_measurement") or data.get("doseKrad") is not None:
            run = int(data.get("run") or 0)
            max_runs = max(1, int(data.get("maxRuns") or 1))
            fraction = min(0.98, (max(0.0, float(run) - (0.0 if kind == "tid_measurement" else 0.65))) / max_runs)
            self.set_task_detail(message)
            self.set_task_state(u"TID 结果评估" if kind == "tid_measurement" else u"TID 仿真运行中", "blue" if kind == "tid_measurement" else "success")
            self.set_task_progress(max(0.08, fraction), "DOSE %s / %s" % (run, max_runs))
            self.tid_run_status_label.set_text(message)
        elif kind in ("trial", "step", "running", "result", "measurement", "node_started"):
            self.update_iteration(kind, data)
            run = data.get("run") or self.current_iteration or 0
            phase = data.get("phase") or ""
            phase_key = {
                "materialize": "materialize", "sde": "sde", "sde_done": "sde",
                "sdevice": "sdevice", "extract": "extract", "reused": "extract",
                "validation": "sdevice",
            }.get(phase)
            phase_order = ["materialize", "sde", "sdevice", "extract", "evaluate"]
            if phase_key:
                current_index = phase_order.index(phase_key)
                for index, key in enumerate(phase_order):
                    if index < current_index:
                        self.set_flow_progress(self.execution_phase_widgets, key, 1.0, u"完成")
                    elif index == current_index:
                        self.set_flow_progress(self.execution_phase_widgets, key, pulse=True, status=u"运行中", detail=message)
            if kind == "measurement":
                self.set_task_state(u"结果评估", "blue")
                for key in phase_order[:-1]:
                    self.set_flow_progress(self.execution_phase_widgets, key, 1.0, u"完成")
                self.set_flow_progress(self.execution_phase_widgets, "evaluate", 1.0, u"完成", message)
            else:
                self.set_task_state(u"仿真运行中" if kind == "running" else u"闭环执行中", "success")
            self.execution_iteration_label.set_text(u"第 %s 次迭代 · 无固定次数上限" % run)
            self.set_task_progress(text=u"第 %s 次迭代" % run, pulse=True)
            self.set_task_detail(message)
        elif kind == "conclusion":
            self.set_task_detail(message)
            self.set_task_state(u"任务完成", "success")
            self.set_task_progress(1.0, "COMPLETE")
            for key in ("materialize", "sde", "sdevice", "extract", "evaluate"):
                self.set_flow_progress(self.execution_phase_widgets, key, 1.0, u"完成")
        elif kind == "approval":
            self.set_task_detail(message)
            self.set_task_state(u"等待批准 / 标定", "warning")
            self.set_task_progress(0.55, "APPROVAL GATE")
        return False

    def apply_ai_response(self, question, result, workspace_task_id=None, run_id=None):
        self.active_ai_process = None
        if workspace_task_id and (
            workspace_task_id != self.current_workspace_execution_id or
            run_id != self.current_run_id or self.execution_stopping
        ):
            return False
        self.ai_busy = False
        self.execution_pulse_source = None
        for send in self.send_buttons:
            send.set_sensitive(True)
        self.set_ai_status(u"API 已配置", "success")
        tool_names = [item.get("name") for item in result.get("tools") or [] if item.get("name")]
        if tool_names:
            self.append_chat(u"工具", u" → ".join(as_text(item) for item in tool_names))
        answer = result.get("text") or u"操作已完成。"
        self.append_chat(u"EmberTCAD · %s" % (result.get("model") or "MODEL"), answer)
        research_plan = result.get("researchPlan") if isinstance(result.get("researchPlan"), dict) else None
        if research_plan:
            self.code_prompt_buffer.set_text(as_text(question))
            self.render_research_plan(research_plan)
            self.set_task_state(u"等待代码审批 / 剂量标定", "warning")
            self.set_task_progress(1.0, "PLAN & DIFF READY")
        else:
            self.set_task_state(u"任务完成", "success")
            self.set_task_progress(1.0, "COMPLETE")
        if workspace_task_id:
            report = result.get("report") if isinstance(result.get("report"), dict) else {}
            linked = research_plan.get("taskId") if research_plan else (self.current_workspace_task or {}).get("linkedTaskId")
            if self.current_workspace_task is not None:
                self.current_workspace_task["linkedTaskId"] = linked
            start_thread(self.record_workspace_outcome_worker, (
                workspace_task_id, answer, bool(research_plan), linked,
                report.get("path") if isinstance(report, dict) else None, False, run_id,
            ))
            self.workspace_complete_title.set_text(u"方案与代码差异已生成" if research_plan else u"任务已完成")
            self.workspace_complete_summary.set_text(answer)
            details = []
            if tool_names:
                details.append(u"工具链：%s" % u" → ".join(as_text(item) for item in tool_names))
            if report.get("path"):
                details.append(u"报告：%s" % as_text(report.get("path")))
            if research_plan:
                details.append(u"修改尚未自动写入。可以在本屏进入代码与证据详情继续审批。")
            self.workspace_complete_details.set_text(u"\n".join(details) or u"结果和可恢复事件已经保存到任务历史。")
            self.workspace_complete_advanced_button.set_sensitive(bool(linked))
            self.workspace_report_button.set_sensitive(False)
            self.workspace_report_spinner.start()
            self.workspace_report_status_label.set_text(u"正在保存完整任务记录…")
            self.workspace_resume_button.set_sensitive(False)
            self.workspace_stop_button.set_sensitive(False)
            self.set_workspace_stage("complete", unlock=True)
            self.current_workspace_execution_id = None
            self.current_run_id = None
        self.ai_history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
        self.ai_history = self.ai_history[-8:]
        self.refresh(False)
        return False

    def apply_ai_error(self, message, workspace_task_id=None, run_id=None):
        self.active_ai_process = None
        if workspace_task_id and (
            workspace_task_id != self.current_workspace_execution_id or
            run_id != self.current_run_id or self.execution_stopping
        ):
            return False
        self.ai_busy = False
        self.execution_pulse_source = None
        for send in self.send_buttons:
            send.set_sensitive(True)
        self.set_ai_status(message[:70], "error")
        self.set_task_detail(message)
        self.set_task_state(u"任务异常", "error")
        self.set_task_progress(text="STOPPED")
        self.append_chat(u"EmberTCAD", u"API/Agent 操作失败：%s" % as_text(message))
        self.add_recent_event("error", message)
        if workspace_task_id:
            start_thread(self.record_workspace_outcome_worker, (
                workspace_task_id, u"执行失败：%s" % as_text(message), False, None, None, True, run_id,
            ))
            self.workspace_complete_title.set_text(u"任务执行失败")
            self.workspace_complete_summary.set_text(u"EmberTCAD 未能完成本次执行；已完成的有效结果和日志仍然保留。")
            self.workspace_complete_details.set_text(as_text(message))
            self.workspace_complete_advanced_button.set_sensitive(False)
            self.workspace_report_button.set_sensitive(False)
            self.workspace_report_spinner.start()
            self.workspace_report_status_label.set_text(u"正在保存失败现场与可恢复事件…")
            self.workspace_resume_button.set_sensitive(True)
            self.workspace_stop_button.set_sensitive(False)
            self.set_workspace_stage("complete", unlock=True)
            self.mark_workspace_stage_failed("execute")
            for row in self.execution_phase_widgets.values():
                if as_text(row["status"].get_text()) == u"运行中":
                    row["status"].set_text(u"失败")
            self.current_workspace_execution_id = None
            self.current_run_id = None
        return False

    def set_ai_status(self, message, tone="success"):
        css = {"success": "success-text", "warning": "warning-text", "error": "error-text"}.get(tone, "muted")
        for item in self.ai_status_labels:
            for name in ("success-text", "warning-text", "error-text", "muted"):
                item.get_style_context().remove_class(name)
            item.get_style_context().add_class(css)
            item.set_text(as_text(message))

    def load_ai_status_worker(self):
        try:
            result = core.ai_rpc({"action": "status"})
            GLib.idle_add(self.apply_ai_status, result)
        except Exception as error:
            GLib.idle_add(self.apply_ai_error, error_text(error))

    def populate_model_suggestions(self, provider, selected_model):
        if not hasattr(self, "ai_model_combo"):
            return
        self.ai_model_combo.remove_all()
        suggestions = list(MODEL_SUGGESTIONS.get(provider, ()))
        if selected_model and selected_model not in suggestions:
            suggestions.insert(0, selected_model)
        for model in suggestions:
            self.ai_model_combo.append_text(model)
        if selected_model:
            self.ai_model_entry.set_text(as_text(selected_model))
        elif suggestions:
            self.ai_model_combo.set_active(0)

    def populate_reasoning_options(self, provider, selected_effort):
        if not hasattr(self, "ai_reasoning_combo"):
            return
        options = PROVIDER_REASONING_EFFORTS.get(provider, PROVIDER_REASONING_EFFORTS["default"])
        valid = [item[0] for item in options]
        effort = selected_effort if selected_effort in valid else "auto"
        self.ai_reasoning_combo.remove_all()
        for key, title in options:
            self.ai_reasoning_combo.append(key, title)
        self.ai_reasoning_combo.set_active_id(effort)

    def apply_ai_status(self, result):
        self.ai_applying_status = True
        provider = result.get("provider") or "custom"
        known = [item[0] for item in MODEL_PROVIDERS]
        self.ai_provider_combo.set_active_id(provider if provider in known else "custom")
        self.ai_base_entry.set_text(result.get("baseUrl") or "")
        self.populate_model_suggestions(provider, result.get("model") or "")
        self.ai_api_style_combo.set_active_id(result.get("apiStyle") or "chat_completions")
        self.populate_reasoning_options(provider, result.get("reasoningEffort") or
                                        ("high" if result.get("thinking") else "auto"))
        self.ai_applying_status = False
        self.update_ai_config_explanation()
        provider_name = as_text(result.get("providerName") or self.ai_provider_combo.get_active_text() or u"模型服务")
        route = u"%s · %s" % (as_text(result.get("model") or u"未选择模型"),
                              self.ai_api_style_title(result.get("apiStyle")))
        self.set_ai_status(u"● %s 已配置 · %s" % (provider_name, route) if result.get("configured") else u"○ 尚未配置 API Key",
                           "success" if result.get("configured") else "warning")
        return False

    def on_ai_provider_changed(self, combo):
        if getattr(self, "ai_applying_status", False):
            return
        provider = combo.get_active_id()
        for key, _title, base_url, model, api_style, reasoning in MODEL_PROVIDERS:
            if key == provider:
                self.ai_base_entry.set_text(base_url)
                self.populate_model_suggestions(provider, model)
                self.ai_api_style_combo.set_active_id(api_style)
                self.populate_reasoning_options(provider, reasoning)
                break
        self.update_ai_config_explanation()

    def ai_api_style_title(self, style):
        names = {
            "responses": u"Responses API",
            "chat_completions": u"Chat Completions",
            "anthropic_messages": u"Anthropic Messages",
        }
        return names.get(style or "", as_text(style or u"未知协议"))

    def update_ai_config_explanation(self, *_args):
        if not hasattr(self, "ai_config_explanation"):
            return
        provider = self.ai_provider_combo.get_active_id() or "custom"
        api_style = self.ai_api_style_combo.get_active_id() or "chat_completions"
        effort = self.ai_reasoning_combo.get_active_id() or "auto"
        if provider == "openai":
            note = (u"OpenAI 推荐：gpt-6.1-sol + Responses API。当前主线的 Sol、Astra、Luna 是不同模型；"
                    u"Terra 是较早一代的模型 ID。必须用精确 ID 选择，API Key 不会自动猜测。")
        elif provider == "deepseek":
            note = (u"DeepSeek 当前模型为 deepseek-flash / deepseek-v4-pro；旧 deepseek-chat / reasoner 只做兼容迁移。"
                    u"推理强度与模型档位分开选择。")
        elif provider == "custom":
            note = u"自定义服务默认按 OpenAI Chat Completions 兼容格式调用；模型 ID 必须与该服务商文档完全一致。"
        else:
            note = u"模型 ID 必须与该服务商账户实际可用的 ID 完全一致；预设只是建议，可直接修改。"
        if effort == "auto":
            effort_note = u"推理强度“自动”表示不强行覆盖模型默认值。"
        elif effort == "standard" and provider == "deepseek":
            effort_note = u"“关闭推理”会要求 DeepSeek 使用非思考模式，响应更快、消耗通常更低。"
        elif effort == "standard":
            effort_note = u"推理强度“标准”表示明确不发送额外推理参数。"
        else:
            effort_note = u"推理强度只控制同一模型投入多少推理计算，不会把一个模型变成另一个模型。"
        self.ai_config_explanation.set_text(u"%s\n当前协议：%s。%s" % (
            note, self.ai_api_style_title(api_style), effort_note))

    def refresh_sentaurus_versions(self):
        try:
            result = core.sentaurus_runtime_status()
            installations = result.get("installations") or []
            self.sentaurus_installations = installations
            self.sentaurus_version_combo.remove_all()
            active_index = -1
            for index, item in enumerate(installations):
                commands = item.get("commands") or {}
                readiness = u"可运行" if commands.get("swb") and commands.get("gsub") else u"组件不完整"
                self.sentaurus_version_combo.append_text(u"%s · %s · %s" % (
                    as_text(item.get("release")), readiness, as_text(item.get("root"))))
                if item.get("selected"):
                    active_index = index
            if installations:
                self.sentaurus_version_combo.set_active(active_index if active_index >= 0 else 0)
                selected = installations[active_index if active_index >= 0 else 0]
                self.sentaurus_status_label.set_text(u"当前版本：%s\n安装目录：%s\n新任务将自动使用该版本的 Tool、手册和语法校验。" % (
                    as_text(result.get("selectedRelease")), as_text(result.get("selectedRoot"))))
                self.sentaurus_apply_button.set_sensitive(True)
            else:
                self.sentaurus_status_label.set_text(u"没有发现可运行的 Sentaurus。请先完成安装，再点击“重新扫描”。")
                self.sentaurus_apply_button.set_sensitive(False)
        except Exception as error:
            self.sentaurus_status_label.set_text(u"Sentaurus 扫描失败：%s" % error_text(error))
            self.sentaurus_apply_button.set_sensitive(False)
        return False

    def on_scan_sentaurus_versions(self, *_args):
        self.sentaurus_status_label.set_text(u"正在重新扫描本机 Sentaurus 安装…")
        self.refresh_sentaurus_versions()

    def on_apply_sentaurus_version(self, *_args):
        if self.current_run_id or (self.generation_busy and self.generation_run_id):
            self.show_message(u"当前有真实节点正在运行。请先停止或等待任务结束，再切换 Sentaurus 版本。",
                              Gtk.MessageType.WARNING)
            return
        index = self.sentaurus_version_combo.get_active()
        installations = getattr(self, "sentaurus_installations", [])
        if index < 0 or index >= len(installations):
            self.show_message(u"请先选择一个已检测到的 Sentaurus 版本。", Gtk.MessageType.WARNING)
            return
        try:
            selected = installations[index]
            result = core.select_sentaurus_root(selected.get("root"))
            self.sentaurus_status_label.set_text(
                u"已切换到 %s。后续工程读取、手册检索、校验、运行和 EmberTCAD 新开的 SWB 都使用该版本；已有 SWB 窗口保持不变。" %
                as_text(result.get("selectedRelease")))
            self.refresh(True)
            start_thread(self.load_research_status_worker)
        except Exception as error:
            self.show_message(u"Sentaurus 版本切换失败：%s" % error_text(error), Gtk.MessageType.ERROR)

    def on_save_ai_config(self, test_after):
        if self.ai_busy:
            return
        request = {
            "action": "save-config",
            "provider": self.ai_provider_combo.get_active_id() or "custom",
            "baseUrl": self.ai_base_entry.get_text().strip(),
            "model": self.ai_model_entry.get_text().strip(),
            "apiKey": self.ai_key_entry.get_text().strip(),
            "apiStyle": self.ai_api_style_combo.get_active_id() or "chat_completions",
            "reasoningEffort": self.ai_reasoning_combo.get_active_id() or "auto",
        }
        self.ai_busy = True
        self.set_ai_status(u"正在保存…", "warning")
        start_thread(self.save_ai_config_worker, (request, test_after))

    def save_ai_config_worker(self, request, test_after):
        try:
            result = core.ai_rpc(request)
            if test_after:
                test_result = core.ai_rpc({"action": "test"})
                result["testText"] = test_result.get("text")
                result["testModel"] = test_result.get("model")
            GLib.idle_add(self.apply_ai_config_saved, result)
        except Exception as error:
            GLib.idle_add(self.apply_ai_error, error_text(error))

    def apply_ai_config_saved(self, result):
        self.ai_busy = False
        self.ai_key_entry.set_text("")
        self.apply_ai_status(result)
        if result.get("testText"):
            self.append_chat(u"EmberTCAD", u"API 测试成功：%s · %s" % (result.get("testModel"), result.get("testText")))
        else:
            self.append_chat(u"EmberTCAD", u"API 配置已保存。")
        return False

    def project_absolute_path(self):
        if not self.workspace_root or not self.active_project:
            raise RuntimeError(u"尚未载入 SWB 工程")
        root = os.path.realpath(self.workspace_root)
        project = os.path.realpath(os.path.join(root, self.active_project))
        if not project.startswith(root + os.sep):
            raise RuntimeError(u"工程路径超出当前 STDB")
        return project

    def on_project_changed(self, combo):
        index = combo.get_active()
        if index < 0 or index >= len(self.project_paths):
            return
        selected = self.project_paths[index]
        if selected != self.active_project or not self.project_session_started:
            self.last_signature = None
            self.results_project = None
            self.loaded_result_signature = None
            self.iteration_store.clear()
            self.iteration_rows = {}
            self.trajectory_points = []
            self.refresh_charts()
            self.start_project_session(selected)

    def refresh(self, manual=False):
        if self.refreshing:
            return True
        # Project-tree probing launches several helper processes and SWB API
        # reads.  It has no user value while the independent new-project AI
        # flow is active and made that page stutter over remote X11.
        if (not manual and self.generation_busy and
                self.page_stack.get_visible_child_name() == "new-project"):
            return True
        self.refreshing = True
        start_thread(self.refresh_worker, (manual,))
        return True

    def refresh_worker(self, manual):
        try:
            status = core.rpc("system.probe")
            projects = status.get("projects") or []
            paths = [item.get("relativePath") for item in projects if item.get("relativePath")]
            project = self.active_project
            if project not in paths:
                project = paths[0] if paths else ""
            tree = core.rpc("project.tree", {"relativePath": project, "maxDepth": 3}) if project else {"entries": []}
            workspace_root = (status.get("workspace") or {}).get("root") or ""
            project_path = os.path.realpath(os.path.join(workspace_root, project)) if project else ""
            live_state = core.read_live_swb_state(project_path) if project_path else {"parameters": [], "nodes": []}
            GLib.idle_add(self.apply_refresh, status, paths, project, tree.get("entries") or [], live_state, manual)
        except Exception as error:
            GLib.idle_add(self.apply_refresh_error, error_text(error))

    def apply_refresh(self, status, paths, project, entries, live_state, manual):
        self.refreshing = False
        self.status = status
        self.active_project = project
        self.current_live_state = live_state or {"parameters": [], "nodes": []}
        if paths != self.project_paths:
            self.project_paths = paths
            self.project_combo.handler_block_by_func(self.on_project_changed)
            self.project_combo.remove_all()
            for path in paths:
                self.project_combo.append_text(path)
            self.project_combo.set_active(paths.index(project) if project in paths else -1)
            self.project_combo.handler_unblock_by_func(self.on_project_changed)
            self.onboarding_project_paths = list(paths)
            self.onboarding_project_combo.remove_all()
            for path in paths:
                self.onboarding_project_combo.append_text(path)
            self.onboarding_project_combo.set_active(paths.index(project) if project in paths else (0 if paths else -1))

        self.dashboard_project_name_label.set_text(u"%s\n%s" % (
            as_text(project or u"未发现工程"),
            as_text(os.path.realpath(os.path.join((status.get("workspace") or {}).get("root") or "", project))) if project else u"请先选择工程",
        ))
        self.workspace_project_count_label.set_text(as_text(len(paths)))
        system = status.get("system") or {}
        workspace = status.get("workspace") or {}
        self.workspace_root = workspace.get("root") or ""
        self.system_label.set_text(u"Connector v%s · %s@%s" % (status.get("connectorVersion", "?"), system.get("user", "user"), system.get("hostname", "linux")))
        self.workspace_label.set_text(u"工作区：%s" % workspace.get("root", u"—"))
        signature = tuple((item.get("relativePath"), item.get("modifiedAt"), item.get("size")) for item in entries)
        changed = self.last_signature is not None and signature != self.last_signature
        self.last_signature = signature
        self.change_label.set_text(u"最近同步：%s%s" % (time.strftime("%H:%M:%S"), u" · 检测到变化" if changed else u""))
        self.render_files(entries)
        self.render_live_state(live_state)
        if manual and self.project_session_started:
            self.append_chat(u"EmberTCAD", u"已刷新工程：%s" % as_text(project))
        self.maybe_load_project_results()
        if self.pending_project_read == project and not self.project_report_loading:
            self.pending_project_read = None
            GLib.timeout_add(250, self.on_read_workspace_project)
        return False

    def apply_refresh_error(self, message):
        self.refreshing = False
        self.system_label.set_text(u"Connector：%s" % as_text(message))
        return False

    def maybe_load_project_results(self):
        if self.ai_busy or self.results_loading or not self.project_session_started or not self.workspace_root or not self.active_project:
            return
        now = time.time()
        if self.results_project == self.active_project and now - self.last_results_load < 30:
            return
        self.results_loading = True
        self.last_results_load = now
        project = self.active_project
        try:
            project_path = self.project_absolute_path()
        except Exception:
            self.results_loading = False
            return
        start_thread(self.project_results_worker, (project, project_path))

    def project_results_worker(self, project, project_path):
        try:
            result = core.ai_rpc({"action": "project-results", "project": project_path})
            GLib.idle_add(self.apply_project_results, project, result)
        except Exception as error:
            GLib.idle_add(self.apply_project_results_error, error_text(error))

    def apply_project_results(self, project, result):
        self.results_loading = False
        if project != self.active_project or self.ai_busy:
            return False
        self.results_project = project
        values = result.get("vthResults") or []
        values = sorted(values, key=lambda item: int(item.get("node") or 0))
        result_signature = tuple(
            (item.get("node"), item.get("structureNode"), item.get("con_pwell"), item.get("vthMaxGm"))
            for item in values
        )
        if result_signature == self.loaded_result_signature:
            return False
        self.loaded_result_signature = result_signature
        self.iteration_store.clear()
        self.iteration_rows = {}
        self.trajectory_points = []
        for index, item in enumerate(values):
            run = index + 1
            concentration_text = as_text(item.get("con_pwell") or u"—")
            structure_node = item.get("structureNode")
            device_node = item.get("node")
            try:
                vth = float(item.get("vthMaxGm"))
                concentration = float(item.get("con_pwell"))
            except (TypeError, ValueError):
                continue
            iterator = self.iteration_store.append((
                str(run), concentration_text,
                u"✓ n%s" % structure_node if structure_node is not None else u"✓",
                u"✓ n%s" % device_node,
                "%.6f" % vth,
                u"已有结果",
                u"复用",
            ))
            self.iteration_rows[run] = iterator
            self.trajectory_points.append({
                "run": run,
                "concentration": concentration,
                "concentrationText": concentration_text,
                "vth": vth,
                "withinTolerance": False,
                "structureNode": structure_node,
                "node": device_node,
            })
        self.task_target = None
        self.task_tolerance = None
        self.refresh_charts()
        self.update_best_summary()
        if values:
            self.set_task_title(u"工程已连接：%s" % as_text(project))
            self.set_task_detail(u"请先查看工程阅读报告，再创建任务。已有结果会在需要时作为可复用证据。")
            self.set_task_state(u"工程就绪", "success")
            self.set_task_progress(0.0, "READY")
            self.add_recent_event("result", u"已索引工程中的 %d 组可复用结果。" % len(self.trajectory_points))
        return False

    def apply_project_results_error(self, message):
        self.results_loading = False
        self.add_recent_event("error", u"结果索引失败：%s" % as_text(message))
        return False

    def render_files(self, entries):
        self.file_store.clear()
        files = [item for item in entries if item.get("type") == "file"]
        files.sort(key=lambda item: item.get("modifiedAt") or "", reverse=True)
        for item in files[:180]:
            extension = (item.get("extension") or "file").upper()[:5]
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
        self.selected_node_label.set_text(u"已选节点：%s · %s" % (self.selected_node, self.selected_node_tool))

    def on_parameter_action(self, action):
        name = self.param_name_entry.get_text().strip()
        value = self.param_value_entry.get_text().strip()
        if not name or not value:
            self.show_message(u"请输入参数名称和值。")
            return
        step = None
        if action == "add-parameter":
            try:
                step = int(self.param_step_entry.get_text().strip())
            except ValueError:
                self.show_message(u"新增参数时，步骤必须是整数。")
                return
        names = {"set-parameter": u"替换参数值", "add-values": u"添加实验取值", "add-parameter": u"新增参数"}
        self.append_chat(u"EmberTCAD", u"正在%s：%s=%s …" % (names[action], name, value))
        start_thread(self.parameter_worker, (action, name, value, step))

    def parameter_worker(self, action, name, value, step):
        try:
            kwargs = {"name": name, "value": value}
            if step is not None:
                kwargs["step"] = step
            result = core.live_action(action, self.project_absolute_path(), **kwargs)
            if action == "set-parameter":
                message = u"参数 %s：%s → %s" % (name, u",".join(result.get("previous") or []), u",".join(result.get("current") or []))
            elif action == "add-values":
                message = u"参数 %s 已添加取值：%s" % (name, u",".join(result.get("added") or []))
            else:
                message = u"已在步骤 %s 新增参数 %s：%s" % (result.get("step"), name, u",".join(result.get("current") or []))
            GLib.idle_add(self.action_success, u"%s；SWB 已自动重载。" % message)
        except Exception as error:
            GLib.idle_add(self.action_error, error_text(error))

    def on_run_node(self, *_args):
        raw_node = self.run_node_entry.get_text().strip()
        if raw_node:
            try:
                node = int(raw_node)
            except ValueError:
                self.show_message(u"节点号必须是整数。")
                return
        elif self.selected_node is not None:
            node = self.selected_node
        else:
            self.show_message(u"请在列表中选择节点，或直接输入节点号。")
            return
        tool = self.selected_node_tool if node == self.selected_node else ""
        self.selected_node = node
        self.run_node_entry.set_text(str(node))
        self.selected_node_label.set_text(u"已选节点：%s%s" % (node, u" · %s" % tool if tool else u""))
        self.append_chat(u"EmberTCAD", u"正在向 SWB 提交节点 %s（%s）…" % (node, tool or u"未指定工具"))
        start_thread(self.run_node_worker, (node,))

    def run_node_worker(self, node):
        try:
            result = core.live_action("run-node", self.project_absolute_path(), node=node)
            GLib.idle_add(self.action_success, u"节点 %s 已提交给 gsub（PID %s）。状态以 .sta 和结果文件为准；日志：%s" % (node, result.get("pid"), result.get("log")))
        except Exception as error:
            GLib.idle_add(self.action_error, error_text(error))

    def on_refresh_all(self, *_args):
        start_thread(self.refresh_all_worker)

    def refresh_all_worker(self):
        try:
            result = core.live_action("refresh-swb", self.project_absolute_path())
            GLib.idle_add(self.action_success, u"已同步工程并刷新 %s 个 SWB 窗口。" % result.get("swbWindowsRefreshed", 0))
        except Exception as error:
            GLib.idle_add(self.action_error, error_text(error))

    def action_success(self, message):
        self.append_chat(u"EmberTCAD", message)
        self.add_recent_event("result", message)
        self.results_project = None
        self.refresh(False)
        return False

    def action_error(self, message):
        self.append_chat(u"EmberTCAD", u"操作失败：%s" % as_text(message))
        self.add_recent_event("error", message)
        self.show_message(message, Gtk.MessageType.ERROR)
        return False

    def on_file_activated(self, _view, path, _column):
        model = self.file_view.get_model()
        iterator = model.get_iter(path)
        relative_path = model.get_value(iterator, 3)
        extension = os.path.splitext(relative_path)[1].lower().lstrip(".")
        if extension not in core.TEXT_EXTENSIONS:
            self.show_message(u"这个文件需要专用查看器，当前先支持文本预览。")
            return
        start_thread(self.load_preview_worker, (relative_path,))

    def load_preview_worker(self, relative_path):
        try:
            result = core.rpc("file.readText", {"relativePath": relative_path})
            GLib.idle_add(self.show_preview, relative_path, result.get("content") or "")
        except Exception as error:
            GLib.idle_add(self.show_message, error_text(error))

    def show_preview(self, title, content):
        dialog = Gtk.Dialog(title=as_text(title), transient_for=self, flags=Gtk.DialogFlags.DESTROY_WITH_PARENT)
        dialog.add_button(u"关闭", Gtk.ResponseType.CLOSE)
        dialog.set_default_size(900, 650)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_monospace(True)
        view.get_buffer().set_text(as_text(content))
        scroll = scroller(view, Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        dialog.get_content_area().pack_start(scroll, True, True, 0)
        dialog.show_all()
        dialog.run()
        dialog.destroy()
        return False

    def show_message(self, message, message_type=Gtk.MessageType.INFO):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=message_type,
            buttons=Gtk.ButtonsType.OK,
            text=as_text(message),
        )
        dialog.run()
        dialog.destroy()
        return False

    def show_planned_message(self, title, message):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=as_text(title),
        )
        dialog.format_secondary_text(as_text(message))
        dialog.run()
        dialog.destroy()


def load_css():
    if not os.path.isfile(CSS_PATH):
        return
    provider = Gtk.CssProvider()
    provider.load_from_path(CSS_PATH)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def run_gui():
    load_css()
    window = AssistantWindow()
    window.show_all()
    GLib.idle_add(window.apply_initial_mode)
    Gtk.main()


def main():
    if "--probe" in sys.argv:
        core.APP_VERSION = APP_VERSION
        core.probe()
        return 0
    lock = core.acquire_single_instance()
    if lock is None:
        print("EmberTCAD is already running")
        return 0
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
