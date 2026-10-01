from __future__ import print_function

import importlib.util
import os
import shutil
import tempfile
import time
import unittest


CONNECTOR_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "connector", "aitcad_connector.py"))


class ConnectorSafetyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aitcad-connector-test-")
        self.workspace = os.path.join(self.root, "STDB_class")
        self.template = os.path.join(self.workspace, "nmos_study", "nmos_project")
        os.makedirs(os.path.join(self.template, "models"))
        with open(os.path.join(self.template, ".project"), "wb") as stream:
            stream.write(b"<projectDescription><name>nmos_project</name></projectDescription>\n")
        with open(os.path.join(self.template, "device.cmd"), "wb") as stream:
            stream.write(b"Electrode { Name=drain Voltage=0.0 }\n")
        with open(os.path.join(self.template, "models", "sdevice.par"), "wb") as stream:
            stream.write(b"Material = Silicon\n")

        previous_root = os.environ.get("AITCAD_PROJECT_ROOT")
        self.previous_root = previous_root
        os.environ["AITCAD_PROJECT_ROOT"] = self.workspace
        spec = importlib.util.spec_from_file_location("aitcad_connector_under_test", CONNECTOR_PATH)
        self.connector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.connector)
        self.connector.APP_DATA_ROOT = os.path.join(self.root, ".app")
        self.connector.CACHE_ROOT = os.path.join(self.connector.APP_DATA_ROOT, "cache")
        self.connector.RUN_ROOT = os.path.join(self.connector.APP_DATA_ROOT, "runs")
        self.connector.BACKUP_ROOT = os.path.join(self.connector.APP_DATA_ROOT, "backups")
        self.connector.CREATION_ROOT = os.path.join(self.connector.APP_DATA_ROOT, "project-creations")
        self.connector.ACCESS_POLICY_PATH = os.path.join(self.connector.APP_DATA_ROOT, "access-policy.json")
        self.connector.ACCESS_AUDIT_ROOT = os.path.join(self.connector.APP_DATA_ROOT, "access-audit")

    def tearDown(self):
        if self.previous_root is None:
            os.environ.pop("AITCAD_PROJECT_ROOT", None)
        else:
            os.environ["AITCAD_PROJECT_ROOT"] = self.previous_root
        shutil.rmtree(self.root)

    def assert_connector_error(self, code, callback):
        with self.assertRaises(self.connector.ConnectorError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def test_generated_project_is_not_bound_to_nmos_template(self):
        for name, chain, filename in (
            ("pn_diode", [{"step": "geometry", "tool": "sde"}], "geometry_dvs.cmd"),
            ("process_stack", [{"step": "oxide", "tool": "sprocess"},
                               {"step": "device", "tool": "sdevice"}], "oxide_fps.cmd"),
        ):
            with self.subTest(name=name):
                files = [{"path": filename, "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")\n"}]
                if name == "process_stack":
                    files[0]["content"] = "# model-generated\n"
                    files.append({"path": "device_des.cmd", "content": 'File { Grid="@tdr|oxide@" }\nPhysics { Mobility }\n'})
                params = {"name": name, "toolChain": chain, "files": files}
                result = self.connector.generated_project_plan(params)
                self.assertEqual(result["fileCount"], len(files))
                self.assertIn("aitcad_workspaces/" + name, result["targetRelativePath"])
                self.assertNotIn("templateId", result)

    def test_generated_project_rejects_path_changes_and_wrong_approval(self):
        params = {"name": "fresh_project", "toolChain": [{"step": "geometry", "tool": "sde"}],
                  "files": [{"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'}]}
        plan = self.connector.generated_project_plan(params)
        self.assert_connector_error("PROJECT_CREATE_APPROVAL_REQUIRED", lambda: self.connector.generated_project_create(
            dict(params, approvalToken="wrong", confirm=True)))
        self.assertFalse(os.path.exists(plan["targetPath"]))
        altered = dict(params, files=[{"path": "geometry_dvs.cmd", "content": '(sde:clear)\n(sde:build-mesh "n@node@_msh")\n'}])
        self.assert_connector_error("PROJECT_CREATE_APPROVAL_REQUIRED", lambda: self.connector.generated_project_create(
            dict(altered, approvalToken=plan["approvalToken"], confirm=True)))
        for bad_path in ("../escape.cmd", "/etc/passwd", "gtree.dat", ".project", "n1_des.cmd", "sub/file.cmd"):
            self.assert_connector_error("PROJECT_FILE_PATH_DENIED", lambda: self.connector.generated_project_plan(
                dict(params, files=[{"path": bad_path, "content": "safe"}])))

    def test_generated_project_parameters_are_approved_and_macros_checked(self):
        params = {"name": "parameter_project",
                  "toolChain": [{"step": "geometry", "tool": "sde"}, {"step": "device", "tool": "sdevice"}],
                  "parameters": [{"name": "Vg", "value": "0.25", "step": 2}],
                  "files": [
                      {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                      {"path": "device_des.cmd", "content": 'File { Grid="@tdr|geometry@" }\nElectrode { Name=gate Voltage=@Vg@ }\n'},
                  ]}
        approved = self.connector.generated_project_plan(params)
        self.assertEqual(approved["parameters"][0]["name"], "Vg")
        self.assert_connector_error("PROJECT_CREATE_APPROVAL_REQUIRED", lambda: self.connector.generated_project_create(
            dict(params, parameters=[{"name": "Vg", "value": "0.3", "step": 2}],
                 approvalToken=approved["approvalToken"], confirm=True)))
        self.assert_connector_error("PROJECT_PARAMETER_MISSING", lambda: self.connector.generated_project_plan(
            dict(params, parameters=[])))
        self.assert_connector_error("PROJECT_PARAMETERS_INVALID", lambda: self.connector.generated_project_plan(
            dict(params, parameters=[{"name": "Vg", "value": "{[exec bad]}", "step": 2}])))

    def test_official_swb_tdrdat_macro_is_allowed(self):
        params = {
            "name": "macro_project",
            "toolChain": [{"step": "geometry", "tool": "sde"}, {"step": "device", "tool": "sdevice"}],
            "files": [
                {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                {"path": "device_des.cmd", "content": (
                    'File { Grid="@tdr|geometry@" Plot="@tdrdat@" Current="@plot@" Output="@log@" }\n'
                )},
            ],
        }
        self.assertEqual(self.connector.generated_project_plan(params)["fileCount"], 2)

    def test_security_scan_ignores_comments_but_rejects_real_shell_escape(self):
        params = {
            "name": "comment_project", "toolChain": [{"step": "geometry", "tool": "sde"}],
            "files": [{"path": "geometry_dvs.cmd", "content": (
                '; coordinate system (x points right)\n(sde:build-mesh "n@node@_msh")\n'
            )}],
        }
        self.assertEqual(self.connector.generated_project_plan(params)["fileCount"], 1)
        params["name"] = "unsafe_project"
        params["files"][0]["content"] = '(system:command "touch /tmp/unsafe")\n(sde:build-mesh "n@node@_msh")\n'
        self.assert_connector_error("PROJECT_SOURCE_UNSAFE", lambda: self.connector.generated_project_plan(params))

    def test_generated_project_destination_is_approved_and_bounded(self):
        params = {"name": "different_project", "directory": "junctions",
                  "toolChain": [{"step": "geom", "tool": "sde"}],
                  "files": [{"path": "geom_dvs.cmd", "content": "(sde:clear)\n(sde:build-mesh \"n@node@_msh\")\n"}]}
        planned = self.connector.generated_project_plan(params)
        self.assertTrue(planned["targetRelativePath"].endswith("aitcad_workspaces/junctions/different_project"))
        changed = self.connector.generated_project_plan(dict(params, directory="bjt"))
        self.assertNotEqual(planned["approvalToken"], changed["approvalToken"])
        for bad in ("../old", "/tmp", "nested/level", "..", "space name"):
            self.assert_connector_error("PROJECT_DIRECTORY_INVALID", lambda: self.connector.generated_project_plan(
                dict(params, directory=bad)))

    def test_existing_generated_project_has_a_specific_collision_error(self):
        target = os.path.join(self.connector.NEW_PROJECT_ROOT, "existing_project")
        os.makedirs(target)
        params = {
            "name": "existing_project", "toolChain": [{"step": "geom", "tool": "sde"}],
            "files": [{"path": "geom_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'}],
        }
        self.assert_connector_error("PROJECT_TARGET_EXISTS", lambda: self.connector.generated_project_plan(params))

    def test_user_can_choose_an_existing_parent_inside_workspace(self):
        chosen = os.path.join(self.workspace, "research", "junctions")
        os.makedirs(chosen)
        params = {
            "name": "chosen_parent_project", "targetParent": "research/junctions",
            "toolChain": [{"step": "geom", "tool": "sde"}],
            "files": [{"path": "geom_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'}],
        }
        planned = self.connector.generated_project_plan(params)
        self.assertEqual(planned["targetParent"], "research/junctions")
        self.assertEqual(planned["parentPath"], os.path.realpath(chosen))
        self.assertEqual(planned["targetRelativePath"], "research/junctions/chosen_parent_project")
        default_plan = self.connector.generated_project_plan(dict(params, targetParent=None))
        self.assertNotEqual(planned["approvalToken"], default_plan["approvalToken"])

        with open(os.path.join(chosen, "gtree.dat"), "wb") as stream:
            stream.write(b"# existing project\n")
        self.assert_connector_error("PROJECT_TARGET_PARENT_INVALID", lambda: self.connector.generated_project_plan(params))
        for bad in ("../outside", "/tmp", "research//junctions", "missing/folder"):
            self.assert_connector_error("PROJECT_TARGET_PARENT_INVALID", lambda: self.connector.generated_project_plan(
                dict(params, targetParent=bad)))

    def test_generated_project_rejects_known_tool_syntax_failures(self):
        device = {"name": "bad_device", "toolChain": [{"step": "device", "tool": "sdevice"}],
                  "files": [{"path": "device_des.cmd", "content": "; invalid for SDevice\nPhysics { SRH }\n"}]}
        self.assert_connector_error("PROJECT_SDEVICE_COMMENT_INVALID", lambda: self.connector.generated_project_plan(device))
        device["files"][0]["content"] = 'File { Parameter="@parameter@" }\nPhysics { Recombination(SRH) }\n'
        self.assert_connector_error("PROJECT_SDEVICE_PARAMETER_FILE_MISSING", lambda: self.connector.generated_project_plan(device))
        device = {
            "name": "bad_device_plot",
            "toolChain": [{"step": "geometry", "tool": "sde"}, {"step": "device", "tool": "sdevice"}],
            "files": [
                {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                {"path": "device_des.cmd", "content": (
                    'File { Grid="@tdr|geometry@" }\nPlot { Potential CurrentDensity }\n'
                )},
            ],
        }
        self.assert_connector_error("PROJECT_SDEVICE_PLOT_INVALID", lambda: self.connector.generated_project_plan(device))
        device["files"][1]["content"] = 'File { Grid="@tdr|geometry@" }\nPlot { Potential NetActive }\n'
        self.assert_connector_error("PROJECT_SDEVICE_PLOT_INVALID", lambda: self.connector.generated_project_plan(device))
        device["files"][1]["content"] = 'File { Grid="@tdr|geometry@" }\nCurrentPlot { eCurrent hCurrent }\n'
        self.assert_connector_error("PROJECT_SDEVICE_CURRENTPLOT_INVALID", lambda: self.connector.generated_project_plan(device))
        device["files"][1]["content"] = 'File { Grid="@tdr|geometry@" Parameter="@parameter@" }\n'
        device["files"].append({"path": "arbitrary.par", "content": "Material=\"Silicon\" {}\n"})
        self.assert_connector_error("PROJECT_SDEVICE_PARAMETER_FILE_MISSING", lambda: self.connector.generated_project_plan(device))
        bad_sde = {"name": "bad_sde_namespace", "toolChain": [{"step": "geometry", "tool": "sde"}],
                   "files": [{"path": "geometry_dvs.cmd", "content": (
                       '(sdegeo:define-constant-profile "D" "BoronActiveConcentration" 1e16)\n'
                       '(sde:build-mesh "n@node@_msh")\n')} ]}
        self.assert_connector_error("PROJECT_SDE_DOPING_NAMESPACE_INVALID", lambda: self.connector.generated_project_plan(bad_sde))
        bad_sde["name"] = "bad_sde_refinement"
        bad_sde["files"][0]["content"] = (
            '(sdedr:refine-mesh "window" "size")\n(sde:build-mesh "n@node@_msh")\n'
        )
        self.assert_connector_error("PROJECT_SDE_REFINEMENT_INVALID", lambda: self.connector.generated_project_plan(bad_sde))
        bad_sde["name"] = "bad_sde_refinement_box"
        bad_sde["files"][0]["content"] = (
            '(sdedr:define-refinement-size "size" 0.1 0.1 0.05 0.05)\n'
            '(sdedr:define-refinement-placement "place" "size" (box 0 0 0 1 1 0))\n'
            '(sde:build-mesh "n@node@_msh")\n'
        )
        self.assert_connector_error("PROJECT_SDE_REFINEMENT_WINDOW_INVALID", lambda: self.connector.generated_project_plan(bad_sde))
        duplicate_mesh = {
            "name": "bad_mesh_flow",
            "toolChain": [{"step": "geometry", "tool": "sde"}, {"step": "mesh", "tool": "snmesh"}],
            "files": [
                {"path": "geometry_dvs.cmd", "content": '(sde:build-mesh "n@node@_msh")\n'},
                {"path": "mesh_msh.cmd", "content": 'Definitions { Refinement "r" { MaxElementSize=(0.1 0.1) } }\nPlacements {}\n'},
            ],
        }
        self.assert_connector_error("PROJECT_MESH_FLOW_CONFLICT", lambda: self.connector.generated_project_plan(duplicate_mesh))

    def test_workbench_parameter_override_only_changes_isolated_gtree(self):
        project = os.path.join(self.root, "isolated-project")
        os.makedirs(project)
        gtree = """# --- simulation flow
nmos_sde sde "" {}
nmos_sde nmos_l "0.18" {0.18}
nmos_sde con_pwell "1e18" {1e18}
sdevice sdevice "" {}
sdevice Vd "0.1" {1.8}
# --- variables
# --- simulation tree
0 1 0 {} {default} 0
1 2 1 {0.18} {default} 0
2 3 2 {1e18} {default} 0
3 4 3 {} {default} 0
4 5 4 {1.8} {default} 0
"""
        path = os.path.join(project, "gtree.dat")
        with open(path, "wb") as stream:
            stream.write(gtree.encode("utf-8"))
        applied = self.connector.apply_workbench_parameter_overrides(project, {"con_pwell": "3e18"})
        self.assertEqual(applied[0]["treeRowsUpdated"], 1)
        with open(path, "rb") as stream:
            updated = stream.read().decode("utf-8")
        self.assertIn('nmos_sde con_pwell "3e18" {3e18}', updated)
        self.assertIn("2 3 2 {3e18} {default} 0", updated)
        self.assertNotIn('nmos_sde con_pwell "1e18" {1e18}', updated)

    def test_file_write_requires_current_token_and_creates_backup(self):
        relative_path = "nmos_study/nmos_project/device.cmd"
        original = b"Electrode { Name=drain Voltage=0.0 }\n"
        updated = "Electrode { Name=drain Voltage=1.0 }\n"
        plan = self.connector.dispatch({
            "method": "file.planWrite",
            "params": {"relativePath": relative_path, "content": updated},
        })
        self.assertEqual(plan["additions"], 1)
        self.assertEqual(plan["deletions"], 1)
        self.assertIn("Voltage=1.0", plan["diff"])

        self.assert_connector_error("WRITE_APPROVAL_REQUIRED", lambda: self.connector.dispatch({
            "method": "file.writeText",
            "params": {"relativePath": relative_path, "content": updated, "approvalToken": "wrong", "confirm": True},
        }))
        with open(os.path.join(self.template, "device.cmd"), "rb") as stream:
            self.assertEqual(stream.read(), original)

        result = self.connector.dispatch({
            "method": "file.writeText",
            "params": {"relativePath": relative_path, "content": updated, "approvalToken": plan["approvalToken"], "confirm": True},
        })
        self.assertTrue(result["recoverable"])
        with open(os.path.join(self.template, "device.cmd"), "rb") as stream:
            self.assertEqual(stream.read(), updated.encode("utf-8"))
        with open(result["backupPath"], "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertTrue(os.path.isfile(result["manifestPath"]))

        history = self.connector.dispatch({"method": "file.backupHistory", "params": {}})
        self.assertEqual(len(history["backups"]), 1)
        self.assertEqual(history["backups"][0]["backupId"], result["backupId"])
        restore_plan = self.connector.dispatch({
            "method": "file.planRestore",
            "params": {"backupId": result["backupId"]},
        })
        self.assertIn("Voltage=0.0", restore_plan["diff"])
        self.assert_connector_error("FILE_RESTORE_APPROVAL_REQUIRED", lambda: self.connector.dispatch({
            "method": "file.restoreBackup",
            "params": {"backupId": result["backupId"], "approvalToken": restore_plan["approvalToken"], "confirm": False},
        }))
        restored = self.connector.dispatch({
            "method": "file.restoreBackup",
            "params": {"backupId": result["backupId"], "approvalToken": restore_plan["approvalToken"], "confirm": True},
        })
        self.assertEqual(restored["status"], "restored")
        with open(os.path.join(self.template, "device.cmd"), "rb") as stream:
            self.assertEqual(stream.read(), original)
        with open(restored["safetyBackupPath"], "rb") as stream:
            self.assertEqual(stream.read(), updated.encode("utf-8"))
        self.assertTrue(os.path.isfile(restored["manifestPath"]))

    def test_project_create_requires_approval_and_never_overwrites(self):
        params = {"name": "nmos_experiment_01", "templateId": "nmos-teaching"}
        plan = self.connector.dispatch({"method": "project.planCreate", "params": params})
        self.assertEqual(plan["fileCount"], 3)
        self.assertEqual([item["path"] for item in plan["files"]], [".project", "device.cmd", "models/sdevice.par"])
        self.assertFalse(plan["filesTruncated"])
        self.assertEqual(plan["targetRelativePath"], "aitcad_workspaces/nmos_experiment_01")

        self.assert_connector_error("PROJECT_CREATE_APPROVAL_REQUIRED", lambda: self.connector.dispatch({
            "method": "project.create",
            "params": dict(params, approvalToken=plan["approvalToken"], confirm=False),
        }))
        self.assertFalse(os.path.exists(plan["targetPath"]))

        result = self.connector.dispatch({
            "method": "project.create",
            "params": dict(params, approvalToken=plan["approvalToken"], confirm=True),
        })
        self.assertEqual(result["status"], "created")
        self.assertTrue(os.path.isfile(os.path.join(result["path"], ".project")))
        self.assertTrue(os.path.isfile(result["manifestPath"]))
        with open(os.path.join(self.template, "device.cmd"), "rb") as stream:
            self.assertEqual(stream.read(), b"Electrode { Name=drain Voltage=0.0 }\n")

        self.assert_connector_error("PROJECT_ALREADY_EXISTS", lambda: self.connector.dispatch({
            "method": "project.planCreate",
            "params": params,
        }))

    def test_cutline_script_is_fixed_and_metadata_is_enriched(self):
        target = os.path.join(self.template, "n5_des.tdr")
        with open(target, "wb") as stream:
            stream.write(b"test-only-tdr-placeholder")
        profile = {"version": "2018", "release": "test"}
        metadata = self.connector.enrich_tdr_metadata({
            "fields": ["ElectrostaticPotential", "eDensity", "eCurrentDensity-V", "DopingConcentration"],
            "datasets": ["n5_des"],
        }, target, profile)
        self.assertEqual(metadata["resultKind"], "device-state")
        self.assertEqual(metadata["nodeHint"], 5)
        self.assertEqual(metadata["datasetCount"], 1)
        self.assertIn("ElectrostaticPotential", metadata["fieldGroups"]["electrostatic"])
        self.assertIn("DopingConcentration", metadata["fieldGroups"]["material"])

        script = self.connector.svisual_cutline_script(
            target,
            "ElectrostaticPotential",
            (-1.0, 0.001, 1.0, 0.001),
        )
        self.assertIn("create_cutline -plot $plot -type free", script)
        self.assertIn("get_variable_data Distance -dataset $cut_dataset", script)
        self.assertIn("AITCAD_CUT_POINT_COUNT", script)
        self.assertIn("if {$point_count > 20000}", script)
        self.assertNotIn("exec ", script)
        self.assert_connector_error("TDR_CUT_COORDINATE_INVALID", lambda: self.connector.validated_cut_coordinate({"x1": "NaN"}, "x1"))

    def test_project_access_policy_is_approval_gated_enforced_and_recoverable(self):
        second_project = os.path.join(self.workspace, "class_examples", "second_project")
        os.makedirs(second_project)
        with open(os.path.join(second_project, ".project"), "wb") as stream:
            stream.write(b"<projectDescription><name>second_project</name></projectDescription>\n")

        relative_path = "nmos_study/nmos_project"
        plan = self.connector.dispatch({
            "method": "project.planAccess",
            "params": {"relativePath": relative_path, "action": "restrict"},
        })
        self.assertTrue(plan["recoverable"])
        self.assertEqual(plan["resultingAllowedProjectCount"], 1)
        self.assert_connector_error("PROJECT_ACCESS_APPROVAL_REQUIRED", lambda: self.connector.dispatch({
            "method": "project.setAccess",
            "params": {"relativePath": relative_path, "action": "restrict", "approvalToken": plan["approvalToken"], "confirm": False},
        }))

        restricted = self.connector.dispatch({
            "method": "project.setAccess",
            "params": {"relativePath": relative_path, "action": "restrict", "approvalToken": plan["approvalToken"], "confirm": True},
        })
        self.assertEqual(restricted["status"], "restricted")
        self.assertTrue(os.path.isfile(self.connector.ACCESS_POLICY_PATH))
        self.assertTrue(os.path.isfile(restricted["auditPath"]))
        status = self.connector.dispatch({"method": "system.probe", "params": {}})
        self.assertNotIn(relative_path, [project["relativePath"] for project in status["projects"]])
        self.assertIn(relative_path, [project["relativePath"] for project in status["restrictedProjects"]])
        self.assert_connector_error("PROJECT_ACCESS_RESTRICTED", lambda: self.connector.dispatch({
            "method": "file.readText",
            "params": {"relativePath": relative_path + "/device.cmd"},
        }))
        self.assert_connector_error("LAST_PROJECT_ACCESS_REQUIRED", lambda: self.connector.dispatch({
            "method": "project.planAccess",
            "params": {"relativePath": "class_examples/second_project", "action": "restrict"},
        }))

        restore_plan = self.connector.dispatch({
            "method": "project.planAccess",
            "params": {"relativePath": relative_path, "action": "restore"},
        })
        restored = self.connector.dispatch({
            "method": "project.setAccess",
            "params": {"relativePath": relative_path, "action": "restore", "approvalToken": restore_plan["approvalToken"], "confirm": True},
        })
        self.assertEqual(restored["status"], "restored")
        preview = self.connector.dispatch({
            "method": "file.readText",
            "params": {"relativePath": relative_path + "/device.cmd"},
        })
        self.assertIn("Voltage=0.0", preview["content"])

    def test_app_owned_run_results_are_read_only_listed_and_path_bounded(self):
        run_id = "20260719-result-test"
        run_path = os.path.join(self.connector.RUN_ROOT, run_id)
        project_path = os.path.join(run_path, "project")
        os.makedirs(project_path)
        plt_path = os.path.join(project_path, "n5_des.plt")
        with open(plt_path, "wb") as stream:
            stream.write(b'DF-ISE text\nInfo { datasets = [ "gate OuterVoltage" "drain TotalCurrent" ] }\nData { 0 1e-9 1 2e-6 }\n')
        tdr_path = os.path.join(project_path, "n5_des.tdr")
        with open(tdr_path, "wb") as stream:
            stream.write(b"test-tdr")
        old_path = os.path.join(project_path, "old.plt")
        with open(old_path, "wb") as stream:
            stream.write(b'DF-ISE text\nInfo { datasets = [ "gate OuterVoltage" "drain TotalCurrent" ] }\nData { 0 0 }\n')
        started = time.time() - 2
        os.utime(old_path, (started - 10, started - 10))
        manifest = {
            "runId": run_id,
            "runPath": run_path,
            "projectPath": project_path,
            "sourceRelativePath": "nmos_study/nmos_project",
            "version": "2018",
            "release": "test",
            "nodes": [{"nodeId": 5, "tool": "SDEVICE"}],
            "createdAt": self.connector.utc_time(started),
            "updatedAt": self.connector.utc_time(time.time()),
            "startedTimestamp": started,
            "status": "completed",
            "approvalToken": "test-token",
            "logPath": os.path.join(run_path, "run.log"),
            "message": "test",
        }
        self.connector.write_json_cache(os.path.join(run_path, "manifest.json"), manifest)

        listed = self.connector.run_result_files(manifest)
        self.assertEqual(set(item["relativePath"] for item in listed), set(("n5_des.tdr", "n5_des.plt")))
        self.assertEqual(dict((item["relativePath"], item["extension"]) for item in listed), {"n5_des.tdr": "tdr", "n5_des.plt": "plt"})
        curve = self.connector.dispatch({
            "method": "result.pltCurve",
            "params": {"runId": run_id, "relativePath": "n5_des.plt"},
        })
        self.assertEqual(curve["sourceKind"], "run")
        self.assertEqual(curve["runId"], run_id)
        self.assertEqual(curve["pointCount"], 2)
        self.assert_connector_error("RUN_RESULT_NOT_LISTED", lambda: self.connector.dispatch({
            "method": "result.pltCurve",
            "params": {"runId": run_id, "relativePath": "old.plt"},
        }))
        self.assert_connector_error("RUN_RESULT_PATH_INVALID", lambda: self.connector.dispatch({
            "method": "result.pltCurve",
            "params": {"runId": run_id, "relativePath": "../outside.plt"},
        }))
        self.assert_connector_error("RUN_RESULT_TYPE_MISMATCH", lambda: self.connector.dispatch({
            "method": "result.pltCurve",
            "params": {"runId": run_id, "relativePath": "n5_des.tdr"},
        }))

        archived = self.connector.dispatch({
            "method": "simulation.archive",
            "params": {"runId": run_id, "approvalToken": "test-token", "confirm": True},
        })
        self.assertEqual(archived["status"], "archived")
        self.assertFalse(os.path.exists(run_path))
        self.assertTrue(os.path.isdir(archived["archivedPath"]))
        archives = self.connector.dispatch({"method": "simulation.archiveHistory", "params": {}})
        self.assertEqual(archives["count"], 1)
        self.assertEqual(archives["archives"][0]["archiveId"], archived["archiveId"])
        self.assertEqual(archives["archives"][0]["resultFileCount"], 2)

        os.makedirs(run_path)
        self.assert_connector_error("ARCHIVE_RESTORE_CONFLICT", lambda: self.connector.dispatch({
            "method": "simulation.planArchiveRestore",
            "params": {"archiveId": archived["archiveId"]},
        }))
        os.rmdir(run_path)
        restore_plan = self.connector.dispatch({
            "method": "simulation.planArchiveRestore",
            "params": {"archiveId": archived["archiveId"]},
        })
        self.assertEqual(restore_plan["runId"], run_id)
        self.assertEqual(restore_plan["resultFileCount"], 2)
        self.assert_connector_error("ARCHIVE_RESTORE_APPROVAL_REQUIRED", lambda: self.connector.dispatch({
            "method": "simulation.restoreArchive",
            "params": {"archiveId": archived["archiveId"], "approvalToken": "wrong", "confirm": True},
        }))
        restored = self.connector.dispatch({
            "method": "simulation.restoreArchive",
            "params": {"archiveId": archived["archiveId"], "approvalToken": restore_plan["approvalToken"], "confirm": True},
        })
        self.assertEqual(restored["runId"], run_id)
        self.assertEqual(restored["status"], "completed")
        self.assertTrue(os.path.isdir(run_path))
        self.assertFalse(os.path.exists(archived["archivedPath"]))
        active_history = self.connector.dispatch({"method": "simulation.history", "params": {}})
        self.assertEqual(active_history["count"], 1)
        self.assertEqual(active_history["runs"][0]["runId"], run_id)
        restored_curve = self.connector.dispatch({
            "method": "result.pltCurve",
            "params": {"runId": run_id, "relativePath": "n5_des.plt"},
        })
        self.assertEqual(restored_curve["pointCount"], 2)


if __name__ == "__main__":
    unittest.main()
