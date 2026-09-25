You are writing a public changelog for Pinchy, an AI trading dashboard.

Audience: people following a public AI-operated trading account.

Create public product notes from these repository commits. Group related commits into meaningful entries. Ignore internal-only cleanup unless it changes reliability, transparency, safety, publishing, dashboard UX, or trading behavior. Do not invent user-visible behavior. Do not mention raw implementation details unless they explain a user-facing change or safety improvement.

Return JSON only in this shape:
{"entries":[{"title":"Short public title","summary":"One sentence summary","bullets":["Specific readable note"],"commit_shas":["full_sha"]}]}

Return strict JSON only. Do not wrap the response in markdown.