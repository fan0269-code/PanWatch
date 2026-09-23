import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { jevApi, type JevJudgment } from '@panwatch/api/jev'
import JevJudgmentPanel from '@panwatch/biz-ui/components/jev-judgment-panel'

vi.mock('@panwatch/api/jev', () => ({
  jevApi: { status: vi.fn(), history: vi.fn(), judge: vi.fn() },
}))

function judgment(overrides: Partial<JevJudgment> = {}): JevJudgment {
  return {
    id: 1,
    symbol: '600519',
    market: 'CN',
    created_at: '2026-09-23T10:00:00Z',
    as_of: '2026-09-22',
    reference_price: 1400.5,
    horizon: 1,
    flat_threshold_pct: 1,
    model: 'jev-test',
    decisions: {
      trend: { choice: 'strong', confidence: 0.7, probabilities: { strong: 0.7, neutral: 0.15, weak: 0.1, insufficient: 0.05 } },
      risk: { choice: 'medium', confidence: 0.6, probabilities: { low: 0.2, medium: 0.6, high: 0.15, insufficient: 0.05 } },
      direction: { choice: 'flat', confidence: 0.5, probabilities: { up: 0.3, flat: 0.5, down: 0.15, insufficient: 0.05 } },
      review: null,
    },
    review_source: null,
    warnings: ['新闻数据暂缺'],
    ...overrides,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(resolvePromise => { resolve = resolvePromise })
  return { promise, resolve }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(jevApi.status).mockResolvedValue({ configured: true, model: 'jev-test' })
  vi.mocked(jevApi.history).mockResolvedValue([])
  vi.mocked(jevApi.judge).mockResolvedValue(judgment())
})

afterEach(cleanup)

describe('JevJudgmentPanel', () => {
  it('loads only configuration and history on mount and refresh', async () => {
    const history = deferred<JevJudgment[]>()
    vi.mocked(jevApi.history).mockReturnValueOnce(history.promise)
    const user = userEvent.setup()
    render(<JevJudgmentPanel symbol="600519" market="CN" />)

    expect(screen.getByRole('status').textContent).toContain('正在读取')
    expect((screen.getByRole('button', { name: '生成 Jev 判断' }) as HTMLButtonElement).disabled).toBe(true)
    expect(jevApi.history).toHaveBeenCalledWith('600519', 'CN')
    expect(jevApi.judge).not.toHaveBeenCalled()

    await act(async () => { history.resolve([]) })
    expect(await screen.findByText('暂无判断记录。')).toBeTruthy()
    await user.click(screen.getByRole('button', { name: '刷新记录' }))
    await waitFor(() => expect(jevApi.history).toHaveBeenCalledTimes(2))
    expect(jevApi.judge).not.toHaveBeenCalled()
  })

  it('disables generation when unconfigured and does not request credentials in the browser', async () => {
    vi.mocked(jevApi.status).mockResolvedValue({ configured: false, model: 'jev-test' })
    render(<JevJudgmentPanel symbol="600519" market="CN" />)

    expect(await screen.findByText('TYPESAFE_API_KEY')).toBeTruthy()
    expect(screen.getByText('JEV_MODEL')).toBeTruthy()
    expect(screen.queryByRole('textbox')).toBeNull()
    expect((screen.getByRole('button', { name: '生成 Jev 判断' }) as HTMLButtonElement).disabled).toBe(true)
    expect(jevApi.judge).not.toHaveBeenCalled()
  })

  it('reports read failures and can retry without creating judgments', async () => {
    vi.mocked(jevApi.status).mockRejectedValueOnce(new Error('服务不可用'))
    vi.mocked(jevApi.history).mockRejectedValueOnce(new Error('读取超时'))
    const user = userEvent.setup()
    render(<JevJudgmentPanel symbol="600519" market="CN" />)

    expect((await screen.findByRole('alert')).textContent).toContain('配置状态读取失败：服务不可用')
    expect(screen.getByRole('alert').textContent).toContain('历史记录读取失败：读取超时')
    expect((screen.getByRole('button', { name: '生成 Jev 判断' }) as HTMLButtonElement).disabled).toBe(true)
    await user.click(screen.getByRole('button', { name: '刷新记录' }))
    expect(await screen.findByText('暂无判断记录。')).toBeTruthy()
    expect(jevApi.judge).not.toHaveBeenCalled()
  })

  it('shows data provenance, probabilities, missing reports and selected history', async () => {
    const older = judgment({
      id: 2, horizon: 3, as_of: '2026-09-18', created_at: '2026-09-19T10:00:00Z', warnings: [],
      review_source: { id: 20, title: '盘后复核报告', agent_name: 'daily_report', created_at: '2026-09-18T10:00:00Z' },
      decisions: { ...judgment().decisions, review: { choice: 'partial', confidence: 0.8, probabilities: { supported: 0.1, partial: 0.8, unsupported: 0.05, insufficient: 0.05 } } },
    })
    vi.mocked(jevApi.history).mockResolvedValue([judgment(), older])
    const user = userEvent.setup()
    render(<JevJudgmentPanel symbol="600519" market="CN" />)

    expect(await screen.findByText('数据截至：2026-09-22')).toBeTruthy()
    expect(screen.getByText('1400.500')).toBeTruthy()
    expect(screen.getByText('新闻数据暂缺')).toBeTruthy()
    expect(screen.getByText('暂无同市场、同标的的可复核报告。')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '当前趋势' })).getByText('70.0%')).toBeTruthy()
    expect(screen.getByText(/选项概率与模型置信度不代表实际交易胜率/)).toBeTruthy()

    await user.click(screen.getByRole('button', { name: /3 个交易日 · ±1%/ }))
    expect(screen.getByText('数据截至：2026-09-18')).toBeTruthy()
    expect(screen.getByText(/复核来源：盘后复核报告/)).toBeTruthy()
    expect(screen.getByRole('region', { name: 'AI 分析证据复核' }).textContent).toContain('部分支持')
    expect(screen.queryByText('新闻数据暂缺')).toBeNull()
    expect(jevApi.judge).not.toHaveBeenCalled()
  })

  it('validates controls and generates only on explicit click with selected horizon and threshold', async () => {
    const result = deferred<JevJudgment>()
    vi.mocked(jevApi.judge).mockReturnValue(result.promise)
    const user = userEvent.setup()
    render(<JevJudgmentPanel symbol="600519" market="CN" />)
    await screen.findByText('暂无判断记录。')

    await user.click(screen.getByRole('button', { name: '5 个交易日', exact: true }))
    const threshold = screen.getByRole('spinbutton', { name: '横盘阈值（±%）' })
    await user.clear(threshold)
    await user.type(threshold, '11')
    expect((screen.getByRole('button', { name: '生成 Jev 判断' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('横盘阈值须为 0.1% 到 10%。')).toBeTruthy()
    await user.clear(threshold)
    await user.type(threshold, '2.5')
    expect(jevApi.judge).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '生成 Jev 判断' }))
    expect(jevApi.judge).toHaveBeenCalledExactlyOnceWith({ symbol: '600519', market: 'CN', horizon: 5, flat_threshold_pct: 2.5 })
    expect((screen.getByRole('button', { name: '正在判断...' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('Jev 正在处理')

    await act(async () => { result.resolve(judgment({ horizon: 5, flat_threshold_pct: 2.5 })) })
    expect(await screen.findByText('预测周期：5 个交易日 · 横盘区间：±2.5%')).toBeTruthy()
    expect(screen.getByText('最近判断（1）')).toBeTruthy()
  })

  it('retains historical results on generation failure and allows retry', async () => {
    vi.mocked(jevApi.history).mockResolvedValue([judgment()])
    vi.mocked(jevApi.judge).mockRejectedValueOnce(new Error('Jev 请求超时'))
    const user = userEvent.setup()
    render(<JevJudgmentPanel symbol="600519" market="CN" />)
    await screen.findByText('数据截至：2026-09-22')

    await user.click(screen.getByRole('button', { name: '生成 Jev 判断' }))
    expect((await screen.findByRole('alert')).textContent).toContain('判断失败：Jev 请求超时')
    expect(screen.getByText('数据截至：2026-09-22')).toBeTruthy()
    expect((screen.getByRole('button', { name: '生成 Jev 判断' }) as HTMLButtonElement).disabled).toBe(false)
    await user.click(screen.getByRole('button', { name: '生成 Jev 判断' }))
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(jevApi.judge).toHaveBeenCalledTimes(2)
  })

  it('ignores a previous stock response after switching markets or symbols', async () => {
    const previous = deferred<JevJudgment[]>()
    vi.mocked(jevApi.history).mockReturnValueOnce(previous.promise)
    const view = render(<JevJudgmentPanel symbol="600519" market="CN" />)
    view.rerender(<JevJudgmentPanel symbol="AAPL" market="US" />)
    await screen.findByText('暂无判断记录。')
    await act(async () => { previous.resolve([judgment()]) })

    expect(screen.queryByText('数据截至：2026-09-22')).toBeNull()
    expect(jevApi.history).toHaveBeenLastCalledWith('AAPL', 'US')
    expect(jevApi.judge).not.toHaveBeenCalled()
  })
})
