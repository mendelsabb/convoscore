# Scoring rubric

ConvoScore asks the model for three fields and nothing else. The contract is a strict JSON schema;
the application validates every response again before storing it.

```json
{
  "sentiment": "positive | neutral | negative",
  "risk_score": 0,
  "rationale": "one or two sentences naming the concrete trigger in the conversation"
}
```

Versions stored with every result: `prompt_version` (currently `v1`), `schema_version`
(currently `1`) and `pricing_version` (the price table used for the cost estimate). Changing the
rubric means bumping `prompt_version`; changing the output shape means bumping `schema_version`.

## Sentiment

The **customer's** overall tone across the conversation, not the agent's.

| Value | Meaning |
|---|---|
| `positive` | the customer expresses satisfaction, gratitude or relief, and ends the conversation content with the issue resolved or on a good note |
| `neutral` | informational or transactional exchange; no clear emotional signal either way, or mixed signals that balance out |
| `negative` | frustration, anger, disappointment or distrust dominate, regardless of whether the agent was polite |

Sentiment and risk are **independent**. A calm, polite customer who says they are cancelling is
`neutral` sentiment and high risk. An angry customer whose problem is fully solved by the end may be
`negative` sentiment and low-to-moderate risk.

## Risk score

`risk_score` is an integer from 0 to 100 estimating **how likely this account needs human
intervention** beyond the conversation itself: churn, escalation, financial exposure, legal or
regulatory exposure, safety, or reputational harm.

The model returns a number; the application derives the band. Bands are guidance for reviewers and
for the prompt, not hard thresholds.

| Band | Range | Typical signals |
|---|---|---|
| **Low concern** | 0–25 | resolved question, routine request, satisfied customer, no unresolved issue |
| **Moderate concern** | 26–50 | unresolved friction, mild dissatisfaction, repeated contact about the same thing, a promise the agent still has to keep |
| **High concern** | 51–75 | explicit dissatisfaction, repeated failures, cancellation hinted at, refund or chargeback dispute, request to speak to a manager |
| **Urgent / escalation** | 76–100 | explicit cancellation or churn threat, threat of legal action, regulator, press or public review, allegation of a safety issue or data breach, allegation of discriminatory or abusive treatment, escalation demand that was ignored |

Rules the model is given:

1. Use only information that is present in the conversation. Never invent details, intentions or
   history that are not stated.
2. Score the risk of the **situation**, not the politeness of the language. Polite threats are still
   threats.
3. A resolved issue lowers risk; an unresolved one raises it, even if the customer is calm.
4. When signals conflict, prefer the higher band and explain why in the rationale.
5. The rationale must be at most two sentences, must name the concrete trigger (quote or paraphrase
   it), and must be readable by a support lead who has not seen the conversation.

## Reproducibility

- `temperature` is set to `0` for models that accept it; models that reject the parameter (the
  gpt-5 family) are called without it. The application never assumes unsupported parameters.
- The same conversation is never scored twice by design (idempotent job processing), so run-to-run
  variance only matters when the prompt or model changes — which is why both are versioned.
- Scores are model outputs, not ground truth. The review UI exists so that a human can disagree.

## Cost estimate

Token counts returned by the provider are stored per job. The estimated cost is computed from a
versioned price table in the backend (USD per million input and output tokens per model). The
number is labelled *estimated* in the API, the UI and Grafana; production billing must be
reconciled against the provider's invoices.
