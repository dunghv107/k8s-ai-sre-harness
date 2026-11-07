# Contract: diagnosis output and Telegram rendering

The sub-agent's final message must be this JSON (validated; one repair turn allowed):

```json
{
  "label": "infra | app | security | unknown",
  "status": "ongoing | recovered | blocked | waiting",
  "confidence": "high | medium | low",
  "summary": "one sentence, Vietnamese, ≤ 160 chars",
  "cause": "one sentence or empty when unknown",
  "evidence": [
    {"source": "logs|events|rollout|metrics|trace|pod|audit|netpol", "ref": "pod/query/policy/trace id", "excerpt": "≤ 200 chars, redacted"}
  ],
  "next_step": "one action for the operator, ≤ 120 chars",
  "human_checks": ["≤ 3 actions only a human can do (exec/shell, read a Secret, roll back, edit a policy), ≤ 120 chars each; [] when none"],
  "dev_message": "optional draft for app issues (US3), empty in MVP"
}
```

Rules: `evidence` empty → forced to `label: unknown, confidence: low, summary: "Chưa đủ bằng chứng: <missing>"`.
`label: security` is only allowed for protected incidents. No code-change proposals for `app`. `human_checks` is optional (missing means `[]`), must be a list of strings, is trimmed, redacted and cut to 3 items; it never lists what the agent already checked. `unknown` is only for when the context pack and the tools really lack evidence.

Telegram rendering (HTML, ≤ 3 500 chars, escaped):

```
🧠 <b>Chẩn đoán #<id></b> · <service> · <rule>
🏷️ <label> · <status> · tin cậy <confidence>
🎯 <summary>
🔎 <cause>
📎 <source>: <excerpt>   (max 3 lines)
➡️ <next_step>
🧑‍💻 Chỉ người làm được:      (only when human_checks is not empty)
• <item>   (max 3)
⏱️ <first_seen local> · <occurrences> lần · <model> · <cost> USD
```

In `fallback` and `both` the title carries `[native]` or `[holmes]` (the engine that produced the final answer); after a Holmes backup the model field reads `<native model> → holmes/<model>` and the cost is the sum of both runs.

Protected incidents add `🔒 Bảo mật — không tự đóng, cần người xác nhận`.
