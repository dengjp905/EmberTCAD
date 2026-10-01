#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function

import datetime
import base64
import difflib
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import stat as stat_module
import subprocess
import sys
import tempfile
import time


CONNECTOR_VERSION = "0.7.0"
WORKSPACE_ROOT = os.path.realpath(os.environ.get("AITCAD_PROJECT_ROOT", os.path.expanduser("~/STDB")))
APP_DATA_ROOT = os.path.expanduser("~/.local/share/aitcad")
CACHE_ROOT = os.path.join(APP_DATA_ROOT, "cache")
RUN_ROOT = os.path.join(APP_DATA_ROOT, "runs")
BACKUP_ROOT = os.path.join(APP_DATA_ROOT, "backups")
CREATION_ROOT = os.path.join(APP_DATA_ROOT, "project-creations")
ACCESS_POLICY_PATH = os.path.join(APP_DATA_ROOT, "access-policy.json")
ACCESS_AUDIT_ROOT = os.path.join(APP_DATA_ROOT, "access-audit")
NEW_PROJECT_ROOT = os.path.join(WORKSPACE_ROOT, "aitcad_workspaces")
MAX_TREE_ENTRIES = 500
MAX_TEXT_BYTES = 512 * 1024
MAX_SEARCH_FILES = 250
MAX_SEARCH_BYTES_PER_FILE = 1024 * 1024
MAX_SEARCH_HITS = 100
MAX_PLT_BYTES = 4 * 1024 * 1024
MAX_PLT_POINTS = 10000
MAX_TDR_BYTES = 2 * 1024 * 1024 * 1024
MAX_TDR_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TDR_CUT_POINTS = 20000
MAX_TDR_CUT_COORDINATE = 1000000.0
MAX_EDIT_BYTES = 24 * 1024
MAX_TEMPLATE_FILES = 5000
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000
EDITABLE_EXTENSIONS = set(("cmd", "tcl", "par", "txt", "scm"))
RUN_RESULT_EXTENSIONS = set((".tdr", ".plt", ".log", ".out", ".err", ".sta"))
PROJECT_TEMPLATES = {
    "nmos-teaching": "nmos_study/nmos_project",
}
GENERATED_TOOLS = set(("sde", "sdevice", "sprocess", "snmesh", "svisual", "inspect"))
GENERATED_SOURCE_EXTENSIONS = set((".cmd", ".scm", ".par", ".tcl", ".txt", ".prf"))
GENERATED_INPUT_SUFFIX = {"sde": "_dvs.cmd", "sdevice": "_des.cmd", "sprocess": "_fps.cmd",
                          "snmesh": "_msh.cmd", "svisual": "_vis.tcl", "inspect": "_ins.cmd"}
TEXT_EXTENSIONS = set((
    "cmd", "log", "out", "err", "tcl", "par", "dat", "txt", "sta", "prf",
    "job", "xml", "csv", "bash", "sh", "scm", "grd", "bnd",
))

CONFIGURED_SENTAURUS_ROOT = os.environ.get("AITCAD_RUN_STROOT", "/usr/synopsys/sentaurus/O_2018.06-SP2")
CONFIGURED_SENTAURUS_RELEASE = os.environ.get(
    "AITCAD_SENTAURUS_RELEASE",
    (os.path.basename(os.path.realpath(os.path.join(CONFIGURED_SENTAURUS_ROOT, "tcad", "current")))
     if os.path.exists(os.path.join(CONFIGURED_SENTAURUS_ROOT, "tcad", "current"))
     else os.path.basename(CONFIGURED_SENTAURUS_ROOT.rstrip(os.sep)).replace("_", "-", 1)),
)
_CONFIGURED_RELEASE_YEAR = re.search(r"(?:19|20)\d{2}", CONFIGURED_SENTAURUS_RELEASE)
SENTAURUS_PROFILES = ({
    "version": os.environ.get(
        "AITCAD_SENTAURUS_VERSION",
        _CONFIGURED_RELEASE_YEAR.group(0) if _CONFIGURED_RELEASE_YEAR else "configured",
    ),
    "release": CONFIGURED_SENTAURUS_RELEASE,
    "root": CONFIGURED_SENTAURUS_ROOT,
    "stdb": WORKSPACE_ROOT,
},)

COMMANDS = ("swb", "sdevice", "sde", "snmesh", "svisual", "inspect")
SVISUAL_TIMEOUT_SECONDS = 70
GCLEANUP_TIMEOUT_SECONDS = 60


class ConnectorError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def utc_time(timestamp):
    if hasattr(datetime, "timezone"):
        return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return datetime.datetime.utcfromtimestamp(timestamp).replace(microsecond=0).isoformat() + "Z"


def release_name():
    for candidate in ("/etc/centos-release", "/etc/redhat-release", "/etc/os-release"):
        try:
            with open(candidate, "r") as stream:
                return stream.readline().strip()
        except IOError:
            pass
    return platform.platform()


def is_inside_root(path):
    root = os.path.realpath(WORKSPACE_ROOT)
    resolved = os.path.realpath(path)
    return resolved == root or resolved.startswith(root + os.sep)


def resolve_relative_unchecked(relative_path):
    relative_path = relative_path or ""
    if os.path.isabs(relative_path):
        raise ConnectorError("ABSOLUTE_PATH_DENIED", "Only paths relative to the configured workspace are allowed")
    resolved = os.path.realpath(os.path.join(WORKSPACE_ROOT, relative_path))
    if not is_inside_root(resolved):
        raise ConnectorError("PATH_OUTSIDE_WORKSPACE", "The requested path is outside the configured workspace")
    return resolved


def normalized_workspace_path(path):
    return os.path.relpath(path, WORKSPACE_ROOT).replace(os.sep, "/")


def read_access_policy():
    if not os.path.isfile(ACCESS_POLICY_PATH):
        return {"version": 1, "restrictedProjects": []}
    try:
        with open(ACCESS_POLICY_PATH, "rb") as stream:
            policy = json.loads(stream.read().decode("utf-8"))
    except (IOError, OSError, ValueError, UnicodeDecodeError) as error:
        raise ConnectorError("ACCESS_POLICY_INVALID", "The app-owned access policy cannot be read safely: " + str(error))
    if not isinstance(policy, dict) or policy.get("version") != 1 or not isinstance(policy.get("restrictedProjects"), list):
        raise ConnectorError("ACCESS_POLICY_INVALID", "The app-owned access policy has an unsupported schema")
    restricted = []
    seen = set()
    for item in policy["restrictedProjects"]:
        if not isinstance(item, dict) or not isinstance(item.get("relativePath"), type(u"")):
            raise ConnectorError("ACCESS_POLICY_INVALID", "Every restricted project entry must contain a relativePath")
        target = resolve_relative_unchecked(item["relativePath"])
        relative_path = normalized_workspace_path(target)
        if relative_path in seen:
            continue
        seen.add(relative_path)
        restricted.append({
            "relativePath": relative_path,
            "restrictedAt": item.get("restrictedAt"),
        })
    restricted.sort(key=lambda item: item["relativePath"])
    return {"version": 1, "restrictedProjects": restricted}


def restricted_project_for_path(path, policy=None):
    policy = policy or read_access_policy()
    relative_path = normalized_workspace_path(os.path.realpath(path))
    for item in policy["restrictedProjects"]:
        project_path = item["relativePath"]
        if relative_path == project_path or relative_path.startswith(project_path + "/"):
            return item
    return None


def resolve_relative(relative_path, enforce_access=True):
    resolved = resolve_relative_unchecked(relative_path)
    if enforce_access:
        restricted = restricted_project_for_path(resolved)
        if restricted:
            raise ConnectorError("PROJECT_ACCESS_RESTRICTED", "Connector access to this project has been revoked; restore it through an approved project access plan")
    return resolved


def extension_counts(directory):
    counts = {}
    for current, directories, files in os.walk(directory):
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(current, name))]
        for name in files:
            extension = os.path.splitext(name)[1].lower().lstrip(".") or "other"
            counts[extension] = counts.get(extension, 0) + 1
    return counts


def count_workspace(root):
    file_count = 0
    directory_count = 0
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(current, name))]
        directory_count += len(directories)
        file_count += len(files)
    return file_count, directory_count


def discover_projects(root):
    projects = []
    for current, directories, files in os.walk(root):
        relative = os.path.relpath(current, root)
        parts = [] if relative == "." else relative.split(os.sep)
        if parts and parts[0] == "tmp":
            directories[:] = []
            continue
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(current, name))]
        if ".project" not in files:
            continue
        counts = extension_counts(current)
        stat = os.stat(current)
        projects.append(
            {
                "name": os.path.basename(current),
                "relativePath": relative.replace(os.sep, "/"),
                "modifiedAt": utc_time(stat.st_mtime),
                "files": sum(counts.values()),
                "artifacts": {
                    "commands": counts.get("cmd", 0),
                    "logs": counts.get("log", 0) + counts.get("out", 0) + counts.get("err", 0),
                    "tdr": counts.get("tdr", 0),
                    "plots": counts.get("plt", 0),
                    "scripts": counts.get("tcl", 0),
                },
            }
        )
    projects.sort(key=lambda item: item["modifiedAt"], reverse=True)
    return projects


def access_policy_fingerprint(policy):
    paths = sorted(item["relativePath"] for item in policy["restrictedProjects"])
    return hashlib.sha256(("\n".join(paths) + "\nproject-access-policy-v1").encode("utf-8")).hexdigest()


def write_access_policy(policy):
    if not os.path.isdir(APP_DATA_ROOT):
        os.makedirs(APP_DATA_ROOT, 0o700)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".access-policy-", dir=APP_DATA_ROOT)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(policy, ensure_ascii=True, indent=2, sort_keys=True).encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        if hasattr(os, "replace"):
            os.replace(temporary_path, ACCESS_POLICY_PATH)
        else:
            os.rename(temporary_path, ACCESS_POLICY_PATH)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def write_access_audit(record):
    if not os.path.isdir(ACCESS_AUDIT_ROOT):
        os.makedirs(ACCESS_AUDIT_ROOT, 0o700)
    stamp = (record.get("changedAt") or utc_time(time.time())).replace(":", "")
    path = os.path.join(ACCESS_AUDIT_ROOT, stamp + "-" + record["approvalToken"][:8] + ".json")
    with open(path, "wb") as stream:
        stream.write(json.dumps(record, ensure_ascii=True, indent=2, sort_keys=True).encode("ascii"))
        stream.flush()
        os.fsync(stream.fileno())
    return path


def project_access_plan(params):
    params = params or {}
    action = params.get("action") or ""
    if action not in ("restrict", "restore"):
        raise ConnectorError("PROJECT_ACCESS_ACTION_INVALID", "Project access action must be restrict or restore")
    target = resolve_relative_unchecked(params.get("relativePath") or "")
    relative_path = normalized_workspace_path(target)
    projects = discover_projects(os.path.realpath(WORKSPACE_ROOT))
    project = next((item for item in projects if item["relativePath"] == relative_path), None)
    if not project:
        raise ConnectorError("PROJECT_NOT_FOUND", "The selected path is not a discovered Sentaurus Workbench project")
    policy = read_access_policy()
    restricted_paths = set(item["relativePath"] for item in policy["restrictedProjects"])
    is_restricted = relative_path in restricted_paths
    if action == "restrict" and is_restricted:
        raise ConnectorError("PROJECT_ALREADY_RESTRICTED", "Connector access to this project is already revoked")
    if action == "restore" and not is_restricted:
        raise ConnectorError("PROJECT_ALREADY_ALLOWED", "Connector access to this project is already enabled")
    allowed_count = len([item for item in projects if item["relativePath"] not in restricted_paths])
    if action == "restrict" and allowed_count <= 1:
        raise ConnectorError("LAST_PROJECT_ACCESS_REQUIRED", "At least one project must remain accessible so the policy can be managed safely")
    policy_fingerprint = access_policy_fingerprint(policy)
    approval_token = hashlib.sha256((action + "|" + relative_path + "|" + policy_fingerprint + "|project-access-v1").encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "mode": "approval-required",
        "action": action,
        "relativePath": relative_path,
        "project": project,
        "policyFingerprint": policy_fingerprint,
        "approvalToken": approval_token,
        "requiresApproval": True,
        "recoverable": True,
        "allowedProjectCount": allowed_count,
        "resultingAllowedProjectCount": allowed_count - 1 if action == "restrict" else allowed_count + 1,
    }


def project_set_access(params):
    params = params or {}
    plan = project_access_plan(params)
    if params.get("approvalToken") != plan["approvalToken"] or params.get("confirm") is not True:
        raise ConnectorError("PROJECT_ACCESS_APPROVAL_REQUIRED", "Changing project access requires the current policy token and explicit confirmation")
    policy = read_access_policy()
    relative_path = plan["relativePath"]
    changed_at = utc_time(time.time())
    if plan["action"] == "restrict":
        policy["restrictedProjects"].append({"relativePath": relative_path, "restrictedAt": changed_at})
        policy["restrictedProjects"].sort(key=lambda item: item["relativePath"])
        status = "restricted"
    else:
        policy["restrictedProjects"] = [item for item in policy["restrictedProjects"] if item["relativePath"] != relative_path]
        status = "restored"
    write_access_policy(policy)
    audit_warning = None
    audit_path = None
    record = {
        "action": plan["action"],
        "status": status,
        "relativePath": relative_path,
        "approvalToken": plan["approvalToken"],
        "previousPolicyFingerprint": plan["policyFingerprint"],
        "resultingPolicyFingerprint": access_policy_fingerprint(policy),
        "changedAt": changed_at,
        "recoverable": True,
    }
    try:
        audit_path = write_access_audit(record)
    except (IOError, OSError) as error:
        audit_warning = "Access policy changed, but the app-owned audit record could not be written: " + str(error)
    return {
        "ok": True,
        "status": status,
        "action": plan["action"],
        "relativePath": relative_path,
        "changedAt": changed_at,
        "approvalToken": plan["approvalToken"],
        "recoverable": True,
        "allowedProjectCount": plan["resultingAllowedProjectCount"],
        "auditPath": audit_path,
        "auditWarning": audit_warning,
    }


def sentaurus_status():
    profiles = []
    for profile in SENTAURUS_PROFILES:
        commands = {}
        for command in COMMANDS:
            path = os.path.join(profile["root"], "bin", command)
            commands[command] = path if os.path.isfile(path) and os.access(path, os.X_OK) else None
        item = dict(profile)
        item["installed"] = os.path.isdir(profile["root"])
        item["commands"] = commands
        profiles.append(item)
    return profiles


def system_probe(_params=None):
    root = os.path.realpath(WORKSPACE_ROOT)
    exists = os.path.isdir(root)
    file_count, directory_count = count_workspace(root) if exists else (0, 0)
    projects = discover_projects(root) if exists else []
    policy = read_access_policy()
    restricted_by_path = dict((item["relativePath"], item) for item in policy["restrictedProjects"])
    allowed_projects = [project for project in projects if project["relativePath"] not in restricted_by_path]
    restricted_projects = []
    for project in projects:
        restriction = restricted_by_path.get(project["relativePath"])
        if restriction:
            item = dict(project)
            item["restrictedAt"] = restriction.get("restrictedAt")
            restricted_projects.append(item)
    return {
        "ok": True,
        "connectorVersion": CONNECTOR_VERSION,
        "mode": "approval-gated",
        "system": {
            "hostname": socket.gethostname(),
            "user": os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown",
            "os": release_name(),
            "kernel": platform.release(),
            "python": platform.python_version(),
        },
        "sentaurus": sentaurus_status(),
        "workspace": {
            "name": os.path.basename(root),
            "root": root,
            "exists": exists,
            "fileCount": file_count,
            "directoryCount": directory_count,
        },
        "projects": allowed_projects,
        "restrictedProjects": restricted_projects,
        "accessPolicy": {
            "recoverable": True,
            "restrictedCount": len(restricted_projects),
            "fingerprint": access_policy_fingerprint(policy),
        },
    }


def project_tree(params):
    params = params or {}
    relative_path = params.get("relativePath") or ""
    try:
        max_depth = int(params.get("maxDepth", 2))
    except (TypeError, ValueError):
        raise ConnectorError("INVALID_DEPTH", "maxDepth must be an integer")
    max_depth = max(1, min(max_depth, 4))
    target = resolve_relative(relative_path)
    if not os.path.isdir(target):
        raise ConnectorError("DIRECTORY_NOT_FOUND", "The requested project directory does not exist")

    entries = []
    policy = read_access_policy()
    target_depth = target.rstrip(os.sep).count(os.sep)
    for current, directories, files in os.walk(target):
        current_depth = current.rstrip(os.sep).count(os.sep) - target_depth
        directories[:] = sorted(
            name for name in directories if not os.path.islink(os.path.join(current, name))
        )
        if current_depth >= max_depth:
            directories[:] = []

        names = [(name, True) for name in directories] + [(name, False) for name in sorted(files)]
        for name, is_directory in names:
            path = os.path.join(current, name)
            if not is_inside_root(path):
                continue
            if restricted_project_for_path(path, policy):
                if is_directory and name in directories:
                    directories.remove(name)
                continue
            stat = os.lstat(path)
            workspace_relative = os.path.relpath(path, WORKSPACE_ROOT).replace(os.sep, "/")
            entries.append(
                {
                    "name": name,
                    "relativePath": workspace_relative,
                    "type": "directory" if is_directory else "file",
                    "extension": "" if is_directory else os.path.splitext(name)[1].lower().lstrip("."),
                    "size": 0 if is_directory else stat.st_size,
                    "modifiedAt": utc_time(stat.st_mtime),
                }
            )
            if len(entries) >= MAX_TREE_ENTRIES:
                return {
                    "ok": True,
                    "root": os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/"),
                    "entries": entries,
                    "truncated": True,
                }

    return {
        "ok": True,
        "root": os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/"),
        "entries": entries,
        "truncated": False,
    }


def decode_text(data):
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1"), "latin-1"


def read_text_file(params):
    params = params or {}
    relative_path = params.get("relativePath") or ""
    target = resolve_relative(relative_path)
    if not os.path.isfile(target):
        raise ConnectorError("FILE_NOT_FOUND", "The requested file does not exist")
    if os.path.islink(target):
        raise ConnectorError("SYMLINK_DENIED", "Symbolic links cannot be previewed")

    extension = os.path.splitext(target)[1].lower().lstrip(".")
    if extension not in TEXT_EXTENSIONS:
        raise ConnectorError("FILE_TYPE_NOT_ALLOWED", "This file type requires a dedicated viewer")

    total_size = os.path.getsize(target)
    with open(target, "rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    truncated = len(data) > MAX_TEXT_BYTES
    data = data[:MAX_TEXT_BYTES]
    if b"\x00" in data:
        raise ConnectorError("BINARY_FILE_DENIED", "Binary files cannot be opened in the text preview")

    content, encoding = decode_text(data)
    stat = os.stat(target)
    return {
        "ok": True,
        "name": os.path.basename(target),
        "relativePath": os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/"),
        "extension": extension,
        "encoding": encoding,
        "size": total_size,
        "readBytes": len(data),
        "modifiedAt": utc_time(stat.st_mtime),
        "truncated": truncated,
        "content": content,
    }


def editable_text_target(relative_path):
    relative_path = relative_path or ""
    if os.path.isabs(relative_path):
        raise ConnectorError("ABSOLUTE_PATH_DENIED", "Only workspace-relative files may be edited")
    lexical_path = os.path.abspath(os.path.join(WORKSPACE_ROOT, relative_path))
    if os.path.islink(lexical_path):
        raise ConnectorError("SYMLINK_DENIED", "Symbolic links cannot be edited")
    target = resolve_relative(relative_path)
    if not os.path.isfile(target):
        raise ConnectorError("FILE_NOT_FOUND", "The requested editable file does not exist")
    extension = os.path.splitext(target)[1].lower().lstrip(".")
    if extension not in EDITABLE_EXTENSIONS:
        raise ConnectorError("FILE_EDIT_TYPE_DENIED", "Only approved TCAD input text types may be edited")
    if os.path.getsize(target) > MAX_EDIT_BYTES:
        raise ConnectorError("FILE_EDIT_TOO_LARGE", "Editable files are limited to 24 KB")
    return target, extension


def normalized_edit_content(value):
    if not isinstance(value, type(u"")):
        try:
            value = value.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise ConnectorError("EDIT_CONTENT_INVALID", "Edited content must be UTF-8 text")
    encoded = value.encode("utf-8")
    if len(encoded) > MAX_EDIT_BYTES:
        raise ConnectorError("EDIT_CONTENT_TOO_LARGE", "Edited content is limited to 24 KB")
    if b"\x00" in encoded:
        raise ConnectorError("BINARY_EDIT_DENIED", "Binary content cannot be written")
    return value, encoded


def create_backup_directory(approval_token):
    if not os.path.isdir(BACKUP_ROOT):
        os.makedirs(BACKUP_ROOT, 0o700)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    for attempt in range(100):
        suffix = hashlib.sha256((approval_token + "|" + repr(time.time()) + "|" + str(os.getpid()) + "|" + str(attempt)).encode("utf-8")).hexdigest()[:8]
        backup_dir = os.path.join(BACKUP_ROOT, timestamp + "-" + suffix)
        try:
            os.mkdir(backup_dir, 0o700)
            return backup_dir
        except OSError:
            if os.path.exists(backup_dir):
                continue
            raise
    raise ConnectorError("BACKUP_CREATE_BUSY", "A unique app-owned backup directory could not be allocated")


def file_write_plan(params):
    params = params or {}
    relative_path = params.get("relativePath") or ""
    target, extension = editable_text_target(relative_path)
    content, encoded_content = normalized_edit_content(params.get("content") or u"")
    with open(target, "rb") as stream:
        original_bytes = stream.read(MAX_EDIT_BYTES + 1)
    if len(original_bytes) > MAX_EDIT_BYTES or b"\x00" in original_bytes:
        raise ConnectorError("FILE_EDIT_DENIED", "The source file is not an approved editable text file")
    original, encoding = decode_text(original_bytes)
    if encoding != "utf-8":
        raise ConnectorError("FILE_ENCODING_EDIT_DENIED", "Only UTF-8/ASCII source files may be edited safely")
    if original == content:
        raise ConnectorError("NO_CHANGES", "The edited content is identical to the source file")
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    updated_hash = hashlib.sha256(encoded_content).hexdigest()
    normalized_path = os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/")
    approval_token = hashlib.sha256((normalized_path + "|" + original_hash + "|" + updated_hash + "|file-write-v1").encode("utf-8")).hexdigest()
    diff_lines = list(difflib.unified_diff(
        original.splitlines(True),
        content.splitlines(True),
        fromfile=normalized_path,
        tofile=normalized_path + " (edited)",
        n=3,
    ))
    diff_text = u"".join(diff_lines)
    diff_truncated = len(diff_text.encode("utf-8")) > 48 * 1024
    if diff_truncated:
        diff_text = diff_text.encode("utf-8")[:48 * 1024].decode("utf-8", "ignore") + u"\n... diff truncated ...\n"
    additions = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return {
        "ok": True,
        "mode": "approval-required",
        "relativePath": normalized_path,
        "name": os.path.basename(target),
        "extension": extension,
        "encoding": encoding,
        "originalSize": len(original_bytes),
        "updatedSize": len(encoded_content),
        "originalSha256": original_hash,
        "updatedSha256": updated_hash,
        "approvalToken": approval_token,
        "additions": additions,
        "deletions": deletions,
        "diff": diff_text,
        "diffTruncated": diff_truncated,
        "backupRoot": BACKUP_ROOT,
        "requiresApproval": True,
    }


def file_write_text(params):
    params = params or {}
    plan = file_write_plan(params)
    if params.get("approvalToken") != plan["approvalToken"] or params.get("confirm") is not True:
        raise ConnectorError("WRITE_APPROVAL_REQUIRED", "Writing a file requires its current diff token and explicit confirmation")
    target, _extension = editable_text_target(plan["relativePath"])
    _content, encoded_content = normalized_edit_content(params.get("content") or u"")
    backup_dir = create_backup_directory(plan["approvalToken"])
    backup_path = os.path.join(backup_dir, plan["relativePath"].replace("/", os.sep))
    if not os.path.isdir(os.path.dirname(backup_path)):
        os.makedirs(os.path.dirname(backup_path), 0o700)
    shutil.copy2(target, backup_path)
    source_mode = os.stat(target).st_mode & 0o777
    descriptor, temporary_path = tempfile.mkstemp(prefix=".aitcad-write-", dir=os.path.dirname(target))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded_content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, source_mode)
        if hasattr(os, "replace"):
            os.replace(temporary_path, target)
        else:
            os.rename(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    manifest = {
        "backupId": os.path.basename(backup_dir),
        "operation": "write",
        "relativePath": plan["relativePath"],
        "backupPath": backup_path,
        "originalSha256": plan["originalSha256"],
        "updatedSha256": plan["updatedSha256"],
        "approvalToken": plan["approvalToken"],
        "writtenAt": utc_time(time.time()),
    }
    manifest_path = os.path.join(backup_dir, "manifest.json")
    with open(manifest_path, "wb") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=True, indent=2).encode("ascii"))
    return {
        "ok": True,
        "status": "written",
        "backupId": manifest["backupId"],
        "relativePath": plan["relativePath"],
        "backupPath": backup_path,
        "manifestPath": manifest_path,
        "originalSha256": plan["originalSha256"],
        "updatedSha256": plan["updatedSha256"],
        "writtenAt": manifest["writtenAt"],
        "recoverable": True,
    }


def validated_backup_id(value):
    if not isinstance(value, type(u"")) or not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$", value):
        raise ConnectorError("BACKUP_ID_INVALID", "Backup IDs must be app-issued identifiers")
    return value


def file_backup_record(backup_id):
    backup_id = validated_backup_id(backup_id)
    backup_dir = os.path.realpath(os.path.join(BACKUP_ROOT, backup_id))
    backup_root = os.path.realpath(BACKUP_ROOT)
    if backup_dir == backup_root or not backup_dir.startswith(backup_root + os.sep):
        raise ConnectorError("BACKUP_PATH_DENIED", "The requested backup is outside the app-owned backup root")
    manifest_path = os.path.join(backup_dir, "manifest.json")
    if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
        raise ConnectorError("BACKUP_NOT_FOUND", "The requested backup manifest does not exist")
    try:
        with open(manifest_path, "rb") as stream:
            manifest = json.loads(stream.read().decode("ascii"))
    except (IOError, OSError, ValueError, UnicodeDecodeError) as error:
        raise ConnectorError("BACKUP_MANIFEST_INVALID", "The backup manifest cannot be read safely: " + str(error))
    relative_path = manifest.get("relativePath") or ""
    if not isinstance(relative_path, type(u"")) or os.path.isabs(relative_path):
        raise ConnectorError("BACKUP_MANIFEST_INVALID", "The backup manifest has an invalid target path")
    backup_path = os.path.realpath(os.path.join(backup_dir, relative_path.replace("/", os.sep)))
    declared_path = os.path.realpath(manifest.get("backupPath") or backup_path)
    if backup_path != declared_path or not backup_path.startswith(backup_dir + os.sep):
        raise ConnectorError("BACKUP_PATH_DENIED", "The backup payload path does not match its manifest")
    if not os.path.isfile(backup_path) or os.path.islink(backup_path):
        raise ConnectorError("BACKUP_PAYLOAD_INVALID", "The backup payload is missing or is not a regular file")
    size = os.path.getsize(backup_path)
    if size > MAX_EDIT_BYTES:
        raise ConnectorError("BACKUP_PAYLOAD_TOO_LARGE", "Restorable backups are limited to 24 KB")
    with open(backup_path, "rb") as stream:
        content = stream.read(MAX_EDIT_BYTES + 1)
    content_hash = hashlib.sha256(content).hexdigest()
    expected_hash = manifest.get("originalSha256")
    if expected_hash and content_hash != expected_hash:
        raise ConnectorError("BACKUP_HASH_MISMATCH", "The backup payload no longer matches its recorded hash")
    return {
        "backupId": backup_id,
        "backupDir": backup_dir,
        "backupPath": backup_path,
        "manifestPath": manifest_path,
        "relativePath": relative_path,
        "name": os.path.basename(relative_path),
        "operation": manifest.get("operation") or "write",
        "writtenAt": manifest.get("writtenAt"),
        "originalSha256": content_hash,
        "updatedSha256": manifest.get("updatedSha256"),
        "size": size,
        "content": content,
    }


def file_backup_history(_params=None):
    records = []
    if not os.path.isdir(BACKUP_ROOT):
        return {"ok": True, "backups": records, "truncated": False}
    names = sorted(os.listdir(BACKUP_ROOT), reverse=True)
    truncated = len(names) > 100
    for name in names[:100]:
        if not re.match(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{8}$", name):
            continue
        try:
            record = file_backup_record(name)
        except ConnectorError:
            continue
        records.append(dict((key, value) for key, value in record.items() if key not in ("content", "backupDir")))
        if len(records) >= 50:
            truncated = True
            break
    return {"ok": True, "backups": records, "truncated": truncated}


def file_restore_plan(params):
    params = params or {}
    record = file_backup_record(params.get("backupId") or "")
    target, extension = editable_text_target(record["relativePath"])
    with open(target, "rb") as stream:
        current_bytes = stream.read(MAX_EDIT_BYTES + 1)
    if len(current_bytes) > MAX_EDIT_BYTES or b"\x00" in current_bytes:
        raise ConnectorError("FILE_RESTORE_DENIED", "The current file is not an approved restorable text file")
    current_text, current_encoding = decode_text(current_bytes)
    backup_text, backup_encoding = decode_text(record["content"])
    if current_encoding != "utf-8" or backup_encoding != "utf-8":
        raise ConnectorError("FILE_ENCODING_RESTORE_DENIED", "Only UTF-8/ASCII backups may be restored")
    current_hash = hashlib.sha256(current_bytes).hexdigest()
    backup_hash = record["originalSha256"]
    if current_hash == backup_hash:
        raise ConnectorError("BACKUP_ALREADY_CURRENT", "The selected backup is already identical to the current file")
    approval_token = hashlib.sha256((record["backupId"] + "|" + record["relativePath"] + "|" + current_hash + "|" + backup_hash + "|file-restore-v1").encode("utf-8")).hexdigest()
    diff_lines = list(difflib.unified_diff(
        current_text.splitlines(True),
        backup_text.splitlines(True),
        fromfile=record["relativePath"] + " (current)",
        tofile=record["relativePath"] + " (restore " + record["backupId"] + ")",
        n=3,
    ))
    diff_text = u"".join(diff_lines)
    diff_truncated = len(diff_text.encode("utf-8")) > 48 * 1024
    if diff_truncated:
        diff_text = diff_text.encode("utf-8")[:48 * 1024].decode("utf-8", "ignore") + u"\n... diff truncated ...\n"
    return {
        "ok": True,
        "mode": "approval-required",
        "backupId": record["backupId"],
        "relativePath": record["relativePath"],
        "name": record["name"],
        "extension": extension,
        "writtenAt": record["writtenAt"],
        "currentSha256": current_hash,
        "backupSha256": backup_hash,
        "currentSize": len(current_bytes),
        "backupSize": len(record["content"]),
        "additions": sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")),
        "deletions": sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---")),
        "diff": diff_text,
        "diffTruncated": diff_truncated,
        "approvalToken": approval_token,
        "requiresApproval": True,
        "recoverable": True,
    }


def file_restore_backup(params):
    params = params or {}
    plan = file_restore_plan(params)
    if params.get("approvalToken") != plan["approvalToken"] or params.get("confirm") is not True:
        raise ConnectorError("FILE_RESTORE_APPROVAL_REQUIRED", "Restoring a backup requires the current diff token and explicit confirmation")
    record = file_backup_record(plan["backupId"])
    target, _extension = editable_text_target(plan["relativePath"])
    safety_dir = create_backup_directory(plan["approvalToken"])
    safety_path = os.path.join(safety_dir, plan["relativePath"].replace("/", os.sep))
    if not os.path.isdir(os.path.dirname(safety_path)):
        os.makedirs(os.path.dirname(safety_path), 0o700)
    shutil.copy2(target, safety_path)
    source_mode = os.stat(target).st_mode & 0o777
    descriptor, temporary_path = tempfile.mkstemp(prefix=".aitcad-restore-", dir=os.path.dirname(target))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(record["content"])
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, source_mode)
        if hasattr(os, "replace"):
            os.replace(temporary_path, target)
        else:
            os.rename(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    restored_at = utc_time(time.time())
    safety_manifest = {
        "backupId": os.path.basename(safety_dir),
        "operation": "restore",
        "sourceBackupId": plan["backupId"],
        "relativePath": plan["relativePath"],
        "backupPath": safety_path,
        "originalSha256": plan["currentSha256"],
        "updatedSha256": plan["backupSha256"],
        "approvalToken": plan["approvalToken"],
        "writtenAt": restored_at,
    }
    safety_manifest_path = os.path.join(safety_dir, "manifest.json")
    with open(safety_manifest_path, "wb") as stream:
        stream.write(json.dumps(safety_manifest, ensure_ascii=True, indent=2).encode("ascii"))
    return {
        "ok": True,
        "status": "restored",
        "backupId": plan["backupId"],
        "relativePath": plan["relativePath"],
        "restoredSha256": plan["backupSha256"],
        "safetyBackupId": safety_manifest["backupId"],
        "safetyBackupPath": safety_path,
        "manifestPath": safety_manifest_path,
        "restoredAt": restored_at,
        "recoverable": True,
    }


def validated_project_name(value):
    if not isinstance(value, type(u"")):
        try:
            value = value.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise ConnectorError("PROJECT_NAME_INVALID", "Project name must be UTF-8 text")
    value = value.strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,47}$", value):
        raise ConnectorError("PROJECT_NAME_INVALID", "Project name must contain 3-48 letters, numbers, underscores, or hyphens")
    return value


def generated_project_plan(params):
    """Preflight a model-generated project without trusting its paths or content."""
    params = params or {}
    name = validated_project_name(params.get("name") or "")
    directory = params.get("directory") or ""
    if not isinstance(directory, type(u"")):
        raise ConnectorError("PROJECT_DIRECTORY_INVALID", "Project subdirectory must be UTF-8 text")
    directory = directory.strip()
    if directory and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$", directory):
        raise ConnectorError("PROJECT_DIRECTORY_INVALID", "Use one safe folder name inside aitcad_workspaces")
    target_parent = params.get("targetParent")
    selected_parent = None
    selected_parent_relative = None
    if target_parent not in (None, ""):
        if not isinstance(target_parent, type(u"")):
            raise ConnectorError("PROJECT_TARGET_PARENT_INVALID", "Selected project parent must be UTF-8 text")
        target_parent = target_parent.strip().replace("\\", "/")
        if (not target_parent or os.path.isabs(target_parent) or "\x00" in target_parent or
                any(ord(character) < 32 for character in target_parent)):
            raise ConnectorError("PROJECT_TARGET_PARENT_INVALID", "Select an existing folder inside the configured workspace")
        if target_parent == ".":
            components = []
        else:
            components = target_parent.split("/")
            if any(component in ("", ".", "..") for component in components):
                raise ConnectorError("PROJECT_TARGET_PARENT_INVALID", "Selected project parent cannot escape the configured workspace")
        selected_parent = os.path.realpath(os.path.join(WORKSPACE_ROOT, *components))
        if (not is_inside_root(selected_parent) or not os.path.isdir(selected_parent) or
                os.path.islink(selected_parent)):
            raise ConnectorError("PROJECT_TARGET_PARENT_INVALID", "Select an existing, non-symbolic folder inside the configured workspace")
        if os.path.isfile(os.path.join(selected_parent, "gtree.dat")):
            raise ConnectorError("PROJECT_TARGET_PARENT_INVALID", "Select the folder that will contain the new project, not an existing SWB project")
        selected_parent_relative = os.path.relpath(selected_parent, WORKSPACE_ROOT).replace(os.sep, "/")
    chain = params.get("toolChain") or []
    files = params.get("files") or []
    if not isinstance(chain, list) or not 1 <= len(chain) <= 8:
        raise ConnectorError("PROJECT_FLOW_INVALID", "The project needs 1-8 SWB tools")
    clean_chain = []
    names = set()
    for item in chain:
        if not isinstance(item, dict):
            raise ConnectorError("PROJECT_FLOW_INVALID", "Each SWB tool needs a step and a tool type")
        step = str(item.get("step") or "")
        tool = str(item.get("tool") or "").lower()
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,47}$", step) or step in names or tool not in GENERATED_TOOLS:
            raise ConnectorError("PROJECT_FLOW_INVALID", "Duplicate or unsupported SWB tool step")
        names.add(step)
        clean_chain.append({"step": step, "tool": tool})
    parameters = params.get("parameters") or []
    if not isinstance(parameters, list) or len(parameters) > 24:
        raise ConnectorError("PROJECT_PARAMETERS_INVALID", "At most 24 SWB parameters are supported")
    clean_parameters = []
    parameter_names = set()
    for item in parameters:
        if not isinstance(item, dict):
            raise ConnectorError("PROJECT_PARAMETERS_INVALID", "SWB parameter must have a name, value, and tool step")
        pname = str(item.get("name") or "")
        value = str(item.get("value") or "")
        step = item.get("step")
        if (not re.match(r"^[A-Za-z][A-Za-z0-9_]{0,47}$", pname) or pname in parameter_names or
                not re.match(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$", value) or
                isinstance(step, bool) or not isinstance(step, int) or step < 1 or step > len(clean_chain)):
            raise ConnectorError("PROJECT_PARAMETERS_INVALID", "Invalid or duplicate numeric SWB parameter: %s" % pname)
        parameter_names.add(pname)
        clean_parameters.append({"name": pname, "value": value, "step": step})
    if not isinstance(files, list) or not 1 <= len(files) <= 24:
        raise ConnectorError("PROJECT_FILES_INVALID", "The project needs 1-24 source files")
    clean_files = []
    paths = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise ConnectorError("PROJECT_FILES_INVALID", "Generated files must have a path and text content")
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, type(u"")) or not isinstance(content, type(u"")):
            raise ConnectorError("PROJECT_FILES_INVALID", "Generated source must be UTF-8 text")
        if (not re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$", path) or
                path.startswith(".") or path in paths or
                os.path.splitext(path)[1].lower() not in GENERATED_SOURCE_EXTENSIONS or
                re.match(r"^(?:gtree|gvars|\.project|n\d+_|pp\d+_)", path, re.I)):
            raise ConnectorError("PROJECT_FILE_PATH_DENIED", "Unsafe generated file path: %s" % path)
        raw = content.encode("utf-8")
        if not raw or len(raw) > MAX_EDIT_BYTES or b"\x00" in raw:
            raise ConnectorError("PROJECT_FILES_INVALID", "Source file is empty or exceeds 24 KiB: %s" % path)
        active_source = "\n".join(
            line for line in content.splitlines()
            if not re.match(r"^\s*(?:#|;)", line)
        )
        if re.search(
                r"(?im)(?:\b(?:curl|wget|scp|ssh)\b|\bexec\s+|\(\s*system:command\b|"
                r"^\s*System\s*\(\s*[\"']|\brm\s+-|/home/[^/]+/\.ssh|\.config/aitcad/model\.json)",
                active_source):
            raise ConnectorError("PROJECT_SOURCE_UNSAFE", "Generated source contains a forbidden external action")
        total += len(raw)
        if total > 256 * 1024:
            raise ConnectorError("PROJECT_FILES_INVALID", "Generated source exceeds the total size limit")
        paths.add(path)
        clean_files.append({"path": path, "content": content})
    for item in clean_chain:
        required = item["step"] + GENERATED_INPUT_SUFFIX[item["tool"]]
        if required not in paths:
            raise ConnectorError("PROJECT_INPUT_MISSING", "SWB step %s needs its source file %s" % (item["step"], required))
    built_in_macros = set(("node", "tdr", "tdrdat", "plot", "log", "parameter", "experiment", "relpath", "pwd", "input"))
    for item in clean_files:
        for macro in re.findall(r"@([A-Za-z][A-Za-z0-9_]*)@", item["content"]):
            if macro not in parameter_names and macro.lower() not in built_in_macros:
                raise ConnectorError("PROJECT_PARAMETER_MISSING", "Unknown SWB macro @%s@ in %s" % (macro, item["path"]))
    tool_types = [item["tool"] for item in clean_chain]
    if "sde" in tool_types and "snmesh" in tool_types:
        raise ConnectorError(
            "PROJECT_MESH_FLOW_CONFLICT",
            "Generated SDE already emits the final mesh; do not add a second snmesh producer",
        )
    file_map = dict((item["path"], item["content"]) for item in clean_files)
    for item in clean_chain:
        required = item["step"] + GENERATED_INPUT_SUFFIX[item["tool"]]
        content = file_map.get(required, "")
        if item["tool"] == "sde":
            mesh_calls = re.findall(r"\(sde:build-mesh\b[^)]*\)", content, re.I | re.S)
            if (len(mesh_calls) != 1 or
                    not re.match(r'^\(sde:build-mesh\s+"n@node@_msh"\s*\)$', " ".join(mesh_calls[0].split()) if mesh_calls else "", re.I)):
                raise ConnectorError(
                    "PROJECT_SDE_MESH_INVALID",
                    '%s generated SDE must contain exactly (sde:build-mesh "n@node@_msh")' % CONFIGURED_SENTAURUS_RELEASE,
                )
            if re.search(r"\(sdegeo:define-(?:constant|gaussian|analytical)-profile(?:-region)?\b", content, re.I):
                raise ConnectorError(
                    "PROJECT_SDE_DOPING_NAMESPACE_INVALID",
                    "SDE doping profile definitions must use the sdedr namespace, not sdegeo",
                )
            if re.search(r"\(sdedr:refine-mesh\b", content, re.I):
                raise ConnectorError(
                    "PROJECT_SDE_REFINEMENT_INVALID",
                    "%s evidence does not support sdedr:refine-mesh; use define-refinement-placement" % CONFIGURED_SENTAURUS_RELEASE,
                )
            for call in re.findall(r"\(sdedr:define-refinement-placement\b[^\n]*", content, re.I):
                if re.search(r"\(box\b", call, re.I):
                    raise ConnectorError(
                        "PROJECT_SDE_REFINEMENT_WINDOW_INVALID",
                        "define-refinement-placement must reference a named window, not an unsupported (box ...) expression",
                    )
            for call in re.findall(r"\(sdedr:define-constant-profile-region\b[^)]*\)", content, re.I | re.S):
                if len(re.findall(r'"[^"]*"', call)) != 3:
                    raise ConnectorError(
                        "PROJECT_SDE_PROFILE_INVALID",
                        "define-constant-profile-region needs placement, profile, and region names",
                    )
        elif item["tool"] == "sdevice":
            if re.search(r"(?m)^\s*;", content):
                raise ConnectorError(
                    "PROJECT_SDEVICE_COMMENT_INVALID",
                    "SDevice %s does not accept semicolon comments; use # comments" % CONFIGURED_SENTAURUS_RELEASE,
                )
            if re.search(r"(?is)\bSRH\s*\([^)]*\bLifetimes?\s*=", content):
                raise ConnectorError(
                    "PROJECT_SDEVICE_MODEL_INVALID",
                    "Do not put Lifetimes= inside the SDevice SRH model options",
                )
            if re.search(r"(?is)\bPlot\s*\{[^}]*\bCurrentDensity\b", content):
                raise ConnectorError(
                    "PROJECT_SDEVICE_PLOT_INVALID",
                    "CurrentDensity is not a verified %s Plot field; use eCurrent/hCurrent/TotalCurrent" % CONFIGURED_SENTAURUS_RELEASE,
                )
            if re.search(r"(?is)\bPlot\s*\{[^}]*\bNetActive\b", content):
                raise ConnectorError(
                    "PROJECT_SDEVICE_PLOT_INVALID",
                    "NetActive is not a verified %s SDevice Plot field; use Doping, DonorConcentration, or AcceptorConcentration" % CONFIGURED_SENTAURUS_RELEASE,
                )
            if re.search(r"(?is)\bCurrentPlot\s*\{[^}]*\b(?:eCurrent|hCurrent|TotalCurrent)\b", content):
                raise ConnectorError(
                    "PROJECT_SDEVICE_CURRENTPLOT_INVALID",
                    "SDevice terminal currents are written by Current=@plot@; do not list eCurrent/hCurrent/TotalCurrent in CurrentPlot",
                )
            required_parameter_file = item["step"] + "_des.par"
            if "@parameter@" in content and required_parameter_file not in paths:
                raise ConnectorError(
                    "PROJECT_SDEVICE_PARAMETER_FILE_MISSING",
                    "SDevice uses @parameter@ but is missing its step-specific %s file" % required_parameter_file,
                )
            tool_index = clean_chain.index(item)
            upstream = next((candidate["step"] for candidate in reversed(clean_chain[:tool_index])
                             if candidate["tool"] in ("sde", "sprocess", "snmesh")), None)
            if upstream is None:
                raise ConnectorError(
                    "PROJECT_SDEVICE_INPUT_MISSING",
                    "A generated SDevice step needs an upstream SDE/SProcess/SMesh grid step",
                )
            expected_macro = "@tdr|%s@" % upstream
            if expected_macro not in content:
                raise ConnectorError(
                    "PROJECT_SDEVICE_DEPENDENCY_INVALID",
                    "SDevice Grid must explicitly use %s instead of a bare @tdr@" % expected_macro,
                )
        elif item["tool"] == "snmesh" and re.search(r"(?is)\brefinement\s*\{\s*window\s*\{", content):
            raise ConnectorError(
                "PROJECT_SMESH_SYNTAX_INVALID",
                "Use the verified %s Definitions/Placements SMesh syntax" % CONFIGURED_SENTAURUS_RELEASE,
            )
    parent = selected_parent or os.path.abspath(os.path.join(NEW_PROJECT_ROOT, directory))
    target = os.path.abspath(os.path.join(parent, name))
    if (not is_inside_root(target) or os.path.islink(parent) or
            (os.path.exists(parent) and not os.path.isdir(parent))):
        raise ConnectorError(
            "PROJECT_TARGET_DENIED",
            u"所选位置不在受管工作区内，或上级路径不是安全目录",
        )
    if os.path.lexists(target):
        raise ConnectorError(
            "PROJECT_TARGET_EXISTS",
            u"目标 %s 已存在；请选择新的工程名或上级目录，旧工程不会被覆盖" % target,
        )
    digest = hashlib.sha256(json.dumps({
        "name": name, "directory": directory, "targetParent": selected_parent_relative,
        "toolChain": clean_chain,
        "parameters": clean_parameters, "files": clean_files,
    }, ensure_ascii=True, sort_keys=True).encode("ascii")).hexdigest()
    return {
        "ok": True, "name": name, "directory": directory,
        "targetParent": selected_parent_relative, "parentPath": parent,
        "targetPath": target,
        "targetRelativePath": os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/"),
        "toolChain": clean_chain,
        "parameters": clean_parameters,
        "files": [{"path": item["path"], "bytes": len(item["content"].encode("utf-8")),
                   "sha256": hashlib.sha256(item["content"].encode("utf-8")).hexdigest()}
                  for item in clean_files],
        "fileCount": len(clean_files), "totalBytes": total,
        "approvalToken": digest, "requiresApproval": True,
    }


def generated_project_create(params):
    """Publish a fresh SWB project atomically after the exact approved preflight."""
    plan = generated_project_plan(params)
    if params.get("confirm") is not True or params.get("approvalToken") != plan["approvalToken"]:
        raise ConnectorError("PROJECT_CREATE_APPROVAL_REQUIRED", "The approved project contents have changed")
    parent = os.path.dirname(plan["targetPath"])
    if plan.get("targetParent") is None:
        if not os.path.isdir(NEW_PROJECT_ROOT):
            os.makedirs(NEW_PROJECT_ROOT, 0o750)
        if os.path.islink(NEW_PROJECT_ROOT) or not is_inside_root(NEW_PROJECT_ROOT):
            raise ConnectorError("PROJECT_ROOT_DENIED", "Managed workspace root is unsafe")
        if parent != NEW_PROJECT_ROOT and not os.path.exists(parent):
            os.mkdir(parent, 0o750)
    if os.path.islink(parent) or not os.path.isdir(parent) or not is_inside_root(parent):
        raise ConnectorError("PROJECT_ROOT_DENIED", "Managed target subdirectory is unsafe")
    lock_path = os.path.join(parent, "." + plan["name"] + ".create.lock")
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise ConnectorError("PROJECT_CREATE_BUSY", "This project is already being created")
    os.close(descriptor)
    staging = tempfile.mkdtemp(prefix=".spark-generate-", dir=parent)
    candidate = os.path.join(staging, plan["name"])
    try:
        os.mkdir(candidate)
        for item in params["files"]:
            path = os.path.join(candidate, item["path"])
            with open(path, "wb") as stream:
                stream.write(item["content"].encode("utf-8"))
        root = os.environ.get("AITCAD_RUN_STROOT", "/usr/synopsys/sentaurus/O_2018.06-SP2")
        gtclsh = os.path.join(root, "bin", "gtclsh")
        if not os.path.isfile(gtclsh):
            raise ConnectorError("SWB_UNAVAILABLE", "The installed SWB gtclsh was not found")
        # The model never supplies Tcl: only allowlisted, locally constructed commands.
        tree = os.path.join(candidate, "gtree.dat")
        script = ["::gtree::New spark_generated"]
        for step_index, item in enumerate(plan["toolChain"]):
            script.append("::gtree::AddTool {%s} {%s}" % (item["step"], item["tool"]))
            if item["tool"] == "sde":
                # Generated projects must also validate without an attached X
                # display.  SWB's stock SDE step otherwise starts the GUI.
                script.append("::gtree::SetStepArg %d {-e}" % step_index)
        script.extend(["::gtree::CreateDefaultScenario", "::gtree::AddPath {}"])
        # ::gtree::AddParam takes the *current* simulation-flow position.  An
        # inserted parameter becomes a virtual step and shifts every tool that
        # follows it.  Therefore adding upstream parameters first silently
        # moves later parameters onto the wrong tool (for example Temperature
        # requested for SDevice ended up on SDE).  Build downstream-to-upstream
        # so every approved 1-based tool position is still stable when used.
        ordered_parameters = sorted(
            enumerate(plan["parameters"]),
            key=lambda pair: (-pair[1]["step"], pair[0]),
        )
        for _parameter_index, item in ordered_parameters:
            script.append("::gtree::AddParam {%s} {%s} %d" % (item["name"], item["value"], item["step"]))
        script.extend(["::gtree::SaveAs {%s}" % tree.replace("}", "\\}"), "puts __SPARK_TREE_OK__"])
        environment = os.environ.copy()
        environment["STROOT"] = root
        environment["STDB"] = WORKSPACE_ROOT
        environment["PATH"] = os.path.join(root, "bin") + os.pathsep + environment.get("PATH", "")
        process = subprocess.Popen([gtclsh], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, cwd=candidate)
        stdout, stderr = process.communicate(("\n".join(script) + "\n").encode("utf-8"))
        if process.returncode or b"__SPARK_TREE_OK__" not in stdout or not os.path.isfile(tree):
            raise ConnectorError("SWB_CREATE_FAILED", stderr.decode("utf-8", "replace")[:900] or "SWB did not save the new flow")
        with open(tree, "rb") as stream:
            tree_text = stream.read().decode("utf-8", "replace")
        for item in plan["parameters"]:
            expected_step = plan["toolChain"][item["step"] - 1]["step"]
            created = re.search(
                r"(?m)^\s*([^\s#]+)\s+%s\s+" % re.escape(item["name"]),
                tree_text,
            )
            if not created:
                raise ConnectorError(
                    "SWB_PARAMETER_MISSING",
                    "SWB did not create approved parameter %s" % item["name"],
                )
            actual_step = created.group(1)
            if actual_step != expected_step:
                raise ConnectorError(
                    "SWB_PARAMETER_WRONG_TOOL",
                    "SWB attached parameter %s to %s, expected %s" % (
                        item["name"], actual_step, expected_step,
                    ),
                )
        if not os.path.exists(os.path.join(candidate, ".project")):
            with open(os.path.join(candidate, ".project"), "wb"):
                pass
        if os.path.lexists(plan["targetPath"]):
            raise ConnectorError("PROJECT_ALREADY_EXISTS", "Target appeared after approval")
        os.rename(candidate, plan["targetPath"])
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        if os.path.exists(lock_path):
            os.unlink(lock_path)
    return {"ok": True, "status": "created", "name": plan["name"],
            "path": plan["targetPath"], "relativePath": plan["targetRelativePath"],
            "fileCount": plan["fileCount"], "toolChain": plan["toolChain"],
            "createdAt": utc_time(time.time())}


def project_template_inventory(root):
    fingerprint = hashlib.sha256()
    file_count = 0
    directory_count = 0
    total_bytes = 0
    file_entries = []
    for current, directories, files in os.walk(root):
        directories.sort()
        files.sort()
        for name in directories:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise ConnectorError("TEMPLATE_SYMLINK_DENIED", "Project templates cannot contain symbolic links")
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            fingerprint.update(("D|" + relative + "\n").encode("utf-8"))
            directory_count += 1
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise ConnectorError("TEMPLATE_SYMLINK_DENIED", "Project templates cannot contain symbolic links")
            file_stat = os.lstat(path)
            if not stat_module.S_ISREG(file_stat.st_mode):
                raise ConnectorError("TEMPLATE_FILE_TYPE_DENIED", "Project templates may only contain regular files and directories")
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            file_count += 1
            total_bytes += file_stat.st_size
            if file_count > MAX_TEMPLATE_FILES or total_bytes > MAX_TEMPLATE_BYTES:
                raise ConnectorError("TEMPLATE_TOO_LARGE", "Project template exceeds the approved file count or size limit")
            fingerprint.update(("F|" + relative + "|" + str(file_stat.st_size) + "\n").encode("utf-8"))
            if len(file_entries) < 200:
                file_entries.append({"path": relative, "bytes": file_stat.st_size})
            with open(path, "rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    fingerprint.update(chunk)
    return {
        "fileCount": file_count,
        "directoryCount": directory_count,
        "totalBytes": total_bytes,
        "fingerprint": fingerprint.hexdigest(),
        "files": file_entries,
        "filesTruncated": file_count > len(file_entries),
    }


def project_create_plan(params):
    params = params or {}
    name = validated_project_name(params.get("name") or u"")
    template_id = params.get("templateId") or "nmos-teaching"
    if template_id not in PROJECT_TEMPLATES:
        raise ConnectorError("PROJECT_TEMPLATE_DENIED", "Only fixed approved project templates may be copied")
    source_relative = PROJECT_TEMPLATES[template_id]
    source = resolve_relative(source_relative)
    if not os.path.isdir(source) or not os.path.isfile(os.path.join(source, ".project")):
        raise ConnectorError("PROJECT_TEMPLATE_INVALID", "The approved template is missing or is not a Sentaurus Workbench project")
    target = os.path.abspath(os.path.join(NEW_PROJECT_ROOT, name))
    if not is_inside_root(target):
        raise ConnectorError("PROJECT_TARGET_DENIED", "New projects must remain inside the configured workspace")
    if os.path.lexists(target):
        raise ConnectorError("PROJECT_ALREADY_EXISTS", "A project or path with this name already exists")
    inventory = project_template_inventory(source)
    target_relative = os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/")
    approval_token = hashlib.sha256((
        template_id + "|" + source_relative + "|" + target_relative + "|" +
        inventory["fingerprint"] + "|project-create-v1"
    ).encode("utf-8")).hexdigest()
    return {
        "ok": True,
        "mode": "approval-required",
        "name": name,
        "templateId": template_id,
        "sourceRelativePath": source_relative,
        "targetRelativePath": target_relative,
        "targetPath": target,
        "fileCount": inventory["fileCount"],
        "directoryCount": inventory["directoryCount"],
        "totalBytes": inventory["totalBytes"],
        "sourceFingerprint": inventory["fingerprint"],
        "files": inventory["files"],
        "filesTruncated": inventory["filesTruncated"],
        "approvalToken": approval_token,
        "requiresApproval": True,
    }


def project_create(params):
    params = params or {}
    plan = project_create_plan(params)
    if params.get("approvalToken") != plan["approvalToken"] or params.get("confirm") is not True:
        raise ConnectorError("PROJECT_CREATE_APPROVAL_REQUIRED", "Creating a project requires its current plan token and explicit confirmation")
    if not os.path.isdir(NEW_PROJECT_ROOT):
        os.makedirs(NEW_PROJECT_ROOT, 0o750)
    if os.path.islink(NEW_PROJECT_ROOT) or not is_inside_root(NEW_PROJECT_ROOT):
        raise ConnectorError("PROJECT_ROOT_DENIED", "The managed project root must be a real directory inside the workspace")
    source = resolve_relative(plan["sourceRelativePath"])
    target = resolve_relative(plan["targetRelativePath"])
    lock_path = os.path.join(NEW_PROJECT_ROOT, "." + plan["name"] + ".create.lock")
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise ConnectorError("PROJECT_CREATE_BUSY", "This project name is already being created")
    os.close(lock_descriptor)
    staging_root = tempfile.mkdtemp(prefix=".aitcad-create-", dir=NEW_PROJECT_ROOT)
    staged_project = os.path.join(staging_root, plan["name"])
    created = False
    try:
        shutil.copytree(source, staged_project, symlinks=False)
        staged_inventory = project_template_inventory(staged_project)
        if staged_inventory["fingerprint"] != plan["sourceFingerprint"]:
            raise ConnectorError("PROJECT_TEMPLATE_CHANGED", "Template content changed during copying; generate a fresh plan")
        if os.path.lexists(target):
            raise ConnectorError("PROJECT_ALREADY_EXISTS", "The target appeared after approval; no files were replaced")
        os.rename(staged_project, target)
        created = True
    finally:
        if os.path.isdir(staging_root):
            shutil.rmtree(staging_root)
        if os.path.exists(lock_path):
            os.unlink(lock_path)
    created_at = utc_time(time.time())
    manifest = {
        "name": plan["name"],
        "templateId": plan["templateId"],
        "sourceRelativePath": plan["sourceRelativePath"],
        "targetRelativePath": plan["targetRelativePath"],
        "sourceFingerprint": plan["sourceFingerprint"],
        "approvalToken": plan["approvalToken"],
        "fileCount": plan["fileCount"],
        "totalBytes": plan["totalBytes"],
        "createdAt": created_at,
    }
    manifest_path = None
    audit_warning = None
    try:
        if not os.path.isdir(CREATION_ROOT):
            os.makedirs(CREATION_ROOT, 0o700)
        manifest_path = os.path.join(CREATION_ROOT, created_at.replace(":", "") + "-" + plan["approvalToken"][:8] + ".json")
        with open(manifest_path, "wb") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=True, indent=2).encode("ascii"))
    except (IOError, OSError) as error:
        audit_warning = "Project was created, but the app-owned audit manifest could not be written: " + str(error)
    return {
        "ok": True,
        "status": "created" if created else "not-created",
        "name": plan["name"],
        "relativePath": plan["targetRelativePath"],
        "path": target,
        "fileCount": plan["fileCount"],
        "totalBytes": plan["totalBytes"],
        "createdAt": created_at,
        "manifestPath": manifest_path,
        "auditWarning": audit_warning,
    }


def search_text_files(params):
    params = params or {}
    query = params.get("query") or ""
    if not isinstance(query, type(u"")):
        try:
            query = query.decode("utf-8")
        except AttributeError:
            query = type(u"")(query)
    query = query.strip()
    if len(query) < 2 or len(query) > 100:
        raise ConnectorError("INVALID_QUERY", "Search query must contain 2 to 100 characters")

    relative_path = params.get("relativePath") or "nmos_study/nmos_project"
    target = resolve_relative(relative_path)
    if not os.path.isdir(target):
        raise ConnectorError("DIRECTORY_NOT_FOUND", "The requested search directory does not exist")

    case_sensitive = bool(params.get("caseSensitive", False))
    needle = query if case_sensitive else query.lower()
    hits = []
    scanned_files = 0
    truncated = False

    for current, directories, files in os.walk(target):
        directories[:] = sorted(
            name for name in directories if not os.path.islink(os.path.join(current, name))
        )
        for name in sorted(files):
            path = os.path.join(current, name)
            extension = os.path.splitext(name)[1].lower().lstrip(".")
            if extension not in TEXT_EXTENSIONS or os.path.islink(path):
                continue
            if scanned_files >= MAX_SEARCH_FILES:
                truncated = True
                break
            scanned_files += 1
            try:
                with open(path, "rb") as stream:
                    data = stream.read(MAX_SEARCH_BYTES_PER_FILE + 1)
            except IOError:
                continue
            if b"\x00" in data:
                continue
            if len(data) > MAX_SEARCH_BYTES_PER_FILE:
                data = data[:MAX_SEARCH_BYTES_PER_FILE]
                truncated = True
            content, _encoding = decode_text(data)
            for line_number, line in enumerate(content.splitlines(), 1):
                haystack = line if case_sensitive else line.lower()
                if needle not in haystack:
                    continue
                preview = line.strip()
                if len(preview) > 240:
                    preview = preview[:237] + "..."
                hits.append(
                    {
                        "name": name,
                        "relativePath": os.path.relpath(path, WORKSPACE_ROOT).replace(os.sep, "/"),
                        "extension": extension,
                        "lineNumber": line_number,
                        "preview": preview,
                        "size": os.path.getsize(path),
                    }
                )
                if len(hits) >= MAX_SEARCH_HITS:
                    truncated = True
                    break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if truncated and (scanned_files >= MAX_SEARCH_FILES or len(hits) >= MAX_SEARCH_HITS):
            break

    return {
        "ok": True,
        "query": query,
        "relativePath": os.path.relpath(target, WORKSPACE_ROOT).replace(os.sep, "/"),
        "scannedFiles": scanned_files,
        "hits": hits,
        "truncated": truncated,
    }


def read_project_text(relative_path, max_bytes=2 * 1024 * 1024):
    target = resolve_relative(relative_path)
    if not os.path.isfile(target):
        return u""
    with open(target, "rb") as stream:
        data = stream.read(max_bytes)
    if b"\x00" in data:
        return u""
    return decode_text(data)[0]


def block_content(text, block_name):
    match = re.search(r"\b" + re.escape(block_name) + r"\s*\{", text, re.I)
    if not match:
        return u""
    depth = 1
    index = match.end()
    start = index
    while index < len(text) and depth:
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    return text[start:index - 1] if depth == 0 else text[start:]


def parse_workflow(gtree_text, node_statuses):
    known_tools = set(("sde", "sdevice", "svisual", "snmesh", "sprocess", "inspect"))
    steps = []
    row_index = 0
    current_step = None
    for raw_line in gtree_text.splitlines():
        line = raw_line.strip()
        if line == "# --- variables":
            break
        if not line or line.startswith("#"):
            continue
        match = re.match(r'^(\S+)\s+(\S+)\s+"([^"]*)"\s+\{([^}]*)\}', line)
        if not match:
            continue
        row_index += 1
        group, name, display_value, active_value = match.groups()
        if name.lower() in known_tools:
            current_step = {
                "name": group,
                "tool": name.lower(),
                "nodeId": row_index,
                "parameters": [],
                "status": "virtual" if name.lower() in ("svisual", "inspect") else "not-run",
                "durationSeconds": None,
            }
            steps.append(current_step)
        elif current_step and group == current_step["name"]:
            value = active_value.strip() or display_value.strip()
            current_step["parameters"].append({"name": name, "value": value})
            current_step["nodeId"] = row_index

    for step in steps:
        status = node_statuses.get(str(step["nodeId"]))
        if status:
            step["status"] = status["status"]
            step["durationSeconds"] = status["durationSeconds"]
            step["finishedAt"] = status["finishedAt"]
    return steps


def parse_node_statuses(project_relative_path):
    project_path = resolve_relative(project_relative_path)
    statuses = {}
    for name in os.listdir(project_path):
        match = re.match(r"^n(\d+)_.*\.sta$", name)
        if not match:
            continue
        text = read_project_text(os.path.join(project_relative_path, name).replace(os.sep, "/"), 8192)
        first_line = text.splitlines()[0] if text.splitlines() else ""
        parts = first_line.split("|")
        if len(parts) < 5:
            continue
        try:
            timestamp = int(parts[1])
            duration = int(parts[4].strip())
        except ValueError:
            continue
        statuses[match.group(1)] = {
            "status": parts[2].strip(),
            "user": parts[3].strip(),
            "durationSeconds": duration,
            "finishedAt": utc_time(timestamp),
            "statusFile": os.path.join(project_relative_path, name).replace(os.sep, "/"),
        }
    return statuses


def parse_sde(sde_text, workflow):
    def numeric_define(name):
        match = re.search(r"\(define\s+" + re.escape(name) + r"\s+([0-9.eE+-]+)\s*\)", sde_text)
        return match.group(1) if match else None

    rectangles = re.findall(
        r'^\s*\(sdegeo:create-rectangle.*?"([A-Za-z0-9_+.-]+)"\s+"([^"]+)"\s*\)',
        sde_text,
        re.M,
    )
    polygons = re.findall(
        r'\(sdegeo:create-polygon[\s\S]*?\)\s+"([A-Za-z0-9_+.-]+)"\s+"([^"]+)"\s*\)',
        sde_text,
    )
    regions = [{"material": material, "name": name} for material, name in rectangles + polygons]
    contacts = list(dict.fromkeys(re.findall(r'\(sdegeo:define-contact-set\s+"([^"]+)"', sde_text)))
    dopants = sorted(set(re.findall(r'"(BoronConcentration|PhosphorusConcentration)"', sde_text)))
    refinements = []
    for match in re.finditer(
        r'\(sdedr:define-refinement-size\s+"([^"]+)"\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)',
        sde_text,
    ):
        refinements.append({"name": match.group(1), "sizes": list(match.groups()[1:])})

    workflow_parameters = {}
    for step in workflow:
        for parameter in step["parameters"]:
            workflow_parameters[parameter["name"]] = parameter["value"]

    return {
        "deviceType": "2D NMOS",
        "dimensions": {"x": numeric_define("Xdim"), "y": numeric_define("Ydim"), "unit": "um"},
        "gateLength": workflow_parameters.get("nmos_l"),
        "materials": sorted(set(item["material"] for item in regions)),
        "regions": regions,
        "contacts": contacts,
        "dopants": dopants,
        "doping": {
            "nPlusPeak": numeric_define("con_plus"),
            "epitaxy": numeric_define("con_epi"),
            "pWell": workflow_parameters.get("con_pwell"),
        },
        "refinements": refinements,
    }


def parse_sdevice(command_text, log_text):
    electrode_block = block_content(command_text, "Electrode")
    physics_block = block_content(command_text, "Physics")
    math_block = block_content(command_text, "Math")
    solve_block = block_content(command_text, "Solve")
    plot_block = block_content(command_text, "Plot")

    electrodes = []
    for name, voltage in re.findall(r'Name\s*=\s*"([^"]+)"\s+Voltage\s*=\s*([^\s}]+)', electrode_block, re.I):
        electrodes.append({"name": name, "initialVoltage": voltage})

    model_candidates = (
        "Fermi", "PhuMob", "Enormal", "HighFieldSaturation", "SRH",
        "DopingDependence", "TempDependence", "Band2Band", "Schenk", "oldSlotboom",
    )
    physics_models = [name for name in model_candidates if re.search(r"\b" + re.escape(name) + r"\b", physics_block, re.I)]

    math_settings = {}
    for key in ("Notdamped", "iterations", "Method", "Digits", "NumberOfThreads"):
        match = re.search(r"\b" + key + r"\s*=\s*([^\s}]+)", math_block, re.I)
        if match:
            math_settings[key] = match.group(1)
    math_flags = [flag for flag in ("Extrapolate", "Derivatives", "ExtendedPrecision", "ExitOnFailure") if re.search(r"\b" + flag + r"\b", math_block, re.I)]

    sweeps = []
    for match in re.finditer(r"Quasistationary\s*\((.*?)\)\s*\{", solve_block, re.I | re.S):
        settings = match.group(1)
        goal = re.search(r'Goal\s*\{\s*Name\s*=\s*"([^"]+)"\s+Voltage\s*=\s*([^\s}]+)', settings, re.I)
        if goal:
            sweeps.append({"electrode": goal.group(1), "goalVoltage": goal.group(2), "doZero": bool(re.search(r"\bDoZero\b", settings, re.I))})

    plot_tokens = re.findall(r"[A-Za-z][A-Za-z0-9]*(?:/Vector)?", re.sub(r"#.*", "", plot_block))
    plot_fields = list(dict.fromkeys(plot_tokens))

    final_contacts = {}
    current_pattern = re.compile(
        r"^\s*(GND|drain|gate|source)\s+([+-]?[0-9.]+E[+-][0-9]+)\s+([+-]?[0-9.]+E[+-][0-9]+)\s+([+-]?[0-9.]+E[+-][0-9]+)\s+([+-]?[0-9.]+E[+-][0-9]+)",
        re.I | re.M,
    )
    for contact, voltage, electron, hole, conduction in current_pattern.findall(log_text):
        final_contacts[contact.lower()] = {
            "voltage": voltage,
            "electronCurrent": electron,
            "holeCurrent": hole,
            "conductionCurrent": conduction,
        }

    wallclock_match = re.search(r"wallclock:\s*([0-9.]+)\s*s", log_text, re.I)
    temperature_match = re.search(r"Temperature\s*=\s*([^\s}]+)", physics_block, re.I)
    area_factor_match = re.search(r"AreaFactor\s*=\s*([^\s}]+)", physics_block, re.I)
    iteration_counts = []
    iteration_blocks = re.findall(
        r"Iteration\s+\|Rhs\|.*?\r?\n-+\r?\n(.*?)(?=Finished, because|Stopped, because)",
        log_text,
        re.I | re.S,
    )
    for iteration_block in iteration_blocks:
        rows = re.findall(r"^\s*(\d+)\s+[0-9.eE+-]+", iteration_block, re.M)
        if rows:
            iteration_counts.append(int(rows[-1]))
    bias_steps = [float(value) for value in re.findall(r"Computing step from t=.*?\(Stepsize:\s*([0-9.eE+-]+)\)", log_text)]
    return {
        "electrodes": electrodes,
        "temperature": temperature_match.group(1) if temperature_match else None,
        "areaFactor": area_factor_match.group(1) if area_factor_match else None,
        "physicsModels": physics_models,
        "math": {"settings": math_settings, "flags": math_flags},
        "sweeps": sweeps,
        "plotFields": plot_fields,
        "status": "done" if "Sentaurus Device simulation finished" in log_text and "Good Bye" in log_text else "unknown",
        "wallclockSeconds": float(wallclock_match.group(1)) if wallclock_match else None,
        "finalContacts": final_contacts,
        "errorCount": len(re.findall(r"^\s*(?:ERROR|Error):", log_text, re.M)),
        "warningCount": len(re.findall(r"^\s*(?:WARNING|Warning):", log_text, re.M)),
        "convergence": {
            "biasSteps": len(bias_steps),
            "solverCalls": len(iteration_counts),
            "maximumNewtonIterations": max(iteration_counts) if iteration_counts else None,
            "averageNewtonIterations": round(float(sum(iteration_counts)) / len(iteration_counts), 2) if iteration_counts else None,
            "minimumStep": min(bias_steps) if bias_steps else None,
            "maximumStep": max(bias_steps) if bias_steps else None,
            "failedSteps": len(re.findall(r"(?:Not converged|step\s+failed|decreasing\s+step)", log_text, re.I)),
        },
    }


def parse_parameter_file(parameter_text):
    materials = []
    includes = []
    overrides = []
    current_material = None
    material_depth = None
    current_section = None
    section_depth = None
    depth = 0

    for line_number, raw_line in enumerate(parameter_text.splitlines(), 1):
        line = re.sub(r"#.*$", "", raw_line).strip()
        material_match = re.match(r'Material\s*=\s*"([^"]+)"\s*\{', line, re.I)
        if material_match:
            current_material = material_match.group(1)
            if current_material not in materials:
                materials.append(current_material)
            material_depth = depth + 1
            current_section = None
            section_depth = None

        include_match = re.search(r'#includeext\s+"([^"]+)"', raw_line, re.I)
        if include_match and current_material:
            includes.append({
                "material": current_material,
                "path": include_match.group(1),
                "lineNumber": line_number,
            })

        section_match = re.match(r"([A-Za-z][A-Za-z0-9_]*)\s*\{\s*$", line)
        if section_match and current_material and not material_match:
            current_section = section_match.group(1)
            section_depth = depth + 1

        assignment_match = re.match(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if assignment_match and current_material and current_section:
            overrides.append({
                "material": current_material,
                "section": current_section,
                "parameter": assignment_match.group(1),
                "value": assignment_match.group(2).strip(),
                "lineNumber": line_number,
            })

        depth += raw_line.count("{") - raw_line.count("}")
        if section_depth is not None and depth < section_depth:
            current_section = None
            section_depth = None
        if material_depth is not None and depth < material_depth:
            current_material = None
            material_depth = None
            current_section = None
            section_depth = None

    risks = []
    if overrides:
        risks.append("Local values override selected parameters from included material libraries.")
    if any(item.get("section") == "Band2BandTunneling" for item in overrides):
        risks.append("Band-to-band tunneling coefficients are locally overridden and should be calibrated for the target process.")
    if any(item.get("section") == "Scharfetter" for item in overrides):
        risks.append("Carrier lifetime parameters are locally overridden and directly affect recombination behavior.")
    return {
        "materials": materials,
        "includes": includes,
        "overrides": overrides,
        "risks": risks,
    }


def project_analysis(params):
    params = params or {}
    project_relative_path = params.get("relativePath") or "nmos_study/nmos_project"
    project_path = resolve_relative(project_relative_path)
    if not os.path.isdir(project_path):
        raise ConnectorError("DIRECTORY_NOT_FOUND", "The requested project directory does not exist")

    def project_file(name):
        return os.path.join(project_relative_path, name).replace(os.sep, "/")

    gtree = read_project_text(project_file("gtree.dat"))
    summary = read_project_text(project_file("gsummary.txt"))
    execution_log = read_project_text(project_file("glog.txt"))
    sde_command = read_project_text(project_file("nmos_sde_dvs.cmd"))
    sdevice_command = read_project_text(project_file("sdevice_des.cmd"))
    sdevice_log = read_project_text(project_file("n5_des.log"))
    parameter_file = read_project_text(project_file("sdevice.par"))

    node_statuses = parse_node_statuses(project_relative_path)
    workflow = parse_workflow(gtree, node_statuses)
    summary_status = re.search(r"current status\s*:\s*([^\r\n]+)", summary, re.I)
    runtime_match = re.search(r"<[^>]+>\s+done\s+\(([0-9]+)\s+sec\)", execution_log, re.I)
    host_match = re.search(r'Host\s+"([^"]+)"', summary)
    final_done = bool(re.search(r"gsub exits with status 0", execution_log, re.I))

    return {
        "ok": True,
        "mode": "deterministic-read-only",
        "analyzedAt": utc_time(os.path.getmtime(project_path)),
        "project": {
            "name": os.path.basename(project_path),
            "relativePath": project_relative_path,
            "status": "done" if final_done else (summary_status.group(1).strip() if summary_status else "unknown"),
            "summaryStatus": summary_status.group(1).strip() if summary_status else "unknown",
            "totalNodes": len(re.findall(r"^\d+\s+\d+\s+\d+\s+", gtree, re.M)),
            "runtimeSeconds": int(runtime_match.group(1)) if runtime_match else None,
            "host": host_match.group(1) if host_match else socket.gethostname(),
        },
        "workflow": workflow,
        "structure": parse_sde(sde_command, workflow),
        "deviceSimulation": parse_sdevice(sdevice_command, sdevice_log),
        "parameters": parse_parameter_file(parameter_file),
        "evidence": {
            "workflow": project_file("gtree.dat"),
            "summary": project_file("gsummary.txt"),
            "execution": project_file("glog.txt"),
            "structure": project_file("nmos_sde_dvs.cmd"),
            "device": project_file("sdevice_des.cmd"),
            "deviceLog": project_file("n5_des.log"),
            "parameters": project_file("sdevice.par"),
        },
    }


def resolve_result_file(params, required_extension, missing_code, missing_message):
    params = params or {}
    relative_path = params.get("relativePath") or ""
    run_id = params.get("runId") or ""
    if not run_id:
        target = resolve_relative(relative_path)
        return target, normalized_workspace_path(target), {"sourceKind": "workspace", "runId": None}

    if not isinstance(relative_path, type(u"")) or not relative_path or len(relative_path) > 1024:
        raise ConnectorError("RUN_RESULT_PATH_INVALID", "The run result path is invalid")
    if os.path.isabs(relative_path) or "\\" in relative_path or "\x00" in relative_path:
        raise ConnectorError("RUN_RESULT_PATH_INVALID", "Only normalized paths relative to the isolated project are allowed")
    normalized = os.path.normpath(relative_path).replace(os.sep, "/")
    if normalized in (".", "..") or normalized.startswith("../"):
        raise ConnectorError("RUN_RESULT_PATH_INVALID", "The run result path cannot leave the isolated project")

    manifest, _manifest_path = read_run_manifest(run_id)
    run_path = os.path.realpath(manifest.get("runPath") or "")
    project_root = os.path.realpath(manifest.get("projectPath") or "")
    expected_project_root = os.path.realpath(os.path.join(run_path, "project"))
    if project_root != expected_project_root or not os.path.isdir(project_root):
        raise ConnectorError("RUN_MANIFEST_INVALID", "The isolated project path in the run manifest is invalid")
    listed_results = dict((item["relativePath"], item) for item in run_result_files(manifest))
    if normalized not in listed_results:
        raise ConnectorError("RUN_RESULT_NOT_LISTED", "Only result files produced by this app-owned run may be opened")
    target = os.path.realpath(os.path.join(project_root, normalized))
    if not target.startswith(project_root + os.sep) or not os.path.isfile(target) or os.path.islink(target):
        raise ConnectorError(missing_code, missing_message)
    if os.path.splitext(target)[1].lower() != required_extension:
        raise ConnectorError("RUN_RESULT_TYPE_MISMATCH", "The requested run result has the wrong file type for this viewer")
    return target, normalized, {"sourceKind": "run", "runId": run_id}


def parse_plt_curve(params):
    params = params or {}
    target, relative_path, source = resolve_result_file(params, ".plt", "FILE_NOT_FOUND", "The requested PLT file does not exist")
    if not os.path.isfile(target):
        raise ConnectorError("FILE_NOT_FOUND", "The requested PLT file does not exist")
    if os.path.islink(target) or os.path.splitext(target)[1].lower() != ".plt":
        raise ConnectorError("PLT_FILE_REQUIRED", "The curve parser only accepts PLT files")
    size = os.path.getsize(target)
    if size > MAX_PLT_BYTES:
        raise ConnectorError("PLT_TOO_LARGE", "The PLT file exceeds the 4 MB parser limit")

    with open(target, "rb") as stream:
        data = stream.read(MAX_PLT_BYTES)
    if b"\x00" in data:
        raise ConnectorError("BINARY_PLT_UNSUPPORTED", "This PLT file is not DF-ISE text format")
    text, encoding = decode_text(data)
    if not text.lstrip().startswith("DF-ISE text"):
        raise ConnectorError("PLT_FORMAT_UNSUPPORTED", "Only DF-ISE text PLT files are supported")

    dataset_match = re.search(r"datasets\s*=\s*\[(.*?)\]", text, re.I | re.S)
    data_match = re.search(r"Data\s*\{(.*?)\}\s*$", text, re.I | re.S)
    if not dataset_match or not data_match:
        raise ConnectorError("PLT_PARSE_FAILED", "The PLT dataset or data block is missing")
    datasets = re.findall(r'"([^"]+)"', dataset_match.group(1))
    if not datasets:
        raise ConnectorError("PLT_PARSE_FAILED", "The PLT file contains no datasets")

    x_dataset = params.get("xDataset") or "gate OuterVoltage"
    y_dataset = params.get("yDataset") or "drain TotalCurrent"
    if x_dataset not in datasets or y_dataset not in datasets:
        raise ConnectorError("DATASET_NOT_FOUND", "The requested PLT dataset is not available")
    x_index = datasets.index(x_dataset)
    y_index = datasets.index(y_dataset)

    number_pattern = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
    values = [float(value) for value in re.findall(number_pattern, data_match.group(1))]
    dataset_count = len(datasets)
    point_count = min(len(values) // dataset_count, MAX_PLT_POINTS)
    points = []
    for point_index in range(point_count):
        offset = point_index * dataset_count
        x_value = values[offset + x_index]
        y_value = values[offset + y_index]
        if math.isnan(x_value) or math.isinf(x_value) or math.isnan(y_value) or math.isinf(y_value):
            continue
        points.append([x_value, y_value])
    if not points:
        raise ConnectorError("PLT_PARSE_FAILED", "The selected datasets contain no finite points")

    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    return {
        "ok": True,
        "format": "DF-ISE text",
        "name": os.path.basename(target),
        "relativePath": relative_path,
        "sourceKind": source["sourceKind"],
        "runId": source["runId"],
        "encoding": encoding,
        "size": size,
        "datasets": datasets,
        "xDataset": x_dataset,
        "yDataset": y_dataset,
        "pointCount": len(points),
        "points": points,
        "range": {
            "xMin": min(x_values),
            "xMax": max(x_values),
            "yMin": min(y_values),
            "yMax": max(y_values),
            "absYMin": min(abs(value) for value in y_values if value != 0) if any(value != 0 for value in y_values) else 0,
            "absYMax": max(abs(value) for value in y_values),
        },
        "truncated": point_count >= MAX_PLT_POINTS or len(values) % dataset_count != 0,
    }


def ensure_cache_root():
    if not os.path.isdir(CACHE_ROOT):
        try:
            os.makedirs(CACHE_ROOT, 0o700)
        except OSError:
            if not os.path.isdir(CACHE_ROOT):
                raise
    try:
        os.chmod(CACHE_ROOT, 0o700)
    except OSError:
        pass


def sentaurus_profile(version):
    requested = str(version or SENTAURUS_PROFILES[0]["version"])
    for profile in SENTAURUS_PROFILES:
        release_year = re.search(r"(?:19|20)\d{2}", profile["release"])
        aliases = set((profile["version"], profile["release"], "configured"))
        if release_year:
            aliases.add(release_year.group(0))
        if requested in aliases:
            executable = os.path.join(profile["root"], "bin", "svisual")
            if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
                raise ConnectorError("SVISUAL_NOT_INSTALLED", "SVisual %s is not available" % requested)
            return profile
    raise ConnectorError("SENTAURUS_VERSION_NOT_ALLOWED", "Only configured Sentaurus versions may be used")


def resolve_tdr(params):
    target, relative_path, source = resolve_result_file(params, ".tdr", "FILE_NOT_FOUND", "The requested TDR file does not exist")
    if not os.path.isfile(target):
        raise ConnectorError("FILE_NOT_FOUND", "The requested TDR file does not exist")
    if os.path.islink(target) or os.path.splitext(target)[1].lower() != ".tdr":
        raise ConnectorError("TDR_FILE_REQUIRED", "The SVisual viewer only accepts TDR files")
    size = os.path.getsize(target)
    if size > MAX_TDR_BYTES:
        raise ConnectorError("TDR_TOO_LARGE", "The TDR file exceeds the 2 GB viewer limit")
    return target, relative_path.replace(os.sep, "/"), size, source


def cache_digest(parts):
    value = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def tdr_fingerprint(target, version):
    stat = os.stat(target)
    return cache_digest((os.path.realpath(target), stat.st_size, stat.st_mtime, version))


def tcl_string(value):
    value = str(value)
    return '"%s"' % (value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("[", "\\[")
        .replace("]", "\\]"))


def svisual_script(target, output_path=None, requested_field="", scale="linear"):
    lines = [
        "set input_file %s" % tcl_string(target),
        "if {[catch {load_file $input_file -name AITCAD_TDR} load_result]} {",
        "  puts \"AITCAD_LOAD_ERROR=$load_result\"",
        "  exit 2",
        "}",
        "set datasets [list_datasets]",
        "if {[llength $datasets] == 0} { puts \"AITCAD_LOAD_ERROR=No datasets found\"; exit 3 }",
        "set dataset [lindex $datasets 0]",
        "set fields [list_fields -dataset $dataset]",
        "puts \"AITCAD_DATASETS=[join $datasets {|}]\"",
        "puts \"AITCAD_FIELDS=[join $fields {|}]\"",
    ]
    if output_path:
        lines.extend([
            "set requested_field %s" % tcl_string(requested_field),
            "if {$requested_field eq \"\"} {",
            "  foreach candidate {ElectrostaticPotential {Abs(ElectricField-V)} DopingConcentration eDensity} {",
            "    if {[lsearch -exact $fields $candidate] >= 0} { set requested_field $candidate; break }",
            "  }",
            "}",
            "if {[lsearch -exact $fields $requested_field] < 0} {",
            "  puts \"AITCAD_FIELD_ERROR=$requested_field\"",
            "  exit 4",
            "}",
            "set plot [create_plot -dataset $dataset]",
            "set_field_prop $requested_field -plot $plot -show_bands -scale %s" % scale,
            "if {[catch {export_view %s -format PNG -overwrite -plots [list $plot] -resolution 1100x720} export_result]} {" % tcl_string(output_path),
            "  puts \"AITCAD_EXPORT_ERROR=$export_result\"",
            "  exit 5",
            "}",
            "puts \"AITCAD_SELECTED_FIELD=$requested_field\"",
            "puts \"AITCAD_EXPORT=%s\"" % output_path.replace("\\", "/"),
        ])
    lines.extend(["puts \"AITCAD_COMPLETE=1\"", "exit 0", ""])
    return "\n".join(lines)


def validated_cut_coordinate(params, name):
    try:
        value = float(params.get(name))
    except (TypeError, ValueError):
        raise ConnectorError("TDR_CUT_COORDINATE_INVALID", "Cutline coordinates must be finite numbers")
    if math.isnan(value) or math.isinf(value) or abs(value) > MAX_TDR_CUT_COORDINATE:
        raise ConnectorError("TDR_CUT_COORDINATE_INVALID", "Cutline coordinates exceed the approved finite range")
    return value


def svisual_cutline_script(target, requested_field, coordinates):
    x1, y1, x2, y2 = coordinates
    coordinate_list = "[list %.16g %.16g %.16g %.16g]" % (x1, y1, x2, y2)
    lines = [
        "set input_file %s" % tcl_string(target),
        "if {[catch {load_file $input_file -name AITCAD_TDR} load_result]} {",
        "  puts \"AITCAD_LOAD_ERROR=$load_result\"",
        "  exit 2",
        "}",
        "set datasets [list_datasets]",
        "if {[llength $datasets] == 0} { puts \"AITCAD_LOAD_ERROR=No datasets found\"; exit 3 }",
        "set dataset [lindex $datasets 0]",
        "set fields [list_fields -dataset $dataset]",
        "puts \"AITCAD_DATASETS=[join $datasets {|}]\"",
        "puts \"AITCAD_FIELDS=[join $fields {|}]\"",
        "set requested_field %s" % tcl_string(requested_field),
        "if {[lsearch -exact $fields $requested_field] < 0} { puts \"AITCAD_FIELD_ERROR=$requested_field\"; exit 4 }",
        "set plot [create_plot -dataset $dataset]",
        "if {[catch {create_cutline -plot $plot -type free -points %s -name AITCAD_CUTLINE} cut_dataset]} {" % coordinate_list,
        "  puts \"AITCAD_CUTLINE_ERROR=$cut_dataset\"",
        "  exit 5",
        "}",
        "if {[catch {get_variable_data Distance -dataset $cut_dataset} distances]} { puts \"AITCAD_CUTLINE_ERROR=$distances\"; exit 6 }",
        "if {[catch {get_variable_data $requested_field -dataset $cut_dataset} values]} { puts \"AITCAD_CUTLINE_ERROR=$values\"; exit 7 }",
        "set available [llength $distances]",
        "if {[llength $values] < $available} { set available [llength $values] }",
        "set point_count $available",
        "set truncated 0",
        "if {$point_count > %d} { set point_count %d; set truncated 1 }" % (MAX_TDR_CUT_POINTS, MAX_TDR_CUT_POINTS),
        "set last_index [expr {$point_count - 1}]",
        "puts \"AITCAD_SELECTED_FIELD=$requested_field\"",
        "puts \"AITCAD_CUT_POINT_COUNT=$point_count\"",
        "puts \"AITCAD_CUT_TRUNCATED=$truncated\"",
        "puts \"AITCAD_CUT_DISTANCE=[join [lrange $distances 0 $last_index] {|}]\"",
        "puts \"AITCAD_CUT_VALUES=[join [lrange $values 0 $last_index] {|}]\"",
        "puts \"AITCAD_COMPLETE=1\"",
        "exit 0",
        "",
    ]
    return "\n".join(lines)


def run_svisual(profile, script, picture_export):
    ensure_cache_root()
    descriptor, script_path = tempfile.mkstemp(prefix="svisual-", suffix=".tcl", dir=CACHE_ROOT)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(script.encode("utf-8"))
        executable = os.path.join(profile["root"], "bin", "svisual")
        command = [
            "/usr/bin/timeout", str(SVISUAL_TIMEOUT_SECONDS), executable,
            "-nowait", "-bx" if picture_export else "-b", script_path,
        ]
        environment = os.environ.copy()
        environment["STROOT"] = profile["root"]
        environment["PATH"] = os.path.join(profile["root"], "bin") + os.pathsep + environment.get("PATH", "")
        license_path = "/usr/synopsys/scl/2018.06-SP1/admin/license/license.dat"
        environment["LM_LICENSE_FILE"] = license_path
        environment["SNPSLMD_LICENSE_FILE"] = license_path
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
        stdout, stderr = process.communicate()
        output = stdout.decode("utf-8", "replace")
        errors = stderr.decode("utf-8", "replace")
        if "AITCAD_COMPLETE=1" not in output:
            marker = re.search(r"AITCAD_(?:LOAD|FIELD|EXPORT|CUTLINE)_ERROR=([^\r\n]+)", output)
            detail = marker.group(1) if marker else (errors.strip().splitlines()[-1] if errors.strip() else "SVisual did not complete")
            if process.returncode == 124:
                raise ConnectorError("SVISUAL_TIMEOUT", "SVisual exceeded the %s second limit" % SVISUAL_TIMEOUT_SECONDS)
            raise ConnectorError("SVISUAL_FAILED", detail)
        return output
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def enrich_tdr_metadata(metadata, target, profile):
    groups = {
        "electrostatic": [],
        "carrier": [],
        "transport": [],
        "recombination": [],
        "tunneling": [],
        "material": [],
        "other": [],
    }
    for field in metadata.get("fields", []):
        lowered = field.lower()
        if any(token in lowered for token in ("potential", "electricfield", "spacecharge", "charge")):
            group = "electrostatic"
        elif any(token in lowered for token in ("edensity", "hdensity", "quasifermi", "temperature")):
            group = "carrier"
        elif any(token in lowered for token in ("current", "mobility", "velocity", "conductivity")):
            group = "transport"
        elif any(token in lowered for token in ("recomb", "srh", "auger", "radiative")):
            group = "recombination"
        elif any(token in lowered for token in ("tunnel", "band2band", "nonlocal")):
            group = "tunneling"
        elif any(token in lowered for token in ("doping", "donor", "acceptor", "molefraction", "material")):
            group = "material"
        else:
            group = "other"
        groups[group].append(field)
    name = os.path.basename(target)
    lowered_name = name.lower()
    if "_msh" in lowered_name or "mesh" in lowered_name:
        result_kind = "mesh"
    elif "_des" in lowered_name:
        result_kind = "device-state"
    elif "_bnd" in lowered_name or lowered_name.endswith(".bnd.tdr"):
        result_kind = "boundary"
    else:
        result_kind = "tdr-result"
    node_match = re.search(r"(?:^|[_-])n(\d+)(?:[_\-.]|$)", lowered_name)
    metadata.update({
        "datasetCount": len(metadata.get("datasets", [])),
        "fieldGroups": groups,
        "resultKind": result_kind,
        "nodeHint": int(node_match.group(1)) if node_match else None,
        "sourceFingerprint": tdr_fingerprint(target, profile["version"]),
    })
    return metadata


def parse_svisual_metadata(output, target, relative_path, size, profile, source=None):
    dataset_match = re.search(r"^AITCAD_DATASETS=(.*)$", output, re.M)
    fields_match = re.search(r"^AITCAD_FIELDS=(.*)$", output, re.M)
    datasets = dataset_match.group(1).strip().split("|") if dataset_match and dataset_match.group(1).strip() else []
    fields = fields_match.group(1).strip().split("|") if fields_match and fields_match.group(1).strip() else []
    if not datasets or not fields:
        raise ConnectorError("TDR_METADATA_FAILED", "SVisual returned no TDR datasets or fields")
    preferred = [field for field in (
        "ElectrostaticPotential", "Abs(ElectricField-V)", "DopingConcentration",
        "eDensity", "hDensity", "eCurrentDensity-V", "hCurrentDensity-V",
    ) if field in fields]
    metadata = enrich_tdr_metadata({
        "ok": True,
        "format": "Sentaurus TDR",
        "name": os.path.basename(target),
        "relativePath": relative_path,
        "size": size,
        "modifiedAt": utc_time(os.path.getmtime(target)),
        "sentaurusVersion": profile["version"],
        "sentaurusRelease": profile["release"],
        "datasets": datasets,
        "fields": fields,
        "fieldCount": len(fields),
        "preferredFields": preferred,
    }, target, profile)
    metadata.update(source or {"sourceKind": "workspace", "runId": None})
    return metadata


def load_json_cache(path):
    try:
        with open(path, "r") as stream:
            return json.load(stream)
    except (IOError, ValueError):
        return None


def write_json_cache(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
    if hasattr(os, "replace"):
        os.replace(temporary, path)
    else:
        os.rename(temporary, path)


def tdr_metadata(params):
    params = params or {}
    target, relative_path, size, source = resolve_tdr(params)
    profile = sentaurus_profile(params.get("version"))
    ensure_cache_root()
    fingerprint = tdr_fingerprint(target, profile["version"])
    metadata_path = os.path.join(CACHE_ROOT, "tdr-%s.json" % fingerprint)
    cached = load_json_cache(metadata_path)
    if cached:
        cached = enrich_tdr_metadata(cached, target, profile)
        cached.update(source)
        write_json_cache(metadata_path, cached)
        cached["cached"] = True
        cached["licenseUsed"] = False
        return cached
    output = run_svisual(profile, svisual_script(target), False)
    metadata = parse_svisual_metadata(output, target, relative_path, size, profile, source)
    write_json_cache(metadata_path, metadata)
    metadata["cached"] = False
    metadata["licenseUsed"] = True
    return metadata


def tdr_view(params):
    params = params or {}
    target, relative_path, size, source = resolve_tdr(params)
    profile = sentaurus_profile(params.get("version"))
    scale = params.get("scale") or "linear"
    if scale not in ("linear", "log", "asinh", "logabs"):
        raise ConnectorError("TDR_SCALE_NOT_ALLOWED", "The requested field scale is not allowed")
    requested_field = params.get("field") or ""
    if len(requested_field) > 160 or "\n" in requested_field or "\r" in requested_field:
        raise ConnectorError("TDR_FIELD_NOT_ALLOWED", "The requested field name is invalid")

    ensure_cache_root()
    fingerprint = tdr_fingerprint(target, profile["version"])
    metadata_path = os.path.join(CACHE_ROOT, "tdr-%s.json" % fingerprint)
    metadata = load_json_cache(metadata_path)
    if metadata:
        metadata = enrich_tdr_metadata(metadata, target, profile)
        metadata.update(source)
    if metadata and requested_field and requested_field not in metadata.get("fields", []):
        raise ConnectorError("TDR_FIELD_NOT_FOUND", "The requested field is not present in this TDR")
    if not requested_field and metadata:
        preferred = metadata.get("preferredFields") or metadata.get("fields") or []
        requested_field = preferred[0] if preferred else ""

    view_digest = cache_digest((fingerprint, requested_field or "default", scale, "1100x720"))
    image_path = os.path.join(CACHE_ROOT, "view-%s.png" % view_digest)
    manifest_path = os.path.join(CACHE_ROOT, "view-%s.json" % view_digest)
    manifest = load_json_cache(manifest_path)
    cached_image = os.path.isfile(image_path) and manifest and metadata
    if cached_image:
        selected_field = manifest.get("selectedField")
    else:
        output = run_svisual(profile, svisual_script(target, image_path, requested_field, scale), True)
        if metadata is None:
            metadata = parse_svisual_metadata(output, target, relative_path, size, profile, source)
            write_json_cache(metadata_path, metadata)
        selected_match = re.search(r"^AITCAD_SELECTED_FIELD=(.*)$", output, re.M)
        selected_field = selected_match.group(1).strip() if selected_match else requested_field
        if selected_field not in metadata.get("fields", []):
            raise ConnectorError("TDR_FIELD_NOT_FOUND", "SVisual did not confirm the selected field")
        if not os.path.isfile(image_path):
            raise ConnectorError("TDR_EXPORT_MISSING", "SVisual completed without creating the PNG view")
        manifest = {
            "selectedField": selected_field,
            "scale": scale,
            "generatedAt": utc_time(os.path.getmtime(image_path)),
        }
        write_json_cache(manifest_path, manifest)

    image_size = os.path.getsize(image_path)
    if image_size > MAX_TDR_IMAGE_BYTES:
        raise ConnectorError("TDR_IMAGE_TOO_LARGE", "The generated TDR image exceeds the 5 MB transfer limit")
    with open(image_path, "rb") as stream:
        image_base64 = base64.b64encode(stream.read()).decode("ascii")
    result = dict(metadata)
    result.update({
        "selectedField": selected_field,
        "scale": scale,
        "image": {
            "mimeType": "image/png",
            "size": image_size,
            "width": 1100,
            "height": 720,
            "base64": image_base64,
        },
        "cached": bool(cached_image),
        "licenseUsed": not bool(cached_image),
        "generatedAt": manifest.get("generatedAt"),
    })
    return result


def tdr_cutline(params):
    params = params or {}
    target, relative_path, size, source = resolve_tdr(params)
    profile = sentaurus_profile(params.get("version"))
    requested_field = params.get("field") or ""
    if not requested_field or len(requested_field) > 160 or "\n" in requested_field or "\r" in requested_field:
        raise ConnectorError("TDR_FIELD_NOT_ALLOWED", "A valid existing TDR field is required for cutline extraction")
    coordinates = tuple(validated_cut_coordinate(params, name) for name in ("x1", "y1", "x2", "y2"))
    if math.hypot(coordinates[2] - coordinates[0], coordinates[3] - coordinates[1]) <= 1e-15:
        raise ConnectorError("TDR_CUTLINE_DEGENERATE", "Cutline endpoints must be different")

    ensure_cache_root()
    fingerprint = tdr_fingerprint(target, profile["version"])
    metadata_path = os.path.join(CACHE_ROOT, "tdr-%s.json" % fingerprint)
    metadata = load_json_cache(metadata_path)
    if metadata:
        metadata = enrich_tdr_metadata(metadata, target, profile)
        metadata.update(source)
        if requested_field not in metadata.get("fields", []):
            raise ConnectorError("TDR_FIELD_NOT_FOUND", "The requested field is not present in this TDR")

    cut_digest = cache_digest((fingerprint, requested_field) + coordinates + ("cutline-v1",))
    cut_path = os.path.join(CACHE_ROOT, "cutline-%s.json" % cut_digest)
    cached = load_json_cache(cut_path)
    if cached:
        cached["cached"] = True
        cached["licenseUsed"] = False
        return cached

    output = run_svisual(profile, svisual_cutline_script(target, requested_field, coordinates), False)
    if metadata is None:
        metadata = parse_svisual_metadata(output, target, relative_path, size, profile, source)
        write_json_cache(metadata_path, metadata)
    selected_match = re.search(r"^AITCAD_SELECTED_FIELD=(.*)$", output, re.M)
    selected_field = selected_match.group(1).strip() if selected_match else requested_field
    if selected_field not in metadata.get("fields", []):
        raise ConnectorError("TDR_FIELD_NOT_FOUND", "SVisual did not confirm the cutline field")
    distance_match = re.search(r"^AITCAD_CUT_DISTANCE=(.*)$", output, re.M)
    values_match = re.search(r"^AITCAD_CUT_VALUES=(.*)$", output, re.M)
    if not distance_match or not values_match:
        raise ConnectorError("TDR_CUTLINE_FAILED", "SVisual returned no cutline samples")
    try:
        distances = [float(value) for value in distance_match.group(1).strip().split("|") if value.strip()]
        values = [float(value) for value in values_match.group(1).strip().split("|") if value.strip()]
    except ValueError:
        raise ConnectorError("TDR_CUTLINE_FAILED", "SVisual returned non-numeric cutline samples")
    point_count = min(len(distances), len(values), MAX_TDR_CUT_POINTS)
    points = []
    for index in range(point_count):
        if not (math.isnan(distances[index]) or math.isinf(distances[index]) or math.isnan(values[index]) or math.isinf(values[index])):
            points.append([distances[index], values[index]])
    if not points:
        raise ConnectorError("TDR_CUTLINE_EMPTY", "The selected cutline does not intersect finite field data")
    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    truncated_match = re.search(r"^AITCAD_CUT_TRUNCATED=(\d+)$", output, re.M)
    generated_at = utc_time(time.time())
    result = {
        "ok": True,
        "format": "SVisual cutline",
        "name": os.path.basename(target),
        "relativePath": relative_path,
        "sourceKind": source["sourceKind"],
        "runId": source["runId"],
        "field": selected_field,
        "xDataset": "Distance",
        "yDataset": selected_field,
        "distanceUnit": "device-coordinate",
        "endpoints": {"x1": coordinates[0], "y1": coordinates[1], "x2": coordinates[2], "y2": coordinates[3]},
        "geometricLength": math.hypot(coordinates[2] - coordinates[0], coordinates[3] - coordinates[1]),
        "pointCount": len(points),
        "points": points,
        "range": {
            "xMin": min(x_values),
            "xMax": max(x_values),
            "yMin": min(y_values),
            "yMax": max(y_values),
            "absYMin": min(abs(value) for value in y_values if value != 0) if any(value != 0 for value in y_values) else 0,
            "absYMax": max(abs(value) for value in y_values),
        },
        "truncated": bool(truncated_match and truncated_match.group(1) == "1") or len(distances) != len(values),
        "sentaurusVersion": profile["version"],
        "sentaurusRelease": profile["release"],
        "generatedAt": generated_at,
        "cached": False,
        "licenseUsed": True,
    }
    write_json_cache(cut_path, result)
    return result


def ensure_run_root():
    if not os.path.isdir(RUN_ROOT):
        try:
            os.makedirs(RUN_ROOT, 0o700)
        except OSError:
            if not os.path.isdir(RUN_ROOT):
                raise
    try:
        os.chmod(RUN_ROOT, 0o700)
    except OSError:
        pass


def sentaurus_environment(profile, stdb_root=None):
    environment = os.environ.copy()
    environment["STROOT"] = profile["root"]
    environment["PATH"] = os.path.join(profile["root"], "bin") + os.pathsep + environment.get("PATH", "")
    license_path = "/usr/synopsys/scl/2018.06-SP1/admin/license/license.dat"
    environment["LM_LICENSE_FILE"] = license_path
    environment["SNPSLMD_LICENSE_FILE"] = license_path
    if stdb_root:
        environment["STDB"] = stdb_root
    return environment


def project_copy_stats(project_path):
    file_count = 0
    byte_count = 0
    latest_mtime = os.path.getmtime(project_path)
    for current, directories, files in os.walk(project_path):
        for name in list(directories) + list(files):
            candidate = os.path.join(current, name)
            if os.path.islink(candidate):
                resolved_link = os.path.realpath(candidate)
                project_root = os.path.realpath(project_path)
                if resolved_link != project_root and not resolved_link.startswith(project_root + os.sep):
                    raise ConnectorError(
                        "PROJECT_SYMLINK_DENIED",
                        "Simulation copy link leaves the source project: %s -> %s" % (
                            os.path.relpath(candidate, project_path).replace(os.sep, "/"),
                            resolved_link,
                        ),
                    )
        for name in files:
            candidate = os.path.join(current, name)
            stat = os.stat(candidate)
            file_count += 1
            byte_count += stat.st_size
            latest_mtime = max(latest_mtime, stat.st_mtime)
    return file_count, byte_count, latest_mtime


def normalized_simulation_nodes(params, analysis):
    allowed = []
    node_tools = {}
    for step in analysis.get("workflow", []):
        tool = (step.get("tool") or "").lower()
        if tool in ("sde", "snmesh", "sprocess", "sdevice") and step.get("status") != "virtual":
            node_id = int(step["nodeId"])
            allowed.append(node_id)
            node_tools[node_id] = tool
    requested = params.get("nodes") or allowed
    if not isinstance(requested, list) or not requested:
        raise ConnectorError("SIMULATION_NODES_REQUIRED", "At least one approved compute node is required")
    try:
        nodes = sorted(set(int(node) for node in requested))
    except (TypeError, ValueError):
        raise ConnectorError("SIMULATION_NODE_INVALID", "Simulation node identifiers must be integers")
    if any(node not in allowed for node in nodes):
        raise ConnectorError("SIMULATION_NODE_NOT_ALLOWED", "Only compute nodes discovered in the project may be submitted")
    return nodes, node_tools


def normalized_parameter_overrides(params, analysis):
    raw_overrides = params.get("parameterOverrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ConnectorError("PARAMETER_OVERRIDES_INVALID", "Simulation parameter overrides must be an object")
    if len(raw_overrides) > 8:
        raise ConnectorError("PARAMETER_OVERRIDES_INVALID", "At most eight workflow parameters may be overridden")

    discovered = {}
    for step in analysis.get("workflow", []):
        for parameter in step.get("parameters", []):
            name = parameter.get("name")
            if name:
                discovered[name] = parameter.get("value")

    safety_bounds = {
        "con_pwell": (1e15, 1e20, "cm^-3"),
    }
    overrides = {}
    changes = []
    for name in sorted(raw_overrides):
        if name not in discovered:
            raise ConnectorError("PARAMETER_NOT_FOUND", "The requested workflow parameter was not discovered: %s" % name)
        if name not in safety_bounds:
            raise ConnectorError("PARAMETER_NOT_ALLOWED", "This parameter is not enabled for bounded automation: %s" % name)
        try:
            numeric_value = float(raw_overrides[name])
        except (TypeError, ValueError):
            raise ConnectorError("PARAMETER_VALUE_INVALID", "The workflow parameter must be numeric: %s" % name)
        if math.isnan(numeric_value) or math.isinf(numeric_value):
            raise ConnectorError("PARAMETER_VALUE_INVALID", "The workflow parameter must be finite: %s" % name)
        minimum, maximum, unit = safety_bounds[name]
        if numeric_value < minimum or numeric_value > maximum:
            raise ConnectorError(
                "PARAMETER_VALUE_OUT_OF_RANGE",
                "%s must stay within %.6g to %.6g %s" % (name, minimum, maximum, unit),
            )
        canonical_value = "%.12g" % numeric_value
        overrides[name] = canonical_value
        changes.append({
            "name": name,
            "from": str(discovered[name]),
            "to": canonical_value,
            "unit": unit,
        })
    return overrides, changes


def apply_workbench_parameter_overrides(project_path, overrides):
    if not overrides:
        return []
    gtree_path = os.path.join(project_path, "gtree.dat")
    if not os.path.isfile(gtree_path) or os.path.islink(gtree_path):
        raise ConnectorError("WORKFLOW_FILE_REQUIRED", "The isolated project does not contain a regular gtree.dat file")
    with open(gtree_path, "rb") as stream:
        original_data = stream.read(MAX_TEXT_BYTES)
    original_text, _encoding = decode_text(original_data)
    lines = original_text.splitlines(True)
    parameter_indexes = {}
    found_parameters = set()
    flow_index = 0
    in_flow = False
    updated_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped == "# --- simulation flow":
            in_flow = True
            updated_lines.append(line)
            continue
        if stripped == "# --- variables":
            in_flow = False
        if in_flow and stripped and not stripped.startswith("#"):
            match = re.match(r'^(\s*)(\S+)\s+(\S+)\s+"([^"]*)"\s+\{([^}]*)\}(.*?)(\r?\n)?$', line)
            if match:
                group, name = match.group(2), match.group(3)
                if name in overrides:
                    value = overrides[name]
                    suffix = match.group(6) or ""
                    newline = match.group(7) or ""
                    line = '%s%s %s "%s" {%s}%s%s' % (
                        match.group(1), group, name, value, value, suffix, newline,
                    )
                    parameter_indexes[name] = flow_index
                    found_parameters.add(name)
                flow_index += 1
        updated_lines.append(line)

    missing = sorted(set(overrides) - found_parameters)
    if missing:
        raise ConnectorError("PARAMETER_NOT_FOUND", "The isolated workflow is missing parameter(s): %s" % ", ".join(missing))

    tree_replacements = dict((index, name) for name, index in parameter_indexes.items())
    tree_counts = dict((name, 0) for name in overrides)
    in_tree = False
    final_lines = []
    for line in updated_lines:
        stripped = line.strip()
        if stripped in ("# --- tree", "# --- simulation tree"):
            in_tree = True
            final_lines.append(line)
            continue
        if in_tree:
            match = re.match(r'^(\s*\d+\s+\d+\s+)(\d+)(\s+)\{[^}]*\}(.*?)(\r?\n)?$', line)
            if match:
                parameter_index = int(match.group(2))
                name = tree_replacements.get(parameter_index)
                if name:
                    suffix = match.group(4) or ""
                    newline = match.group(5) or ""
                    line = "%s%d%s{%s}%s%s" % (
                        match.group(1), parameter_index, match.group(3), overrides[name], suffix, newline,
                    )
                    tree_counts[name] += 1
        final_lines.append(line)

    missing_tree = sorted(name for name, count in tree_counts.items() if count == 0)
    if missing_tree:
        raise ConnectorError("PARAMETER_TREE_NOT_FOUND", "The isolated workflow tree is missing parameter value(s): %s" % ", ".join(missing_tree))

    encoded = "".join(final_lines).encode("utf-8")
    original_mode = stat_module.S_IMODE(os.stat(gtree_path).st_mode)
    temporary = tempfile.NamedTemporaryFile(prefix=".aitcad-gtree-", dir=project_path, delete=False)
    temporary_path = temporary.name
    try:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        os.chmod(temporary_path, original_mode)
        if hasattr(os, "replace"):
            os.replace(temporary_path, gtree_path)
        else:
            os.rename(temporary_path, gtree_path)
    finally:
        try:
            temporary.close()
        except Exception:
            pass
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return [{"name": name, "value": overrides[name], "treeRowsUpdated": tree_counts[name]} for name in sorted(overrides)]


def simulation_plan(params):
    params = params or {}
    relative_path = params.get("relativePath") or "nmos_study/nmos_project"
    project_path = resolve_relative(relative_path)
    if not os.path.isdir(project_path) or not os.path.isfile(os.path.join(project_path, ".project")):
        raise ConnectorError("SWB_PROJECT_REQUIRED", "Simulation planning requires a Sentaurus Workbench project")
    profile = sentaurus_profile(params.get("version"))
    analysis = project_analysis({"relativePath": relative_path})
    nodes, node_tools = normalized_simulation_nodes(params, analysis)
    parameter_overrides, parameter_changes = normalized_parameter_overrides(params, analysis)
    file_count, byte_count, latest_mtime = project_copy_stats(project_path)
    snapshot = cache_digest((os.path.realpath(project_path), file_count, byte_count, latest_mtime))
    node_text = " ".join(str(node) for node in nodes)
    override_text = json.dumps(parameter_overrides, sort_keys=True, separators=(",", ":"))
    approval_token = cache_digest((snapshot, profile["version"], node_text, override_text, "isolated-copy-v2"))
    tools = []
    for node in nodes:
        label = node_tools[node].upper()
        if label not in tools:
            tools.append(label)
    estimated = analysis.get("project", {}).get("runtimeSeconds")
    return {
        "ok": True,
        "mode": "approval-required",
        "approvalToken": approval_token,
        "sourceSnapshot": snapshot,
        "source": {
            "name": os.path.basename(project_path),
            "relativePath": relative_path.replace(os.sep, "/"),
            "absolutePath": project_path,
            "fileCount": file_count,
            "size": byte_count,
            "modifiedAt": utc_time(latest_mtime),
        },
        "sentaurus": {
            "version": profile["version"],
            "release": profile["release"],
            "gcleanup": os.path.join(profile["root"], "bin", "gcleanup"),
            "gsub": os.path.join(profile["root"], "bin", "gsub"),
        },
        "nodes": [{"nodeId": node, "tool": node_tools[node].upper()} for node in nodes],
        "parameterOverrides": parameter_overrides,
        "parameterChanges": parameter_changes,
        "commands": [
            "gcleanup -default -n \"%s\" <isolated-project>" % node_text,
            "gsub -nodes \"%s\" <isolated-project>" % node_text,
        ],
        "workingCopyRoot": os.path.join(RUN_ROOT, "<run-id>", "project"),
        "estimatedRuntimeSeconds": estimated,
        "licenseTools": tools,
        "risks": [
            "The run can consume local TCAD license features and CPU/RAM.",
            "Only the isolated copy is cleaned and written; the source project remains read-only.",
            "Starting and stopping are recorded in an app-owned manifest and log.",
        ],
        "requiresApproval": True,
    }


def run_manifest_path(run_id):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{5,80}$", run_id or ""):
        raise ConnectorError("RUN_ID_INVALID", "The run identifier is invalid")
    path = os.path.realpath(os.path.join(RUN_ROOT, run_id))
    root = os.path.realpath(RUN_ROOT)
    if not path.startswith(root + os.sep):
        raise ConnectorError("RUN_PATH_DENIED", "The run path is outside the app-owned run directory")
    return path, os.path.join(path, "manifest.json")


def read_run_manifest(run_id):
    run_path, manifest_path = run_manifest_path(run_id)
    manifest = load_json_cache(manifest_path)
    if not manifest:
        raise ConnectorError("RUN_NOT_FOUND", "The requested simulation run does not exist")
    if os.path.realpath(manifest.get("runPath") or "") != run_path:
        raise ConnectorError("RUN_MANIFEST_INVALID", "The run manifest path is invalid")
    return manifest, manifest_path


def write_run_manifest(manifest, manifest_path):
    manifest["updatedAt"] = utc_time(time.time())
    write_json_cache(manifest_path, manifest)


def reusable_prepared_run(approval_token):
    ensure_run_root()
    for run_id in sorted(os.listdir(RUN_ROOT), reverse=True):
        try:
            manifest, _manifest_path = read_run_manifest(run_id)
        except ConnectorError:
            continue
        if manifest.get("approvalToken") == approval_token and manifest.get("status") == "prepared":
            return manifest
    return None


def public_run(manifest):
    return {
        "ok": True,
        "runId": manifest.get("runId"),
        "status": manifest.get("status"),
        "sourceRelativePath": manifest.get("sourceRelativePath"),
        "runPath": manifest.get("runPath"),
        "projectPath": manifest.get("projectPath"),
        "version": manifest.get("version"),
        "release": manifest.get("release"),
        "nodes": manifest.get("nodes") or [],
        "parameterOverrides": manifest.get("parameterOverrides") or {},
        "parameterChanges": manifest.get("parameterChanges") or [],
        "createdAt": manifest.get("createdAt"),
        "startedAt": manifest.get("startedAt"),
        "finishedAt": manifest.get("finishedAt"),
        "updatedAt": manifest.get("updatedAt"),
        "pid": manifest.get("pid"),
        "estimatedRuntimeSeconds": manifest.get("estimatedRuntimeSeconds"),
        "cleanup": manifest.get("cleanup"),
        "approvalToken": manifest.get("approvalToken"),
        "logPath": manifest.get("logPath"),
        "message": manifest.get("message"),
    }


def simulation_prepare(params):
    params = params or {}
    plan = simulation_plan(params)
    supplied_token = params.get("approvalToken") or ""
    if not supplied_token or supplied_token != plan["approvalToken"]:
        raise ConnectorError("APPROVAL_TOKEN_INVALID", "The displayed simulation plan must be approved before preparing a run")
    existing = reusable_prepared_run(supplied_token)
    if existing:
        result = public_run(existing)
        result["reused"] = True
        return result

    ensure_run_root()
    run_id = "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), supplied_token[:8])
    run_path, manifest_path = run_manifest_path(run_id)
    suffix = 1
    while os.path.exists(run_path):
        run_id = "%s-%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), supplied_token[:8], suffix)
        run_path, manifest_path = run_manifest_path(run_id)
        suffix += 1
    os.makedirs(run_path, 0o700)
    project_path = os.path.join(run_path, "project")
    source_path = resolve_relative(plan["source"]["relativePath"])
    created_at = utc_time(time.time())
    manifest = {
        "runId": run_id,
        "status": "preparing",
        "sourceRelativePath": plan["source"]["relativePath"],
        "sourceSnapshot": plan["sourceSnapshot"],
        "runPath": run_path,
        "projectPath": project_path,
        "version": plan["sentaurus"]["version"],
        "release": plan["sentaurus"]["release"],
        "nodes": plan["nodes"],
        "parameterOverrides": plan.get("parameterOverrides") or {},
        "parameterChanges": plan.get("parameterChanges") or [],
        "createdAt": created_at,
        "estimatedRuntimeSeconds": plan["estimatedRuntimeSeconds"],
        "approvalToken": supplied_token,
        "logPath": os.path.join(run_path, "run.log"),
    }
    write_run_manifest(manifest, manifest_path)
    try:
        shutil.copytree(source_path, project_path, symlinks=True)
        manifest["parameterApplication"] = apply_workbench_parameter_overrides(
            project_path, manifest.get("parameterOverrides") or {},
        )
        write_run_manifest(manifest, manifest_path)
        profile = sentaurus_profile(plan["sentaurus"]["version"])
        node_text = " ".join(str(item["nodeId"]) for item in plan["nodes"])
        cleanup_command = [
            "/usr/bin/timeout", str(GCLEANUP_TIMEOUT_SECONDS),
            os.path.join(profile["root"], "bin", "gcleanup"),
            "-default", "-n", node_text, project_path,
        ]
        cleanup = subprocess.Popen(
            cleanup_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=sentaurus_environment(profile, run_path),
            close_fds=True,
        )
        cleanup_output, _unused = cleanup.communicate()
        cleanup_text = cleanup_output.decode("utf-8", "replace")
        with open(os.path.join(run_path, "cleanup.log"), "wb") as stream:
            stream.write(cleanup_output)
        manifest["cleanup"] = {
            "exitCode": cleanup.returncode,
            "command": "gcleanup -default -n \"%s\" <isolated-project>" % node_text,
            "logTail": "\n".join(cleanup_text.splitlines()[-30:]),
        }
        if cleanup.returncode != 0:
            manifest["status"] = "prepare-failed"
            manifest["message"] = "gcleanup failed in the isolated project"
            write_run_manifest(manifest, manifest_path)
            raise ConnectorError("RUN_PREPARE_FAILED", manifest["message"])
        manifest["status"] = "prepared"
        manifest["message"] = "Isolated copy prepared; a second approval is required to start gsub"
        write_run_manifest(manifest, manifest_path)
    except Exception:
        if manifest.get("status") == "preparing":
            manifest["status"] = "prepare-failed"
            manifest["message"] = "The isolated copy could not be prepared"
            write_run_manifest(manifest, manifest_path)
        raise
    result = public_run(manifest)
    result["reused"] = False
    return result


def run_process_is_alive(manifest):
    pid = manifest.get("pid")
    project_path = manifest.get("projectPath") or ""
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as stream:
            command_line = stream.read().decode("utf-8", "replace").replace("\x00", " ")
        return project_path in command_line and ("gsub" in command_line or "gsub0" in command_line)
    except IOError:
        return False


def simulation_start(params):
    params = params or {}
    manifest, manifest_path = read_run_manifest(params.get("runId") or "")
    if params.get("approvalToken") != manifest.get("approvalToken"):
        raise ConnectorError("APPROVAL_TOKEN_INVALID", "Starting this run requires the approval token from its reviewed plan")
    if manifest.get("status") == "running" and run_process_is_alive(manifest):
        return public_run(manifest)
    if manifest.get("status") != "prepared":
        raise ConnectorError("RUN_NOT_PREPARED", "Only a prepared isolated run may be started")
    profile = sentaurus_profile(manifest.get("version"))
    node_text = " ".join(str(item["nodeId"]) for item in manifest.get("nodes") or [])
    command = [os.path.join(profile["root"], "bin", "gsub"), "-nodes", node_text, manifest["projectPath"]]
    log_stream = open(manifest["logPath"], "ab", 0)
    try:
        process = subprocess.Popen(
            command,
            stdin=open(os.devnull, "rb"),
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            env=sentaurus_environment(profile, manifest["runPath"]),
            cwd=manifest["projectPath"],
            close_fds=True,
            preexec_fn=os.setsid,
        )
    finally:
        log_stream.close()
    manifest["pid"] = process.pid
    manifest["status"] = "running"
    manifest["startedAt"] = utc_time(time.time())
    manifest["startedTimestamp"] = time.time()
    manifest["message"] = "gsub is running in the isolated project"
    write_run_manifest(manifest, manifest_path)
    return public_run(manifest)


def run_log_tail(manifest, line_limit=120):
    path = manifest.get("logPath") or ""
    try:
        with open(path, "rb") as stream:
            data = stream.read()[-256 * 1024:]
        return "\n".join(data.decode("utf-8", "replace").splitlines()[-line_limit:])
    except IOError:
        return ""


def run_node_log(manifest, line_limit=120):
    project_path = manifest.get("projectPath") or ""
    candidates = []
    if not os.path.isdir(project_path):
        return None, ""
    for name in os.listdir(project_path):
        match = re.match(r"^n(\d+)_.*\.log$", name)
        if match and os.path.isfile(os.path.join(project_path, name)):
            candidates.append((int(match.group(1)), name))
    if not candidates:
        return None, ""
    _node_id, name = sorted(candidates)[-1]
    path = os.path.join(project_path, name)
    try:
        with open(path, "rb") as stream:
            data = stream.read()[-512 * 1024:]
        return name, "\n".join(data.decode("utf-8", "replace").splitlines()[-line_limit:])
    except IOError:
        return name, ""


def run_final_summary(manifest):
    if manifest.get("status") != "completed":
        return None
    project_path = manifest.get("projectPath") or ""
    command_path = os.path.join(project_path, "sdevice_des.cmd")
    log_candidates = []
    if os.path.isdir(project_path):
        for name in os.listdir(project_path):
            match = re.match(r"^n(\d+)_.*\.log$", name)
            if match and os.path.isfile(os.path.join(project_path, name)):
                log_candidates.append((int(match.group(1)), os.path.join(project_path, name)))
    if not os.path.isfile(command_path) or not log_candidates:
        return None
    log_path = sorted(log_candidates)[-1][1]
    try:
        with open(command_path, "rb") as stream:
            command_text = decode_text(stream.read(2 * 1024 * 1024))[0]
        with open(log_path, "rb") as stream:
            log_text = decode_text(stream.read(8 * 1024 * 1024))[0]
    except IOError:
        return None
    parsed = parse_sdevice(command_text, log_text)
    return {
        "status": parsed.get("status"),
        "wallclockSeconds": parsed.get("wallclockSeconds"),
        "finalContacts": parsed.get("finalContacts"),
        "errorCount": parsed.get("errorCount"),
        "warningCount": parsed.get("warningCount"),
    }


def run_diagnostic(manifest):
    if manifest.get("status") not in ("failed", "prepare-failed", "stopped"):
        return None
    project_path = manifest.get("projectPath") or ""
    if not os.path.isdir(project_path):
        return None
    candidates = []
    for name in os.listdir(project_path):
        match = re.match(r"^n(\d+)_.*\.job$", name)
        if match and os.path.isfile(os.path.join(project_path, name)):
            candidates.append((int(match.group(1)), name))
    for node_id, name in sorted(candidates):
        path = os.path.join(project_path, name)
        try:
            with open(path, "rb") as stream:
                text = decode_text(stream.read(512 * 1024))[0]
        except IOError:
            continue
        if "Job failed" not in text and "exits with status 1" not in text:
            continue
        error_match = re.search(r"Error:\s*(.*?)\s*gjob exits", text, re.I | re.S)
        message = re.sub(r"\s+", " ", error_match.group(1)).strip() if error_match else "Workbench node failed"
        return {
            "nodeId": node_id,
            "sourceFile": name,
            "message": message,
            "gsubLog": os.path.basename(manifest.get("logPath") or "run.log"),
        }
    return None


def run_result_files(manifest):
    project_path = manifest.get("projectPath") or ""
    started_timestamp = manifest.get("startedTimestamp") or 0
    items = []
    if not os.path.isdir(project_path):
        return items
    for current, directories, files in os.walk(project_path):
        directories[:] = [name for name in directories if not os.path.islink(os.path.join(current, name))]
        for name in files:
            candidate = os.path.join(current, name)
            extension = os.path.splitext(name)[1].lower()
            if extension not in RUN_RESULT_EXTENSIONS or os.path.islink(candidate):
                continue
            try:
                stat = os.stat(candidate)
            except OSError:
                continue
            if not stat_module.S_ISREG(stat.st_mode):
                continue
            if stat.st_mtime + 1 < started_timestamp:
                continue
            items.append({
                "name": name,
                "relativePath": os.path.relpath(candidate, project_path).replace(os.sep, "/"),
                "extension": extension.lstrip("."),
                "size": stat.st_size,
                "modifiedAt": utc_time(stat.st_mtime),
            })
    items.sort(key=lambda item: item["modifiedAt"], reverse=True)
    return items[:80]


def simulation_status(params):
    params = params or {}
    manifest, manifest_path = read_run_manifest(params.get("runId") or "")
    log_tail = run_log_tail(manifest)
    if manifest.get("status") in ("running", "stopping") and not run_process_is_alive(manifest):
        if re.search(r"gsub exits with status 0", log_tail, re.I):
            manifest["status"] = "completed"
            manifest["message"] = "gsub completed successfully"
        elif manifest.get("status") == "stopping":
            manifest["status"] = "stopped"
            manifest["message"] = "The run was stopped by user approval"
        else:
            manifest["status"] = "failed"
            manifest["message"] = "gsub exited without a success marker"
        manifest["finishedAt"] = utc_time(time.time())
        manifest["pid"] = None
        write_run_manifest(manifest, manifest_path)
    result = public_run(manifest)
    node_log_name, node_log_tail = run_node_log(manifest)
    result["logTail"] = log_tail
    result["nodeLogName"] = node_log_name
    result["nodeLogTail"] = node_log_tail
    result["resultFiles"] = run_result_files(manifest)
    result["alive"] = run_process_is_alive(manifest)
    result["finalSummary"] = run_final_summary(manifest)
    result["diagnostic"] = run_diagnostic(manifest)
    return result


def simulation_stop(params):
    params = params or {}
    manifest, manifest_path = read_run_manifest(params.get("runId") or "")
    if params.get("approvalToken") != manifest.get("approvalToken") or params.get("confirm") is not True:
        raise ConnectorError("STOP_APPROVAL_REQUIRED", "Stopping a run requires explicit confirmation and its approval token")
    if manifest.get("status") != "running" or not run_process_is_alive(manifest):
        raise ConnectorError("RUN_NOT_ACTIVE", "The requested run is not active")
    try:
        os.killpg(manifest["pid"], signal.SIGTERM)
    except OSError as error:
        raise ConnectorError("RUN_STOP_FAILED", str(error))
    manifest["status"] = "stopping"
    manifest["message"] = "SIGTERM sent to the app-owned gsub process group"
    write_run_manifest(manifest, manifest_path)
    return public_run(manifest)


def simulation_history(_params=None):
    ensure_run_root()
    runs = []
    for run_id in sorted(os.listdir(RUN_ROOT), reverse=True):
        try:
            manifest, _manifest_path = read_run_manifest(run_id)
            if manifest.get("status") in ("running", "stopping"):
                item = simulation_status({"runId": run_id})
            else:
                item = public_run(manifest)
                item["alive"] = False
                item["resultFileCount"] = len(run_result_files(manifest))
                summary = run_final_summary(manifest)
                if summary:
                    item["finalSummary"] = summary
            runs.append(item)
        except (ConnectorError, OSError, ValueError):
            continue
        if len(runs) >= 30:
            break
    return {"ok": True, "runs": runs, "count": len(runs), "runRoot": RUN_ROOT}


def archive_root_path():
    root = os.path.join(RUN_ROOT, ".archive")
    if not os.path.isdir(root):
        try:
            os.makedirs(root, 0o700)
        except OSError:
            if not os.path.isdir(root):
                raise
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return os.path.realpath(root)


def read_archived_manifest(archive_id):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{10,140}$", archive_id or ""):
        raise ConnectorError("ARCHIVE_ID_INVALID", "The archive identifier is invalid")
    root = archive_root_path()
    archive_path = os.path.realpath(os.path.join(root, archive_id))
    if not archive_path.startswith(root + os.sep) or os.path.dirname(archive_path) != root:
        raise ConnectorError("ARCHIVE_PATH_DENIED", "The archive path is outside the app-owned archive directory")
    manifest_path = os.path.join(archive_path, "manifest.json")
    manifest = load_json_cache(manifest_path)
    if not manifest:
        raise ConnectorError("ARCHIVE_NOT_FOUND", "The requested archived run does not exist")
    run_id = manifest.get("runId") or ""
    destination, _destination_manifest = run_manifest_path(run_id)
    archived_from = os.path.realpath(manifest.get("archivedFrom") or "")
    if not archive_id.startswith(run_id + "-") or archived_from != destination:
        raise ConnectorError("ARCHIVE_MANIFEST_INVALID", "The archived run manifest does not match its app-owned destination")
    if os.path.realpath(manifest.get("runPath") or "") != destination:
        raise ConnectorError("ARCHIVE_MANIFEST_INVALID", "The archived run manifest contains an invalid original run path")
    if os.path.realpath(manifest.get("projectPath") or "") != os.path.realpath(os.path.join(destination, "project")):
        raise ConnectorError("ARCHIVE_MANIFEST_INVALID", "The archived run manifest contains an invalid original project path")
    return manifest, archive_path, manifest_path, destination


def archive_inventory(archive_path):
    fingerprint = hashlib.sha256()
    file_count = 0
    directory_count = 0
    total_bytes = 0
    for current, directories, files in os.walk(archive_path):
        directories.sort()
        files.sort()
        relative_current = os.path.relpath(current, archive_path).replace(os.sep, "/")
        safe_directories = []
        for name in directories:
            path = os.path.join(current, name)
            relative = (name if relative_current == "." else relative_current + "/" + name)
            if os.path.islink(path):
                link_target = os.readlink(path)
                fingerprint.update(("L|" + relative + "|" + link_target + "\n").encode("utf-8"))
                continue
            directory_count += 1
            fingerprint.update(("D|" + relative + "\n").encode("utf-8"))
            safe_directories.append(name)
        directories[:] = safe_directories
        if file_count + directory_count > MAX_ARCHIVE_ENTRIES:
            raise ConnectorError("ARCHIVE_TOO_LARGE", "The archived run exceeds the safe inventory entry limit")
        for name in files:
            path = os.path.join(current, name)
            relative = (name if relative_current == "." else relative_current + "/" + name)
            try:
                stat = os.lstat(path)
            except OSError as error:
                raise ConnectorError("ARCHIVE_INVENTORY_FAILED", "The archived run inventory could not be read: " + str(error))
            if not (stat_module.S_ISREG(stat.st_mode) or stat_module.S_ISLNK(stat.st_mode)):
                raise ConnectorError("ARCHIVE_ENTRY_NOT_ALLOWED", "The archived run contains an unsupported special file")
            file_count += 1
            total_bytes += stat.st_size
            kind = "L" if stat_module.S_ISLNK(stat.st_mode) else "F"
            fingerprint.update((kind + "|" + relative + "|" + str(stat.st_size) + "|" + repr(stat.st_mtime) + "\n").encode("utf-8"))
            if file_count + directory_count > MAX_ARCHIVE_ENTRIES:
                raise ConnectorError("ARCHIVE_TOO_LARGE", "The archived run exceeds the safe inventory entry limit")
    return {
        "fileCount": file_count,
        "directoryCount": directory_count,
        "totalBytes": total_bytes,
        "fingerprint": fingerprint.hexdigest(),
    }


def public_archived_run(manifest, archive_id, archive_path):
    archived_manifest = dict(manifest)
    archived_manifest["runPath"] = archive_path
    archived_manifest["projectPath"] = os.path.join(archive_path, "project")
    return {
        "archiveId": archive_id,
        "runId": manifest.get("runId"),
        "status": manifest.get("status"),
        "sourceRelativePath": manifest.get("sourceRelativePath"),
        "version": manifest.get("version"),
        "release": manifest.get("release"),
        "nodes": manifest.get("nodes") or [],
        "createdAt": manifest.get("createdAt"),
        "finishedAt": manifest.get("finishedAt"),
        "archivedAt": manifest.get("archivedAt"),
        "resultFileCount": len(run_result_files(archived_manifest)),
        "recoverable": True,
    }


def simulation_archive_history(_params=None):
    root = archive_root_path()
    archives = []
    for archive_id in sorted(os.listdir(root), reverse=True):
        try:
            manifest, archive_path, _manifest_path, _destination = read_archived_manifest(archive_id)
            archives.append(public_archived_run(manifest, archive_id, archive_path))
        except (ConnectorError, OSError, ValueError):
            continue
        if len(archives) >= 30:
            break
    return {"ok": True, "archives": archives, "count": len(archives), "recoverable": True}


def simulation_archive_restore_plan(params):
    params = params or {}
    archive_id = params.get("archiveId") or ""
    manifest, archive_path, _manifest_path, destination = read_archived_manifest(archive_id)
    if os.path.exists(destination):
        raise ConnectorError("ARCHIVE_RESTORE_CONFLICT", "A run with the same identifier already exists in the active run directory")
    inventory = archive_inventory(archive_path)
    approval_token = cache_digest((archive_id, manifest["runId"], destination, inventory["fingerprint"], "archive-restore-v1"))
    archived_manifest = dict(manifest)
    archived_manifest["runPath"] = archive_path
    archived_manifest["projectPath"] = os.path.join(archive_path, "project")
    return {
        "ok": True,
        "mode": "approval-required",
        "archiveId": archive_id,
        "runId": manifest["runId"],
        "status": manifest.get("status"),
        "sourceRelativePath": manifest.get("sourceRelativePath"),
        "version": manifest.get("version"),
        "release": manifest.get("release"),
        "nodes": manifest.get("nodes") or [],
        "archivedAt": manifest.get("archivedAt"),
        "destination": destination,
        "fileCount": inventory["fileCount"],
        "directoryCount": inventory["directoryCount"],
        "totalBytes": inventory["totalBytes"],
        "resultFileCount": len(run_result_files(archived_manifest)),
        "archiveFingerprint": inventory["fingerprint"],
        "approvalToken": approval_token,
        "requiresApproval": True,
        "recoverable": True,
    }


def simulation_archive_restore(params):
    params = params or {}
    plan = simulation_archive_restore_plan(params)
    if params.get("approvalToken") != plan["approvalToken"] or params.get("confirm") is not True:
        raise ConnectorError("ARCHIVE_RESTORE_APPROVAL_REQUIRED", "Restoring an archived run requires its current inventory token and explicit confirmation")
    manifest, archive_path, _manifest_path, destination = read_archived_manifest(plan["archiveId"])
    if os.path.exists(destination):
        raise ConnectorError("ARCHIVE_RESTORE_CONFLICT", "A run with the same identifier already exists in the active run directory")
    os.rename(archive_path, destination)
    destination_manifest = os.path.join(destination, "manifest.json")
    try:
        manifest["runPath"] = destination
        manifest["projectPath"] = os.path.join(destination, "project")
        manifest["logPath"] = os.path.join(destination, os.path.basename(manifest.get("logPath") or "run.log"))
        manifest["restoredAt"] = utc_time(time.time())
        manifest["restoredFromArchiveId"] = plan["archiveId"]
        manifest.pop("archivedAt", None)
        manifest.pop("archivedFrom", None)
        write_run_manifest(manifest, destination_manifest)
    except Exception as error:
        try:
            if os.path.isdir(destination) and not os.path.exists(archive_path):
                os.rename(destination, archive_path)
        except OSError:
            pass
        raise ConnectorError("ARCHIVE_RESTORE_FAILED", "The archived run could not be restored safely: " + str(error))
    result = public_run(manifest)
    result.update({
        "status": manifest.get("status"),
        "restoredAt": manifest["restoredAt"],
        "restoredFromArchiveId": plan["archiveId"],
        "recoverable": True,
    })
    return result


def simulation_archive(params):
    params = params or {}
    manifest, manifest_path = read_run_manifest(params.get("runId") or "")
    if params.get("approvalToken") != manifest.get("approvalToken") or params.get("confirm") is not True:
        raise ConnectorError("ARCHIVE_APPROVAL_REQUIRED", "Archiving a run requires explicit confirmation and its approval token")
    if manifest.get("status") in ("running", "stopping", "preparing") or run_process_is_alive(manifest):
        raise ConnectorError("RUN_ACTIVE", "An active simulation run cannot be archived")
    archive_root = archive_root_path()
    run_path = os.path.realpath(manifest["runPath"])
    expected_root = os.path.realpath(RUN_ROOT)
    if not run_path.startswith(expected_root + os.sep) or os.path.dirname(run_path) != expected_root:
        raise ConnectorError("RUN_PATH_DENIED", "Only a direct app-owned run directory may be archived")
    archived_name = "%s-%s" % (manifest["runId"], time.strftime("%Y%m%d-%H%M%S"))
    archived_path = os.path.realpath(os.path.join(archive_root, archived_name))
    suffix = 1
    while os.path.exists(archived_path):
        archived_name = "%s-%s-%d" % (manifest["runId"], time.strftime("%Y%m%d-%H%M%S"), suffix)
        archived_path = os.path.realpath(os.path.join(archive_root, archived_name))
        suffix += 1
    if not archived_path.startswith(archive_root + os.sep) or os.path.dirname(archived_path) != archive_root:
        raise ConnectorError("RUN_PATH_DENIED", "The archive target is outside the app-owned archive")
    manifest["archivedAt"] = utc_time(time.time())
    manifest["archivedFrom"] = run_path
    write_run_manifest(manifest, manifest_path)
    try:
        os.rename(run_path, archived_path)
    except OSError as error:
        manifest.pop("archivedAt", None)
        manifest.pop("archivedFrom", None)
        write_run_manifest(manifest, manifest_path)
        raise ConnectorError("RUN_ARCHIVE_FAILED", "The run could not be moved into the app-owned archive: " + str(error))
    return {
        "ok": True,
        "archiveId": archived_name,
        "runId": manifest["runId"],
        "status": "archived",
        "archivedAt": manifest["archivedAt"],
        "archivedPath": archived_path,
        "recoverable": True,
    }


METHODS = {
    "system.probe": system_probe,
    "project.tree": project_tree,
    "file.readText": read_text_file,
    "file.searchText": search_text_files,
    "file.planWrite": file_write_plan,
    "file.writeText": file_write_text,
    "file.backupHistory": file_backup_history,
    "file.planRestore": file_restore_plan,
    "file.restoreBackup": file_restore_backup,
    "project.planCreate": project_create_plan,
    "project.create": project_create,
    "project.planGenerated": generated_project_plan,
    "project.createGenerated": generated_project_create,
    "project.planAccess": project_access_plan,
    "project.setAccess": project_set_access,
    "project.analyze": project_analysis,
    "result.pltCurve": parse_plt_curve,
    "result.tdrMetadata": tdr_metadata,
    "result.tdrView": tdr_view,
    "result.tdrCutline": tdr_cutline,
    "simulation.plan": simulation_plan,
    "simulation.prepare": simulation_prepare,
    "simulation.start": simulation_start,
    "simulation.status": simulation_status,
    "simulation.stop": simulation_stop,
    "simulation.history": simulation_history,
    "simulation.archive": simulation_archive,
    "simulation.archiveHistory": simulation_archive_history,
    "simulation.planArchiveRestore": simulation_archive_restore_plan,
    "simulation.restoreArchive": simulation_archive_restore,
}


def dispatch(request):
    if not isinstance(request, dict):
        raise ConnectorError("INVALID_REQUEST", "Request must be a JSON object")
    method = request.get("method")
    if method not in METHODS:
        raise ConnectorError("METHOD_NOT_ALLOWED", "This connector only exposes fixed approved methods")
    return METHODS[method](request.get("params") or {})


def emit(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def rpc():
    line = sys.stdin.readline()
    if not line:
        raise ConnectorError("EMPTY_REQUEST", "No RPC request was received")
    return dispatch(json.loads(line))


def main():
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "probe":
            emit(system_probe())
        elif len(sys.argv) == 2 and sys.argv[1] == "rpc":
            emit(rpc())
        else:
            raise ConnectorError("USAGE", "Usage: connector.py probe|rpc")
    except ConnectorError as error:
        emit({"ok": False, "error": {"code": error.code, "message": error.message}})
        return 2
    except Exception as error:
        emit({"ok": False, "error": {"code": "CONNECTOR_FAILURE", "message": str(error)}})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
