import { useEffect, useId, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { jevApi, type JevChoiceResult, type JevHorizon, type JevJudgment, type JevStatus } from '@panwatch/api/jev'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { Input } from '@panwatch/base-ui/components/ui/input'

const CHOICE_LABELS: Record<string, string> = {
  strong: '偏强', neutral: '中性', weak: '偏弱',
  low: '低风险', medium: '中等风险', high: '高风险',
  up: '上涨', flat: '横盘', down: '下跌',
  supported: '证据支持', partial: '部分支持', unsupported: '证据不支持',
  insufficient: '数据不足，暂不判断',
}

function formatTime(value: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请稍后重试'
}

function DecisionCard({ title, result }: { title: string; result: JevChoiceResult }) {
  return (
    <section className="rounded-lg border border-border/50 p-3 space-y-3" aria-label={title}>
      <div className="flex flex-wrap justify-between gap-2">
        <h4 className="text-[12px] font-medium text-muted-foreground">{title}</h4>
        <span className="text-[11px] text-muted-foreground">模型置信度 {percent(result.confidence)}</span>
      </div>
      <p className={`text-sm font-semibold ${result.choice === 'insufficient' ? 'text-amber-600 dark:text-amber-400' : 'text-foreground'}`}>
        {CHOICE_LABELS[result.choice] || result.choice}
      </p>
      <div className="space-y-1.5">
        <p className="text-[11px] text-muted-foreground">选项概率</p>
        <dl className="space-y-1.5">
          {Object.entries(result.probabilities).map(([choice, probability]) => (
            <div key={choice} className="flex items-center justify-between gap-3 text-[11px]">
              <dt className={choice === result.choice ? 'text-foreground' : 'text-muted-foreground'}>{CHOICE_LABELS[choice] || choice}</dt>
              <dd className="font-mono text-muted-foreground">{percent(probability)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}

function JudgmentResult({ result }: { result: JevJudgment }) {
  return (
    <article className="space-y-3" aria-label="Jev 判断结果">
      <div className="rounded-lg bg-accent/40 p-3 text-[12px] space-y-1.5">
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <span>数据截至：{formatTime(result.as_of)}</span>
          <span>参考收盘价：<span className="font-mono">{result.reference_price.toFixed(3)}</span></span>
        </div>
        <div>预测周期：{result.horizon} 个交易日 · 横盘区间：±{result.flat_threshold_pct}%</div>
        <div className="text-muted-foreground break-words">生成时间：{formatTime(result.created_at)} · 模型：{result.model}</div>
      </div>
      {result.warnings.length > 0 && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-[12px]" role="note">
          <p className="font-medium mb-1">数据提示</p>
          <ul className="list-disc pl-4 space-y-1">
            {result.warnings.map((warning, index) => <li key={`${index}:${warning}`}>{warning}</li>)}
          </ul>
        </div>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <DecisionCard title="当前趋势" result={result.decisions.trend} />
        <DecisionCard title="当前风险" result={result.decisions.risk} />
        <DecisionCard title={`未来 ${result.horizon} 个交易日方向`} result={result.decisions.direction} />
        {result.decisions.review ? (
          <DecisionCard title="AI 分析证据复核" result={result.decisions.review} />
        ) : (
          <section className="rounded-lg border border-border/50 p-3 space-y-3" aria-label="AI 分析证据复核">
            <h4 className="text-[12px] font-medium text-muted-foreground">AI 分析证据复核</h4>
            <p className="text-[12px] text-muted-foreground">暂无同市场、同标的的可复核报告。</p>
          </section>
        )}
      </div>
      {result.review_source && (
        <div className="text-[12px] text-muted-foreground break-words">
          复核来源：{result.review_source.title}（{result.review_source.agent_name} · {formatTime(result.review_source.created_at)}）
        </div>
      )}
      <p className="text-[11px] text-muted-foreground leading-relaxed">
        方向按数据截至日收盘到第 {result.horizon} 个后续交易日收盘的累计收益判断；收益超过 +{result.flat_threshold_pct}% 为上涨，低于 -{result.flat_threshold_pct}% 为下跌，其余为横盘。选项概率与模型置信度不代表实际交易胜率。
      </p>
    </article>
  )
}

/** Changing the stock remounts the state so an old response cannot appear for a new symbol. */
export default function JevJudgmentPanel(props: { symbol: string; market: string }) {
  return <JevJudgmentPanelContent key={`${props.market}:${props.symbol}`} {...props} />
}

function JevJudgmentPanelContent({ symbol, market }: { symbol: string; market: string }) {
  const thresholdId = useId()
  const alive = useRef(true)
  const submitting = useRef(false)
  const [status, setStatus] = useState<JevStatus | null>(null)
  const [records, setRecords] = useState<JevJudgment[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [loadErrors, setLoadErrors] = useState<string[]>([])
  const [runError, setRunError] = useState('')
  const [reload, setReload] = useState(0)
  const [horizon, setHorizon] = useState<JevHorizon>(1)
  const [threshold, setThreshold] = useState('1.0')
  const thresholdNumber = Number(threshold)
  const thresholdValid = threshold.trim() !== '' && Number.isFinite(thresholdNumber) && thresholdNumber >= 0.1 && thresholdNumber <= 10
  const selected = records.find(record => record.id === selectedId) || records[0]

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setLoadErrors([])
    Promise.allSettled([jevApi.status(), jevApi.history(symbol, market)]).then(([statusResult, historyResult]) => {
      if (cancelled) return
      const errors: string[] = []
      if (statusResult.status === 'fulfilled') setStatus(statusResult.value)
      else {
        setStatus(null)
        errors.push(`配置状态读取失败：${message(statusResult.reason)}`)
      }
      if (historyResult.status === 'fulfilled') setRecords(historyResult.value)
      else errors.push(`历史记录读取失败：${message(historyResult.reason)}`)
      setLoadErrors(errors)
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [symbol, market, reload])

  async function runJudgment() {
    if (submitting.current || !status?.configured || !thresholdValid || loading) return
    submitting.current = true
    setRunning(true)
    setRunError('')
    try {
      const result = await jevApi.judge({ symbol, market, horizon, flat_threshold_pct: thresholdNumber })
      if (!alive.current) return
      setRecords(previous => [result, ...previous.filter(record => record.id !== result.id)].slice(0, 10))
      setSelectedId(result.id)
    } catch (error) {
      if (alive.current) setRunError(message(error))
    } finally {
      submitting.current = false
      if (alive.current) setRunning(false)
    }
  }

  return (
    <div className="space-y-4" aria-label="Jev 判断" aria-busy={loading || running}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Jev 判断</h3>
          <p className="mt-1 text-[12px] text-muted-foreground">趋势、风险、未来方向与 AI 分析证据复核。点击后生成并保存一次判断。</p>
        </div>
        <Button type="button" variant="outline" size="sm" onClick={() => setReload(value => value + 1)} disabled={loading || running}>
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />刷新记录
        </Button>
      </div>

      {loading && <p role="status" className="text-[12px] text-muted-foreground">正在读取 Jev 配置与历史记录...</p>}
      {loadErrors.length > 0 && <div role="alert" className="text-[12px] text-destructive space-y-1">{loadErrors.map(error => <p key={error}>{error}</p>)}</div>}
      {status && !status.configured && (
        <div role="note" className="rounded-lg border border-border/50 p-3 text-[12px] text-muted-foreground">
          尚未配置 Jev。请在服务端设置 <code>TYPESAFE_API_KEY</code>，可选设置 <code>JEV_MODEL</code>，然后刷新记录。
        </div>
      )}

      <div className="rounded-lg border border-border/50 p-3 space-y-3">
        <div className="flex flex-wrap items-end gap-4">
          <fieldset disabled={running} className="space-y-1.5">
            <legend className="text-[12px] text-muted-foreground mb-1.5">预测周期</legend>
            <div className="flex gap-1.5">
              {([1, 3, 5] as const).map(value => (
                <Button key={value} type="button" size="sm" variant={horizon === value ? 'secondary' : 'ghost'} aria-pressed={horizon === value} onClick={() => setHorizon(value)}>
                  {value} 个交易日
                </Button>
              ))}
            </div>
          </fieldset>
          <div className="space-y-1.5">
            <label htmlFor={thresholdId} className="text-[12px] text-muted-foreground">横盘阈值（±%）</label>
            <Input id={thresholdId} type="number" min="0.1" max="10" step="0.1" value={threshold} onChange={event => setThreshold(event.target.value)} disabled={running} aria-invalid={!thresholdValid} aria-describedby={!thresholdValid ? `${thresholdId}-error` : undefined} className="h-8 w-32" />
          </div>
          <Button type="button" size="sm" onClick={runJudgment} disabled={loading || running || !status?.configured || !thresholdValid}>
            {running ? '正在判断...' : '生成 Jev 判断'}
          </Button>
        </div>
        {!thresholdValid && <p id={`${thresholdId}-error`} className="text-[12px] text-destructive">横盘阈值须为 0.1% 到 10%。</p>}
        <p className="text-[11px] text-muted-foreground">复核会使用该市场、该标的最新可用的 AI 报告。{status?.model ? `当前模型：${status.model}` : ''}</p>
        {running && <p role="status" className="text-[12px] text-muted-foreground">Jev 正在处理，完成后会显示并保存结果。</p>}
        {runError && <p role="alert" className="text-[12px] text-destructive">判断失败：{runError}</p>}
      </div>

      {selected ? <JudgmentResult result={selected} /> : !loading && loadErrors.length === 0 && <p className="text-[12px] text-muted-foreground py-4 text-center">暂无判断记录。</p>}

      {records.length > 0 && (
        <section className="space-y-2" aria-label="Jev 历史记录">
          <h4 className="text-[12px] font-medium text-muted-foreground">最近判断（{records.length}）</h4>
          <div className="space-y-1">
            {records.map(record => (
              <button key={record.id} type="button" aria-pressed={selected?.id === record.id} onClick={() => setSelectedId(record.id)} className={`w-full rounded-lg border p-2.5 text-left text-[12px] flex flex-wrap justify-between gap-2 ${selected?.id === record.id ? 'border-primary/40 bg-primary/5' : 'border-border/40 hover:bg-accent/40'}`}>
                <span>{formatTime(record.created_at)} · {record.horizon} 个交易日 · ±{record.flat_threshold_pct}%</span>
                <span>{CHOICE_LABELS[record.decisions.direction.choice] || record.decisions.direction.choice}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
