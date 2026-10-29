"""jinchen-dev 专用真实 PG15 合成恢复演练，不读取或连接生产。

python3 .deploy/tests/test_presales3_ops_restore.py
只创建带随机前缀、network=none、无发布端口的自有容器。
"""
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest

SPEC = importlib.util.spec_from_file_location("presales3_ops", Path(__file__).resolve().parents[1] / "presales3_ops.py")
ops = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ops)


def run(argv, input=None):
    return subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=True, timeout=180).stdout


class SyntheticRestoreTest(unittest.TestCase):
    def test_two_heads_restore_and_same_count_row_update_detection(self):
        self.assertEqual(run(["hostname"]).decode().strip(), "jinchen-dev", "synthetic rehearsal is dev-host only")
        image = json.loads(run(["docker", "image", "inspect", "postgres:15"]))[0]["Id"]
        unique = "itsp-presales3-synthetic-" + secrets.token_hex(5)
        sha = "a" * 40
        cid = None
        with tempfile.TemporaryDirectory(prefix=unique + "-") as directory:
            root = Path(directory)
            env = root / "source.env"
            ops.write_new(env, "POSTGRES_USER=spareparts\nPOSTGRES_DB=spareparts\nPOSTGRES_PASSWORD=" + secrets.token_hex(32) + "\n")
            try:
                cid = run(["docker", "run", "-d", "--name", unique, "--network", "none", "--memory", "512m", "--cpus", "1",
                           "--pids-limit", "128", "--label", "itsp.synthetic=" + unique, "--env-file", str(env), image]).decode().strip()
                for _ in range(60):
                    probe = subprocess.run(["docker", "exec", cid, "pg_isready", "-h", "127.0.0.1", "-U", "spareparts"], capture_output=True)
                    if probe.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    self.fail("synthetic PG15 not ready")
                sql = "CREATE TABLE alembic_version(version_num varchar(32)); INSERT INTO alembic_version VALUES('oldhead');\n"
                for table in ops.TABLES:
                    sql += 'CREATE TABLE "' + table + '" (id integer PRIMARY KEY, payload text); '
                    sql += 'INSERT INTO "' + table + '" VALUES(1,\'synthetic one\'),(2,\'synthetic two\');\n'
                run(["docker", "exec", "-i", cid, "psql", "-U", "spareparts", "-d", "spareparts", "-v", "ON_ERROR_STOP=1"], sql.encode())
                release = ops.Release.__new__(ops.Release)
                release.root, release.prefix, release.sha = root, unique, sha
                release.marker = lambda name: {"db_image": image}
                expected = {table: 2 for table in ops.TABLES}
                receipts = []
                for head in ("oldhead", "newhead"):
                    run(["docker", "exec", cid, "psql", "-U", "spareparts", "-d", "spareparts", "-c",
                         "UPDATE alembic_version SET version_num='" + head + "'"])
                    dump = root / (head + ".dump")
                    ops.write_new(dump, run(["docker", "exec", cid, "pg_dump", "-U", "spareparts", "-d", "spareparts", "-Fc"]))
                    receipt = release.restore(dump, head, expected, head)
                    self.assertTrue(receipt["actual_restore_performed"])
                    self.assertEqual(receipt["counts"], expected)
                    receipts.append(receipt)
                # 全旧列投影比较：加默认列仍相同，原列同计数更新必须阻断回退。
                ops.DB = cid
                original_sql = release.sql
                release.sql = lambda query, container=cid: original_sql(query, container=container)
                before = release.data_fingerprints()
                release.marker = lambda name: {"data_fingerprints": before}
                run(["docker", "exec", cid, "psql", "-U", "spareparts", "-d", "spareparts", "-c",
                     "ALTER TABLE presales_offer ADD COLUMN extra text DEFAULT 'new default'"])
                self.assertTrue(release.unchanged_business())
                run(["docker", "exec", cid, "psql", "-U", "spareparts", "-d", "spareparts", "-c",
                     "UPDATE presales_offer SET payload='changed without a count change' WHERE id=1"])
                with self.assertRaisesRegex(RuntimeError, "content changed"):
                    release.unchanged_business()
                print(json.dumps({"synthetic": True, "real_pg15_restores": len(receipts), "network": "none",
                                  "ports": [], "old_new_heads_checked": True, "same_count_mutation_rejected": True}))
            finally:
                if cid:
                    row = json.loads(run(["docker", "inspect", cid]))[0]
                    self.assertEqual(row["Id"], cid)
                    self.assertEqual(row["Name"], "/" + unique)
                    self.assertEqual(row["Config"]["Labels"].get("itsp.synthetic"), unique)
                    run(["docker", "rm", "-f", "-v", cid])


if __name__ == "__main__":
    unittest.main(verbosity=2)
