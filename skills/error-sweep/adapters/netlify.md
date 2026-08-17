# Adapter: netlify

Function, edge-function, and deploy errors from a Netlify site. The CLI is logged in under the user's Windows profile — **no token needed**.

Card must supply: `site_id`, and whether the repo is `netlify link`ed.

## 1. Working directory

`netlify logs` refuses to run outside a linked directory, and `--url` does **not** lift that.

If the repo is linked (`.netlify/state.json` present), run from the repo. If not, make a scratch link dir once:

```bash
mkdir -p "$TEMP/<proj>-netlify/.netlify"
printf '{"siteId":"<site_id>"}' > "$TEMP/<proj>-netlify/.netlify/state.json"
```

Never create `.netlify/state.json` inside a repo that does not already have one.

## 2. Function + edge-function errors

```bash
netlify logs --json --since <window> --level error --level fatal \
  --source functions --source edge-functions > out.json
```

- **Redirect to a file. Do not pipe through `Select-Object`** — piping truncates the stream *and* returns a spurious non-zero exit code, which reads as a failed collector when it succeeded.
- **Do NOT pass `--source deploy`** — it 404s on these sites. Use step 3.
- Each line is already `{source, name, timestamp, level, message}` — the adapter contract shape.
- **Zero lines is the healthy result.** Exit 0 + empty file = success.

## 3. Failed deploys

```bash
netlify api listSiteDeploys --data '{"site_id":"<site_id>","per_page":10}'
```

Parse each deploy's `state` and `error_message`. `state == "error"` is a finding, with two exceptions that are **normal and must be ignored**:

| `error_message` | Why it is not a bug |
|---|---|
| `Canceled build due to no content change` | `netlify.toml`'s `[build] ignore` whitelist working as designed |
| `Skipped due to account credit usage exceeded` | Billing condition. Mention in the report; file nothing |

## 4. The whitelist gotcha, and why it matters to a fix agent

`netlify.toml`'s `ignore` command is a **whitelist** of build inputs. If a fix adds a new input — a new top-level folder the build reads, a new config file, a script that starts consuming a JSON file — that path **must** be added to the `ignore` command in the same PR, or edits to it will silently never deploy. Put this in every fix brief for a Netlify project.

## 5. Build-credit cost

Every pushed PR branch triggers a deploy-preview build. On metered plans it is usually builds, not bandwidth, that dominate the bill — which is why the pipeline has a per-project fix-session cap. Check the project's own budget before raising a cap, and say in the report what it will cost.
