"""Online shop deployment: DNS isolation, health ordering, route CAS and recovery."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

DEPLOY = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shop_cycle_cutover_ops", DEPLOY / "shop_cycle_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def config(dial="it-shop-next-frontend:80"):
    return {"apps": {"http": {"servers": {"srv0": {"routes": [
        {"match": [{"host": ["ybwznt.com", "www.ybwznt.com"]}], "handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": "itsp-app:80"}]}]},
        {"match": [{"host": ["shop.ybwznt.com"]}], "terminal": True, "handle": [
            {"handler": "reverse_proxy", "upstreams": [{"dial": dial}]}]},
    ]}}}}}


class OnlineFixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.r = object.__new__(ops.Release)
        self.r.root = Path(folder.name)
        self.r.release = "c" * 40
        self.r.raw = b'{"release":"fixture"}'
        self.r.prefix = "shop-cycle-" + self.r.release[:12]
        self.r.m = {"app_image": "sha256:" + "d" * 64, "frontend_image": "sha256:" + "e" * 64,
                    "old": {name: {"id": str(index + 1) * 64, "image": "sha256:" + "a" * 64}
                            for index, name in enumerate(ops.MANAGED)}}
        self.rows = {}
        for name, expected in self.r.m["old"].items():
            networks = {"it-shop-next_private": {"Aliases": [name, name.split("-")[-1]]}}
            if name == ops.APPS[3]:
                networks.update({"it-shop-nocodb-frontend": {"Aliases": [name, "frontend"]},
                                 "it-spareparts-ingress": {"Aliases": [name, "frontend"]}})
            self.rows[expected["id"]] = {
                "Id": expected["id"], "Name": "/" + name, "Image": expected["image"],
                "State": {"Running": True, "Health": {"Status": "healthy"}, "StartedAt": "t", "OOMKilled": False},
                "RestartCount": 0,
                "Config": {"Env": ["DATABASE_URL=postgresql://u:p@db/shop_next", "CYCLE_SELL_TOKEN_SHA256="],
                           "Image": expected["image"], "Labels": {}, "Hostname": name},
                "HostConfig": {"NetworkMode": "it-shop-next_private", "PortBindings": {}, "PublishAllPorts": False},
                "NetworkSettings": {"Networks": networks},
            }
        self.events = []
        self.r.inspect = lambda cid: copy.deepcopy(self.rows[cid])
        self.r.protected = Mock()
        self.r.online_review = Mock()
        self.r.schema = Mock(return_value=ops.TARGET_HEAD)
        self.r.posted_cycle_rows = Mock(return_value=0)
        self.r.http = Mock(return_value={"status": 200})
        self.r.frontend_http = Mock(return_value={"status": 200})
        self.r.entry = Mock(return_value={"active": "fixture"})

        def run(argv, **kwargs):
            self.events.append((list(argv), kwargs))
            if argv[:3] == ["docker", "network", "create"]:
                return b"f" * 64
            if argv[:2] == ["docker", "stop"]:
                self.rows[argv[-1]]["State"]["Running"] = False
            elif argv[:2] == ["docker", "start"]:
                self.rows[argv[-1]]["State"]["Running"] = True
            elif argv[0] == "curl":
                body = json.loads(kwargs["input"])
                cid = format(len(self.rows) + 1, "064x")
                candidate = argv[-1].split("name=", 1)[1]
                self.rows[cid] = {
                    "Id": cid, "Name": "/" + candidate, "Image": body["Image"],
                    "State": {"Running": False, "Health": {"Status": "healthy"}, "StartedAt": "t", "OOMKilled": False},
                    "RestartCount": 0, "Config": {k: v for k, v in body.items() if k not in {"HostConfig", "NetworkingConfig"}},
                    "HostConfig": body["HostConfig"], "NetworkSettings": {"Networks": body["NetworkingConfig"]["EndpointsConfig"]},
                }
                return json.dumps({"Id": cid}).encode()
            return b""

        self.r.run = run
        self.r.mark = Mock(return_value={})

    def create(self):
        result = self.r.create()
        self.r.mark = Mock(return_value=result)
        return result


class CandidateIsolationTests(OnlineFixture):
    def test_candidates_leave_originals_running_and_names_unchanged(self):
        created = self.create()["created"]
        self.assertEqual(set(created), set(ops.APPS))
        self.r.originals_online()
        self.assertFalse(any(argv[:2] in (["docker", "stop"], ["docker", "rename"]) for argv, _ in self.events))

    def test_frontend_resolves_only_the_candidate_backend(self):
        created = self.create()["created"]
        frontend = self.rows[created[ops.APPS[3]]]
        backend = self.rows[created[ops.APPS[0]]]
        private = self.r.prefix + "-private"
        self.assertNotIn("it-shop-next_private", frontend["NetworkSettings"]["Networks"])
        self.assertIn("backend", backend["NetworkSettings"]["Networks"][private]["Aliases"])
        for name, cid in created.items():
            row = self.rows[cid]
            self.assertFalse(row["HostConfig"]["PortBindings"])
            if name != ops.APPS[3]:
                aliases = row["NetworkSettings"]["Networks"]["it-shop-next_private"]["Aliases"]
                self.assertEqual(aliases, [self.r.candidate_name(name)])

    def test_create_refuses_a_stopped_original_without_mutating(self):
        self.rows[self.r.m["old"][ops.APPS[0]]["id"]]["State"]["Running"] = False
        with self.assertRaisesRegex(RuntimeError, "original serving container stopped"):
            self.r.create()
        self.assertFalse(self.events)

    def test_candidate_image_or_ownership_drift_is_rejected(self):
        created = self.create()["created"]
        self.rows[created[ops.APPS[0]]]["Config"]["Labels"]["shop.cycle.release"] = "other"
        with self.assertRaisesRegex(RuntimeError, "ownership differs"):
            self.r.start_candidate(ops.APPS[0], created[ops.APPS[0]])


class ActivationOrderingTests(OnlineFixture):
    def prepare(self):
        created = self.create()
        self.r.prepare_cutover = Mock()
        self.r.change_entry = Mock(side_effect=lambda **kwargs: self.events.append((["cutover"], kwargs)))
        self.r.candidate_http = Mock(side_effect=lambda path: self.events.append((["probe", path], {})) or {"status": 200})
        return created

    def test_failed_candidate_probe_does_not_stop_any_original(self):
        self.prepare()
        self.r.candidate_http.side_effect = RuntimeError("candidate unavailable")
        with self.assertRaisesRegex(RuntimeError, "candidate unavailable"):
            self.r.activate()
        self.r.originals_online()
        self.assertFalse(any(argv[:2] == ["docker", "stop"] for argv, _ in self.events))
        self.assertFalse((self.r.root / "exposure-started.json").exists())

    def test_health_checks_precede_background_handoff_and_cutover(self):
        created = self.prepare()["created"]
        self.r.activate()
        argv = [event[0] for event in self.events]
        first_probes = [i for i, item in enumerate(argv) if item[0] == "probe"][:3]
        first_stop = min(i for i, item in enumerate(argv) if item[:2] == ["docker", "stop"])
        self.assertLess(max(first_probes), first_stop)
        last_probe = max(i for i, item in enumerate(argv) if item[0] == "probe")
        last_start = max(i for i, item in enumerate(argv) if item[:2] == ["docker", "start"])
        self.assertGreater(last_probe, last_start)
        for name in ops.APPS[1:3]:
            old = self.r.m["old"][name]["id"]
            stop = next(i for i, item in enumerate(argv) if item[:2] == ["docker", "stop"] and item[-1] == old)
            start = argv.index(["docker", "start", created[name]])
            self.assertLess(stop, start)
        self.r.originals_online(background=False)
        self.assertEqual(argv[-1], ["cutover"])

    def test_recovery_transfers_background_back_before_old_start(self):
        created = self.prepare()["created"]
        self.r.activate()
        (self.r.root / "cutover-plan.json").write_text("{}")
        self.events.clear()
        self.r.recover_cutover()
        argv = [event[0] for event in self.events]
        self.assertEqual(argv[0], ["cutover"])
        self.r.change_entry.assert_called_with(forward=False)
        self.r.frontend_http.assert_any_call(ops.APPS[3], "/api/ready")
        self.r.frontend_http.assert_any_call(ops.APPS[3], "/healthz")
        for name in ops.APPS[1:3]:
            stop = next(i for i, item in enumerate(argv) if item[:2] == ["docker", "stop"] and item[-1] == created[name])
            start = argv.index(["docker", "start", self.r.m["old"][name]["id"]])
            self.assertLess(stop, start)
        self.r.originals_online()

    def test_recovery_refuses_new_cycle_data_before_any_mutation(self):
        self.prepare()
        self.r.posted_cycle_rows.return_value = 1
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, "no cycle writes"):
            self.r.recover_cutover_gate()
        self.assertFalse(self.events)


class ShopRouteTests(unittest.TestCase):
    def test_only_shop_dial_changes_and_itsp_route_is_retained(self):
        before = config()
        after = ops.replace_shop_dial(before, "it-shop-next-frontend:80", "new-front:80")
        self.assertEqual(before["apps"]["http"]["servers"]["srv0"]["routes"][0],
                         after["apps"]["http"]["servers"]["srv0"]["routes"][0])
        self.assertEqual(ops.replace_shop_dial(after, "new-front:80", "it-shop-next-frontend:80"), before)

    def test_or_branch_without_shop_host_is_rejected(self):
        before = config()
        before["apps"]["http"]["servers"]["srv0"]["routes"][1]["match"].append({"path": ["/*"]})
        with self.assertRaisesRegex(RuntimeError, "not singular"):
            ops.replace_shop_dial(before, "it-shop-next-frontend:80", "new-front:80")

    def test_shadowing_shop_route_with_another_dial_is_rejected(self):
        before = config()
        routes = before["apps"]["http"]["servers"]["srv0"]["routes"]
        shadow = copy.deepcopy(routes[1])
        shadow["handle"][0]["upstreams"][0]["dial"] = "unrelated-shop:80"
        routes.insert(0, shadow)
        with self.assertRaisesRegex(RuntimeError, "singular|differs|refused"):
            ops.shop_route(before, "it-shop-next-frontend:80")

    def test_duplicate_shop_route_is_rejected(self):
        before = config()
        routes = before["apps"]["http"]["servers"]["srv0"]["routes"]
        routes.append(copy.deepcopy(routes[1]))
        with self.assertRaisesRegex(RuntimeError, "not singular"):
            ops.shop_route(before, "it-shop-next-frontend:80")


class CaddyCASTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.r = object.__new__(ops.Release)
        self.r.root = Path(folder.name)
        self.r.release = "c" * 40
        self.r.prefix = "shop-cycle-" + self.r.release[:12]
        self.r.raw = b"manifest"
        self.file = self.r.root / "Caddyfile"
        self.file.write_text("shop.ybwznt.com { reverse_proxy it-shop-next-frontend:80 }\n")
        self.patch = patch.object(ops, "CADDYFILE", self.file)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.state = config()
        self.patch_status = 200
        self.mutations = []

        def admin(method, path, body=None, headers=None):
            if method == "GET" and path == "/config/":
                return 200, {}, json.dumps(self.state).encode()
            old = self.state["apps"]["http"]["servers"]["srv0"]["routes"][1]
            if method == "GET":
                return 200, {"etag": '"shop-route-etag"'}, json.dumps(old).encode()
            self.assertEqual(method, "PATCH")
            self.assertEqual(headers["If-Match"], '"shop-route-etag"')
            self.mutations.append(path)
            if self.patch_status != 200:
                return self.patch_status, {}, b""
            self.state["apps"]["http"]["servers"]["srv0"]["routes"][1] = json.loads(body)
            return 200, {}, b""

        self.r.admin_request = admin
        self.r.adapt_caddy = lambda data=None: config(self.r.candidate_name(ops.APPS[3]) + ":80" if
                                                     self.r.candidate_name(ops.APPS[3]).encode() in
                                                     (self.file.read_bytes() if data is None else data)
                                                     else "it-shop-next-frontend:80")
        self.r.mark = Mock(return_value={"entry": self.r.entry()})
        self.plan = self.r.prepare_cutover()

    def test_cas_changes_only_shop_persists_and_can_switch_back(self):
        inode = self.file.stat().st_ino
        self.r.change_entry()
        self.assertEqual(self.r.entry(), self.plan["new_entry"])
        self.assertEqual(self.file.stat().st_ino, inode)
        self.assertEqual(self.state["apps"]["http"]["servers"]["srv0"]["routes"][0],
                         config()["apps"]["http"]["servers"]["srv0"]["routes"][0])
        self.r.change_entry(forward=False)
        self.assertEqual(self.r.entry(), self.plan["old_entry"])

    def test_cas_rejection_can_be_retried_from_partial_file_commit(self):
        self.patch_status = 412
        with self.assertRaisesRegex(RuntimeError, "conditional shop route patch failed"):
            self.r.change_entry()
        self.assertEqual(self.file.read_text(), self.plan["new_file"])
        self.assertEqual(self.state, self.plan["old_config"])
        self.patch_status = 200
        self.r.change_entry()
        self.assertEqual(self.r.entry(), self.plan["new_entry"])

    def test_concurrent_other_site_change_is_rejected_before_write(self):
        self.state["apps"]["http"]["servers"]["srv0"]["routes"][0]["terminal"] = True
        before = self.file.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "runtime drift"):
            self.r.change_entry()
        self.assertFalse(self.mutations)
        self.assertEqual(self.file.read_bytes(), before)

    def test_concurrent_file_change_is_rejected_before_patch(self):
        self.file.write_text(self.file.read_text() + "# concurrent change\n")
        with self.assertRaisesRegex(RuntimeError, "Caddyfile drift"):
            self.r.change_entry()
        self.assertFalse(self.mutations)


if __name__ == "__main__":
    unittest.main()
