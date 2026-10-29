# pr-shipping-report

Builds the Night Forge shipping report: every merged pull request across the owner's GitHub repos,
with PR counts and diff sizes, rendered from `template.html` into `report.html`. Also writes
`report.json` with the figures.

```
python build.py [--owner jfreal] [--out report.html]
```

Needs the `gh` CLI, signed in with read access to every repo the owner has, private ones included.
The `night-forge-weekly` routine runs it every Monday and republishes the page to its artifact.
