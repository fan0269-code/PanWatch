import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { Children, isValidElement, type ReactNode } from 'react'
import { fetchAPI, type NotifyChannel } from '@panwatch/api'
import SettingsPage from '@/pages/Settings'

vi.mock('@panwatch/api', () => ({ fetchAPI: vi.fn() }))
vi.mock('@/hooks/use-avatar', () => ({ useAvatar: () => '', saveAvatar: vi.fn(), fileToAvatarDataUrl: vi.fn() }))
vi.mock('@/components/PatSection', () => ({ default: () => null }))
// Keep these tests focused on channel form behavior; Radix positioning belongs to browser tests.
vi.mock('@panwatch/base-ui/components/ui/select', () => ({
  Select: ({ value, onValueChange, children }: { value: string; onValueChange: (value: string) => void; children: ReactNode }) => {
    const trigger = Children.toArray(children).find(child => isValidElement<{ id?: string }>(child) && child.props.id)
    const id = isValidElement<{ id?: string }>(trigger) ? trigger.props.id : undefined
    return <select id={id} aria-label={id ? undefined : '类型'} value={value} onChange={event => onValueChange(event.target.value)}>{children}</select>
  },
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: ReactNode }) => <>{children}</>,
  SelectItem: ({ value, children }: { value: string; children: ReactNode }) => <option value={value}>{children}</option>,
}))

let channels: NotifyChannel[] = []
const webhook = 'https://open.feishu.cn/open-apis/bot/v2/hook/test-fixture-token'

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn()
  HTMLElement.prototype.hasPointerCapture = vi.fn(() => false)
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLElement.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  channels = []
  vi.mocked(fetchAPI).mockReset()
  vi.mocked(fetchAPI).mockImplementation(async (path, options) => {
    if (options?.method) return { id: 1 }
    if (path === '/channels') return channels
    if (path === '/settings/version') return { version: 'dev' }
    if (path === '/agents/health') return { timezone: 'Asia/Shanghai', summary: { next_24h_count: 0, recent_failed_count: 0 } }
    if (path.startsWith('/feedback/stats')) return { total: 0, useful: 0, useless: 0, useful_rate: 0, by_day: [], by_agent: [] }
    return []
  })
})

afterEach(cleanup)

async function openFeishuForm() {
  const user = userEvent.setup({ pointerEventsCheck: 0 })
  render(<SettingsPage />)
  await user.click(await screen.findByRole('button', { name: '添加通知渠道' }))
  const dialog = within(screen.getByRole('dialog'))
  await user.selectOptions(dialog.getByRole('combobox', { name: '类型' }), 'feishu')
  fireEvent.change(dialog.getByLabelText('名称', { exact: true }), { target: { value: '国内飞书' } })
  return { user, dialog }
}

describe('notification channel settings', () => {
  it('creates an application bot with a hidden secret and default group recipient', async () => {
    const { user, dialog } = await openFeishuForm()
    await user.selectOptions(dialog.getByRole('combobox', { name: '类型', exact: true }), 'feishu_app')
    const save = dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement
    const appId = dialog.getByLabelText('App ID')
    const appSecret = dialog.getByLabelText('App Secret') as HTMLInputElement
    const recipientType = dialog.getByRole('combobox', { name: '接收对象类型' }) as HTMLSelectElement
    const recipient = dialog.getByLabelText('接收对象 ID')
    expect(recipientType.value).toBe('chat_id')
    expect([...recipientType.options].map(option => option.value)).toEqual(['chat_id', 'open_id', 'user_id'])
    expect(appSecret.type).toBe('password')
    expect(save.disabled).toBe(true)
    expect(dialog.getByText(/先把应用机器人加入目标群/)).toBeTruthy()
    expect(dialog.getByText(/应用需要开通发送消息权限/)).toBeTruthy()
    fireEvent.change(appId, { target: { value: ' cli_frontend_fixture ' } })
    fireEvent.change(appSecret, { target: { value: ' fixture-app-secret ' } })
    expect(save.disabled).toBe(true)
    fireEvent.change(recipient, { target: { value: ' oc_fixture_chat ' } })
    expect(save.disabled).toBe(false)
    await user.click(save)
    await waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/channels', {
      method: 'POST',
      body: JSON.stringify({ name: '国内飞书', type: 'feishu_app', config: {
        app_id: 'cli_frontend_fixture', app_secret: 'fixture-app-secret', receive_id_type: 'chat_id', receive_id: 'oc_fixture_chat',
      } }),
    }))
    expect(vi.mocked(fetchAPI).mock.calls.some(([path]) => path.endsWith('/test'))).toBe(false)
  })

  it('requires application credentials and saves a selected user recipient type', async () => {
    const { user, dialog } = await openFeishuForm()
    await user.selectOptions(dialog.getByRole('combobox', { name: '类型', exact: true }), 'feishu_app')
    const save = dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement
    fireEvent.change(dialog.getByLabelText('App ID'), { target: { value: 'cli_frontend_fixture' } })
    fireEvent.change(dialog.getByLabelText('接收对象 ID'), { target: { value: 'oc_fixture_chat' } })
    fireEvent.change(dialog.getByLabelText('App Secret'), { target: { value: '   ' } })
    expect(save.disabled).toBe(true)
    fireEvent.change(dialog.getByLabelText('App Secret'), { target: { value: 'fixture-app-secret' } })
    const recipientType = dialog.getByRole('combobox', { name: '接收对象类型' })
    await user.selectOptions(recipientType, 'open_id')
    expect(save.disabled).toBe(true)
    expect(dialog.getByRole('alert').textContent).toContain('用户 Open ID 需以 ou_ 开头')
    fireEvent.change(dialog.getByLabelText('接收对象 ID'), { target: { value: 'ou_' } })
    expect(save.disabled).toBe(true)
    fireEvent.change(dialog.getByLabelText('接收对象 ID'), { target: { value: 'ou_fixture_user' } })
    expect(save.disabled).toBe(false)
    await user.selectOptions(recipientType, 'user_id')
    fireEvent.change(dialog.getByLabelText('接收对象 ID'), { target: { value: ' fixture-user-id ' } })
    await user.click(save)
    await waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/channels', {
      method: 'POST',
      body: JSON.stringify({ name: '国内飞书', type: 'feishu_app', config: {
        app_id: 'cli_frontend_fixture', app_secret: 'fixture-app-secret', receive_id_type: 'user_id', receive_id: 'fixture-user-id',
      } }),
    }))
  })

  it('validates application IDs, secrets and recipient IDs before saving', async () => {
    const { user, dialog } = await openFeishuForm()
    await user.selectOptions(dialog.getByRole('combobox', { name: '类型', exact: true }), 'feishu_app')
    const save = dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement
    const appId = dialog.getByLabelText('App ID')
    const appSecret = dialog.getByLabelText('App Secret')
    const recipient = dialog.getByLabelText('接收对象 ID')
    fireEvent.change(appId, { target: { value: 'cli_frontend_fixture' } })
    fireEvent.change(appSecret, { target: { value: 'fixture-app-secret' } })
    fireEvent.change(recipient, { target: { value: 'oc_fixture_chat' } })
    expect(save.disabled).toBe(false)
    for (const invalid of ['not-an-app-id', `cli_${'a'.repeat(125)}`, 'cli_has space']) {
      fireEvent.change(appId, { target: { value: invalid } })
      expect(save.disabled).toBe(true)
      expect(dialog.getByRole('alert').textContent).toContain('App ID 需以 cli_ 开头')
    }
    fireEvent.change(appId, { target: { value: 'cli_frontend_fixture' } })
    for (const invalid of ['a'.repeat(257), 'has space', '密钥']) {
      fireEvent.change(appSecret, { target: { value: invalid } })
      expect(save.disabled).toBe(true)
      expect(dialog.getByRole('alert').textContent).toContain('App Secret 需为')
    }
    fireEvent.change(appSecret, { target: { value: 'fixture-app-secret' } })
    for (const invalid of ['wrong-prefix', 'oc_', 'oc_has space', `oc_${'a'.repeat(126)}`]) {
      fireEvent.change(recipient, { target: { value: invalid } })
      expect(save.disabled).toBe(true)
    }
    expect(vi.mocked(fetchAPI).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('creates domestic Feishu with a full webhook and optional hidden signing secret', async () => {
    const { user, dialog } = await openFeishuForm()
    const save = dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement
    const url = dialog.getByLabelText('Webhook 地址') as HTMLInputElement
    const secret = dialog.getByLabelText('签名密钥 (选填)') as HTMLInputElement
    expect(save.disabled).toBe(true)
    expect(secret.type).toBe('password')
    expect(url.type).toBe('password')
    expect(dialog.getByText(/若机器人启用了签名校验/)).toBeTruthy()

    fireEvent.change(url, { target: { value: ` ${webhook} ` } })
    expect(save.disabled).toBe(false)
    fireEvent.change(secret, { target: { value: ' test-signing-secret ' } })
    await user.click(save)
    await waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/channels', {
      method: 'POST',
      body: JSON.stringify({ name: '国内飞书', type: 'feishu', config: { webhook_url: webhook, secret: 'test-signing-secret' } }),
    }))
    expect(vi.mocked(fetchAPI).mock.calls.some(([path]) => path.endsWith('/test'))).toBe(false)
  })

  it('rejects incomplete, international and modified webhook URLs while allowing no signature', async () => {
    const { user, dialog } = await openFeishuForm()
    const url = dialog.getByLabelText('Webhook 地址')
    const save = dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement
    for (const invalid of [
      'only-a-token',
      webhook.replace('open.feishu.cn', 'open.larksuite.com'),
      webhook.replace('https:', 'http:'),
      `${webhook}?foo=bar`,
      `${webhook}#fragment`,
      `${webhook}/`,
      webhook.replace('open.feishu.cn', 'open.feishu.cn:443'),
      webhook.replace('open.feishu.cn', 'name@open.feishu.cn'),
    ]) {
      fireEvent.change(url, { target: { value: invalid } })
      expect(save.disabled).toBe(true)
      expect(dialog.getByRole('alert').textContent).toContain('完整的国内飞书 Webhook 地址')
    }
    fireEvent.change(url, { target: { value: webhook } })
    expect(save.disabled).toBe(false)
    await user.click(save)
    await waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/channels', {
      method: 'POST',
      body: JSON.stringify({ name: '国内飞书', type: 'feishu', config: { webhook_url: webhook, secret: '' } }),
    }))
  })

  it('rejects oversized signing secrets and preserves existing international Lark configuration', async () => {
    channels = [{ id: 7, name: '已有国际群', type: 'lark', config: { webhook_token: 'existing-token' }, enabled: true, is_default: true }]
    const user = userEvent.setup({ pointerEventsCheck: 0 })
    render(<SettingsPage />)
    expect(await screen.findByText('国际 Lark 机器人')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '编辑已有国际群' }))
    let dialog = within(screen.getByRole('dialog'))
    expect((dialog.getByLabelText('Webhook Token') as HTMLInputElement).value).toBe('existing-token')
    await user.click(dialog.getByRole('button', { name: '保存', exact: true }))
    await waitFor(() => expect(fetchAPI).toHaveBeenCalledWith('/channels/7', {
      method: 'PUT', body: JSON.stringify({ name: '已有国际群', type: 'lark', config: { webhook_token: 'existing-token' } }),
    }))

    await user.click(screen.getByRole('button', { name: '添加通知渠道' }))
    dialog = within(screen.getByRole('dialog'))
    await user.selectOptions(dialog.getByRole('combobox', { name: '类型' }), 'feishu')
    fireEvent.change(dialog.getByLabelText('名称', { exact: true }), { target: { value: '国内群' } })
    fireEvent.change(dialog.getByLabelText('Webhook 地址'), { target: { value: webhook } })
    fireEvent.change(dialog.getByLabelText('签名密钥 (选填)'), { target: { value: 'a'.repeat(257) } })
    expect((dialog.getByRole('button', { name: '创建', exact: true }) as HTMLButtonElement).disabled).toBe(true)
    expect(dialog.getByRole('alert').textContent).toContain('签名密钥不能超过 256 个字符')
  })
})
