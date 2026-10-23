"""Isolated dual-repository acceptance host. Synthetic accounts; loopback only.

Run with the corresponding repository's Python. Configuration is generated outside
the repositories; no .env file or production credential is loaded. The only injected
transport is a real HTTPS socket with this fixture's private CA, never MockTransport.
"""
import argparse
import json
import os
from pathlib import Path
import ssl
import sys
from uuid import UUID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["itsp", "shop"])
    parser.add_argument("action", choices=["prepare", "serve"])
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config["cerbos_url"] != "http://127.0.0.1:28492":
        raise ValueError("Only the local acceptance policy engine is permitted")
    root = Path(config[args.role + "_repo"]).resolve()
    sys.path.insert(0, str(root / "backend"))
    os.chdir(args.config.parent)
    # Clear inherited provider configuration; this process is an offline fixture.
    for key in list(os.environ):
        if key not in {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH"}:
            os.environ.pop(key)
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    dsn = config[args.role + "_database"]
    url = make_url(dsn)
    if url.host != "127.0.0.1" or not url.database.startswith("pn_local_accept_"):
        raise ValueError("Only the dedicated loopback acceptance database is permitted")
    from app import config as application_config
    if args.role == "itsp":
        from app.authz.catalog import RESOURCE_DEFINITIONS
        from app.authz.pn_resources import CATALOG_ACTIONS
        resources = ["part_master", "part_identity", "part_content", "part_gallery"]
        settings = application_config.Settings(_env_file=None, database_url=dsn,
            secret_key=config["login_secret"], admin_password=config["password"],
            pn_shop_proof_secret=config["proof_secret"], pn_delegation_secret=config["delegation_secret"],
            pn_confirmation_secret=config["confirmation_secret"], authorization_mode="unified_modules",
            authz_enabled_resources=resources, cerbos_url=config["cerbos_url"],
            pn_shop_allowed_actions=[],
            pn_shop_allowed_fields={r: sorted(RESOURCE_DEFINITIONS[r].field_permissions - {"legacy_evidence"}) for r in resources},
            cors_origins=["http://127.0.0.1:28479"])
    else:
        settings = application_config.Settings(_env_file=None, app_env="test", database_url=dsn,
            shared_pn_enabled=True, shared_pn_url="https://127.0.0.1:28481",
            shared_pn_proof_secret=config["proof_secret"], ai_paused=True)
    application_config.get_settings = lambda: settings
    if args.role == "itsp":
        from app.services.pn_catalog_access import MIGRATION_ACTIONS
        settings.pn_shop_allowed_actions = sorted(set(CATALOG_ACTIONS) - MIGRATION_ACTIONS)
    if args.action == "prepare":
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            if not connection.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": url.database}):
                connection.exec_driver_sql('CREATE DATABASE "' + url.database + '"')
        admin.dispose()
        from alembic.config import Config
        from alembic import command
        command.upgrade(Config(str(root / "backend/alembic.ini")), "head")
        if args.role == "itsp":
            from app.db import SessionLocal
            from app.auth import hash_password
            from app.models.system import SysUser
            from app.models.authorization import SysAuthorizationGrant
            from app.authz.pn_models import CatalogSubjectBinding
            from app.authz.runtime import activate_configured_resources
            from app.db import engine
            with SessionLocal.begin() as db:
                if db.query(SysUser).filter_by(username="pn-local-editor").first() is None:
                    user = SysUser(username="pn-local-editor", display_name="隔离验收账号", role="readonly",
                        password_hash=hash_password(config["password"]), token_version=1)
                    db.add(user)
                    db.flush()
                    for resource in resources:
                        definition = RESOURCE_DEFINITIONS[resource]
                        db.add(SysAuthorizationGrant(user_id=user.id, resource=resource,
                            actions=sorted(definition.actions), scope="all",
                            field_permissions=sorted(definition.field_permissions), version=1))
                    db.add(CatalogSubjectBinding(issuer="shop", subject=UUID(config["shop_subject"]),
                        user_id=user.id, active=True, version=1, updated_by=user.id))
            activate_configured_resources(engine)
        else:
            from app.db import Database
            from app.auth import hash_password
            from app.models import User
            from app.shared_pn.control import activate_cutover
            database = Database(settings)
            with database.sessions.begin() as db:
                if db.get(User, UUID(config["shop_subject"])) is None:
                    db.add(User(id=UUID(config["shop_subject"]), username="pn-local-editor", display_name="隔离验收账号",
                        password_hash=hash_password(config["password"]), role="admin"))
                    db.flush()
                activate_cutover(db, manifest_sha256="0" * 64, activation_ref="synthetic-empty-fixture",
                    activated_by="local-acceptance")
            database.dispose()
        print(args.role + " isolated migration and seed ready", flush=True)
        return
    import uvicorn
    if args.role == "itsp":
        from app.main import app
        uvicorn.run(app, host="127.0.0.1", port=28481, access_log=False,
            ssl_keyfile=config["tls_key"], ssl_certfile=config["tls_cert"])
    else:
        import httpx
        from app.db import Database
        from app.main import create_app
        from app.shared_pn.delegation import AuthorityDelegationProvider
        from app.services.storage import MemoryStorage
        database = Database(settings)
        tls = ssl.create_default_context(cafile=config["tls_cert"])
        # Real network transport, restricted to the configured localhost authority.
        class LocalTransport(httpx.HTTPTransport):
            def __enter__(self):
                return self
            def __exit__(self, *_exc):
                pass
            def handle_request(self, request):
                if request.url.host != "127.0.0.1" or request.url.port != 28481 or request.url.scheme != "https":
                    raise RuntimeError("External egress is disabled in acceptance")
                return super().handle_request(request)
            def close(self):
                # The fixture owns this shared real socket pool for the whole app.
                # Per-request clients must not close another concurrent request's IO.
                pass
        proof_transport = LocalTransport(verify=tls)
        provider = AuthorityDelegationProvider(settings, database.sessions, transport=proof_transport)
        app = create_app(settings, database=database, storage=MemoryStorage(), shared_pn_delegation=provider)
        app.state.shared_pn_transport = LocalTransport(verify=tls)
        try:
            uvicorn.run(app, host="127.0.0.1", port=28482, access_log=False)
        finally:
            httpx.HTTPTransport.close(proof_transport)
            httpx.HTTPTransport.close(app.state.shared_pn_transport)
            database.dispose()


if __name__ == "__main__":
    main()
