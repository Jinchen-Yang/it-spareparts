import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '../../../test/antdRender';
import type { AccountModules, AuthzCatalog, AuthorizationPreview } from '../../../api/authz';
import accountCases from '../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/02-account-modules.json';
import ModuleAccountDrawer from '../ModuleAccountDrawer';
import AuthorizationDiff from '../AuthorizationDiff';
import { useAuthorizationTask } from '../useAuthorizationTask';
import catalogCases from '../../../../../docs/reviews/authz-rewrite-20261023/s1-wire-fixtures/01-catalog-me.json';

const apiMocks = vi.hoisted(() => ({ getAccountModules: vi.fn(), authzCommand: vi.fn() }));
vi.mock('../../../api/authz', async importOriginal => ({
  ...(await importOriginal<typeof import('../../../api/authz')>()), ...apiMocks,
}));

function pending<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function TaskProbe({ job, success }: { job: () => Promise<unknown>; success: (value: unknown) => void }) {
  const task = useAuthorizationTask('independent-target');
  return <><button onClick={() => void task.run(job, success)}>开始写入</button>
    <button onClick={task.invalidate}>改变草稿</button><div role="status">{task.error}</div></>;
}

beforeEach(() => { localStorage.clear(); localStorage.setItem('token', 'owner-A'); });
afterEach(cleanup);

describe('S1 independent session and confirmation boundaries', () => {
  it.each(['close', 'newer_generation'])('committed self revocation survives %s while the same login owns the result', async kind => {
    const result = pending<unknown>(), success = vi.fn(), job = vi.fn(() => result.promise);
    const view = render(<TaskProbe job={job} success={success} />);
    fireEvent.click(screen.getByRole('button', { name: '开始写入' }));
    if (kind === 'close') view.unmount();
    else fireEvent.click(screen.getByRole('button', { name: '改变草稿' }));
    await act(async () => result.resolve({ committed: true, actor_session_revoked: true }));
    expect(localStorage.getItem('token')).toBeNull();
    expect(success).not.toHaveBeenCalled();
    expect(job).toHaveBeenCalledTimes(1);
  });

  it('late self revocation from login A cannot clear login B', async () => {
    const result = pending<unknown>(), success = vi.fn();
    const view = render(<TaskProbe job={() => result.promise} success={success} />);
    fireEvent.click(screen.getByRole('button', { name: '开始写入' }));
    localStorage.setItem('token', 'owner-B');
    view.unmount();
    await act(async () => result.resolve({ committed: true, actor_session_revoked: true }));
    expect(localStorage.getItem('token')).toBe('owner-B');
    expect(success).not.toHaveBeenCalled();
  });

  it('closed target does not consume a late ordinary success', async () => {
    const result = pending<unknown>(), success = vi.fn();
    const view = render(<TaskProbe job={() => result.promise} success={success} />);
    fireEvent.click(screen.getByRole('button', { name: '开始写入' }));
    view.unmount();
    await act(async () => result.resolve({ committed: true, actor_session_revoked: false }));
    expect(localStorage.getItem('token')).toBe('owner-A');
    expect(success).not.toHaveBeenCalled();
  });

  it('409 remains an error and never becomes an automatic replay', async () => {
    const result = pending<unknown>(), success = vi.fn(), job = vi.fn(() => result.promise);
    render(<TaskProbe job={job} success={success} />);
    fireEvent.click(screen.getByRole('button', { name: '开始写入' }));
    await act(async () => result.reject({ response: { status: 409, data: { detail: {
      code: 'AUTHZ_PREVIEW_CONFLICT', message: '预览之后权限已改变', committed: false,
      conflicts: [], field_errors: [],
    } } } }));
    expect(screen.getByRole('status')).toHaveTextContent('预览之后权限已改变');
    expect(job).toHaveBeenCalledTimes(1);
    expect(success).not.toHaveBeenCalled();
    expect(localStorage.getItem('token')).toBe('owner-A');
  });

  it('admin creation confirmation shows the canonical identity and authorization intent without a password', () => {
    const catalog = catalogCases.cases[0].response.json as unknown as AuthzCatalog;
    const preview: AuthorizationPreview = {
      schema_version: 1, authorization_mode: 'unified_modules', catalog_version: catalog.catalog_version,
      dry_run: true, committed: false, fingerprint: 'a'.repeat(64),
      command: { schema_version: 1, catalog_version: catalog.catalog_version,
        profile: { username: 'review-new-admin', display_name: '可审查的新账号' },
        security_change: { role: 'admin', salesperson_name: '明确的销售映射' },
        authorization: { kind: 'apply_template', template_code: 'frozen-template', expected_template_version: 7 },
        customer_assignments: { ids: [51, 52] },
      },
      preview: { username: 'review-new-admin', username_must_be_absent: true, role_after: 'admin',
        authorization_kind: 'apply_template', authorization_epoch_after: 1 },
    };
    render(<AuthorizationDiff preview={preview} catalog={catalog} />);
    expect(screen.getByText('review-new-admin')).toBeInTheDocument();
    expect(screen.getByText('管理员')).toBeInTheDocument();
    expect(screen.getByText('明确的销售映射')).toBeInTheDocument();
    expect(screen.getByText('模板 frozen-template v7')).toBeInTheDocument();
    expect(screen.getByText('51、52')).toBeInTheDocument();
    expect(screen.queryByText('{}')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });
});

it('no-template personal override can be cleared with the exact epoch and resource CAS', async () => {
  const catalog = catalogCases.cases[0].response.json as unknown as AuthzCatalog;
  const account = structuredClone(accountCases.cases[0].response.json) as unknown as AccountModules;
  account.template = null;
  for (const resource of Object.values(account.resources)) resource.template_snapshot = null;
  expect(account.resources.sales_analysis.personal_override.present).toBe(true);
  apiMocks.getAccountModules.mockResolvedValue(account);
  apiMocks.authzCommand.mockResolvedValue({ committed: true, actor_session_revoked: false });
  const saved = vi.fn();
  render(<ModuleAccountDrawer username="alice" catalog={catalog} onClose={vi.fn()} onSaved={saved} />);
  const clear = await screen.findByRole('button', { name: '清除个人调整并撤销模块' });
  expect(clear).toBeEnabled();
  fireEvent.click(clear);
  expect(screen.getByText('没有保存的模板配置；清除个人调整后该模块将拒绝访问。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '保存配置与归属' }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(apiMocks.authzCommand).toHaveBeenCalledExactlyOnceWith('/authz/users/alice/modules', 'put', {
    schema_version: 1, catalog_version: catalog.catalog_version,
    expected_epoch: account.authorization_epoch,
    updates: [{ resource_id: 'sales_analysis', expected_version: account.resources.sales_analysis.version,
      operation: 'clear_override' }],
  }, 'owner-A');
});
