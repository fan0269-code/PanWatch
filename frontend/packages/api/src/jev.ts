import { fetchAPI } from './client'

export type JevHorizon = 1 | 3 | 5

export interface JevStatus {
  configured: boolean
  model: string
}

export interface JevChoiceResult {
  choice: string
  probabilities: Record<string, number>
  confidence: number
}

export interface JevJudgment {
  id: number
  symbol: string
  market: string
  created_at: string
  as_of: string
  reference_price: number
  horizon: JevHorizon
  flat_threshold_pct: number
  model: string
  decisions: {
    trend: JevChoiceResult
    risk: JevChoiceResult
    direction: JevChoiceResult
    review: JevChoiceResult | null
  }
  review_source: {
    id: number
    title: string
    agent_name: string
    created_at: string
  } | null
  warnings: string[]
}

export interface JevJudgmentRequest {
  symbol: string
  market: string
  horizon: JevHorizon
  flat_threshold_pct: number
  analysis_id?: number
}

export const jevApi = {
  status: () => fetchAPI<JevStatus>('/jev/status'),

  history: (symbol: string, market: string, limit = 10) => {
    const query = new URLSearchParams({ symbol, market, limit: String(limit) })
    return fetchAPI<JevJudgment[]>(`/jev/judgments?${query.toString()}`)
  },

  judge: (params: JevJudgmentRequest) =>
    fetchAPI<JevJudgment>('/jev/judgments', {
      method: 'POST',
      body: JSON.stringify(params),
      timeoutMs: 180000,
    }),
}
