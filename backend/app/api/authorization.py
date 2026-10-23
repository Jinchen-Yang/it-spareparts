"""Typed control-plane adapters; all mutations delegate to canonical management."""
from fastapi import APIRouter, Body, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.authz_route import AuthorizationRoute
from app.authz import catalog, management, principal, service
from app.authz.errors import authz_error
from app.db import get_db
from app.models.dimensions import DimCustomer


def _no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/authz', tags=['authorization'], dependencies=[Depends(_no_store)], route_class=AuthorizationRoute)
# Compatibility for imports only; the core owns the version.
CATALOG_VERSION = catalog.CATALOG_VERSION


@router.get('/me')
def my_authorization(identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return service.my_snapshot(db, identity)


@router.get('/catalog')
def authorization_catalog(identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    management.actor(db, identity)
    return service.catalog_snapshot(db)


@router.get('/customers')
def search_customers(q: str = Query('', max_length=256), limit: int = Query(50, ge=1, le=100),
                     identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    management.actor(db, identity)
    name = func.coalesce(func.nullif(DimCustomer.name_normalized, ''), DimCustomer.name_raw)
    stmt = select(DimCustomer.id, name.label('name'), DimCustomer.name_raw.label('raw_name'))
    if q.strip():
        term = q.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        stmt = stmt.where(or_(DimCustomer.name_raw.ilike(f'%{term}%', escape='\\'), DimCustomer.name_normalized.ilike(f'%{term}%', escape='\\')))
    return {'items': [dict(row) for row in db.execute(stmt.order_by(name, DimCustomer.id).limit(limit)).mappings()]}


@router.get('/users/{username}/modules')
def get_modules(username: str, identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return management.read_account(db, identity, username)


@router.put('/users/{username}/modules')
def replace_modules(username: str, body: dict = Body(...), identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return management.replace_modules(db, identity, username, body)


@router.get('/users/{username}')
def get_authorization(username: str, identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    # S0 read adapter is safe: derives from canonical materialized state.
    result = management.read_account(db, identity, username)
    resource = result['resources'][catalog.SALES_RESOURCE]
    config = resource['configuration'] or management.denial(catalog.SALES_RESOURCE)
    return {'username': username, 'version': resource['version'], **config,
            'customer_ids': result['customer_assignments']['ids'], 'customers': result['customer_assignments']['customers']}


@router.put('/users/{username}')
def replace_authorization(username: str, identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    management.actor(db, identity)
    raise authz_error(409, 'AUTHZ_LEGACY_ENDPOINT_RETIRED', '旧授权写入口已退役，请使用 /modules')


@router.get('/templates/{code}/modules')
def get_template_modules(code: str, identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return management.read_template(db, identity, code)


@router.put('/templates/{code}/modules')
def replace_template_modules(code: str, body: dict = Body(...), identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return management.save_template(db, identity, body, code=code)


@router.post('/templates/{code}/sync')
def sync_template(code: str, body: dict = Body(...), identity: principal.VerifiedIdentity = Depends(principal.current_verified_identity), db: Session = Depends(get_db)):
    return management.bulk_modules(db, identity, body, sync_code=code)
