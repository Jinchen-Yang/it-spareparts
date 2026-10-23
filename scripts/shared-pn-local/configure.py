"""Create synthetic loopback-only acceptance configuration, never read a .env."""

import argparse
import json
from pathlib import Path
import secrets
import subprocess
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--itsp", required=True, type=Path)
    parser.add_argument("--shop", required=True, type=Path)
    parser.add_argument("--postgres-port", type=int, default=32785)
    args = parser.parse_args()
    destination = args.directory.resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = destination / "config.json"
    if config_path.exists():
        raise SystemExit("Existing fixture preserved; reuse its config or choose a new directory")
    if not 1024 <= args.postgres_port <= 65535:
        raise SystemExit("Invalid local PostgreSQL port")
    itsp, shop = args.itsp.resolve(), args.shop.resolve()
    for repository in (itsp, shop):
        if not (repository / "backend/alembic.ini").is_file():
            raise SystemExit(f"Missing repository migrations: {repository}")
    certificate, key = destination / "localhost.pem", destination / "localhost.key"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(certificate), "-days", "2",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    stem = f"postgresql+psycopg://spareparts:spareparts@127.0.0.1:{args.postgres_port}/"
    suffix = uuid4().hex[:12]
    value = {
        "itsp_repo": str(itsp), "shop_repo": str(shop),
        "itsp_database": stem + "pn_local_accept_itsp_" + suffix,
        "shop_database": stem + "pn_local_accept_shop_" + suffix,
        "cerbos_url": "http://127.0.0.1:28492",
        "tls_cert": str(certificate), "tls_key": str(key),
        "shop_subject": str(uuid4()),
        **{name: secrets.token_urlsafe(48) for name in (
            "password", "login_secret", "proof_secret", "delegation_secret", "confirmation_secret"
        )},
    }
    with config_path.open("x") as handle:
        config_path.chmod(0o600)
        json.dump(value, handle, indent=2)
    print(f"Synthetic configuration written to {config_path}; credentials were not printed")


if __name__ == "__main__":
    main()
