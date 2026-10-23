"""Generate PDP policies from the closed execution registry; --check detects drift."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.authz.catalog import RESOURCE_DEFINITIONS, SALES_RESOURCE, validate_catalog


def policy(resource, definition):
    grant = f'P.attr.grants.{resource}'
    rules = []
    for action in sorted(definition.actions):
        conditions = [f'{json.dumps(action)} in {grant}.actions']
        conditions += [f'{json.dumps(dep)} in {grant}.actions' for owner, dep in definition.action_dependencies if owner == action]
        for owner, alternatives in definition.action_any_dependencies:
            if owner == action:
                conditions.append('(' + ' || '.join(f'{json.dumps(dep)} in {grant}.actions' for dep in alternatives) + ')')
        conditions += [f'{json.dumps(dep)} in {grant}.field_permissions' for owner, dep in definition.action_field_dependencies if owner == action]
        if resource == SALES_RESOURCE:
            conditions.append(f'(R.attr.module_probe || {grant}.scope == "all" || ({grant}.scope == "assigned_customers" && R.attr.customer_id in P.attr.contexts.{resource}.customer_ids))')
        else:
            conditions.append(f'{grant}.scope == "all"')
        rules.append({'name': 'explicit-' + action.replace('_', '-'), 'actions': [action],
                      'effect': 'EFFECT_ALLOW', 'roles': ['registered_account'],
                      'condition': {'match': {'expr': ' && '.join(conditions)}},
                      'output': {'when': {'ruleActivated': '{"resource": ' + json.dumps(resource) +
                          ', "scope": ' + grant + '.scope, "version": ' + grant + '.version, "field_permissions": ' + grant + '.field_permissions}'}}})
    return {'apiVersion': 'api.cerbos.dev/v1', 'resourcePolicy': {'version': 'default', 'resource': resource, 'rules': rules}}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--check', action='store_true'); args = parser.parse_args()
    validate_catalog()
    directory = ROOT / 'deploy/cerbos/policies'
    expected = {f'{r}.yaml': json.dumps(policy(r, d), ensure_ascii=False, indent=2) + '\n' for r, d in RESOURCE_DEFINITIONS.items()}
    actual = {p.name for p in directory.glob('*.yaml')}
    if actual - expected.keys():
        raise SystemExit('Unregistered policy files: ' + ', '.join(sorted(actual - expected.keys())))
    for name, content in expected.items():
        path = directory / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                raise SystemExit('Policy drift: ' + name)
        else:
            path.write_text(content)
    print(f'{len(expected)} authorization policies verified' if args.check else f'{len(expected)} authorization policies generated')


if __name__ == '__main__': main()
