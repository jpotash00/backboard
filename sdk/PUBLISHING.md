# Publishing the SDK to npm

The npm package is rooted in `sdk/` and ships **only the compiled client SDK** — the Python
engine (`engine/`, `api/`, `eval/`) lives in a separate package and can never end up in the
npm tarball. Two things enforce that:

- `package.json` `"files": ["dist", "README.md"]` — an allowlist. Only `dist/` (compiled JS +
  `.d.ts`) and the README are packed; `src/`, configs, and everything else are excluded.
- `prepack`/`prepublishOnly` run `tsc` so `dist/` is always freshly built from `src/`.

Verify exactly what would ship before you publish:

```bash
cd sdk
npm pack --dry-run     # lists the tarball contents — should be dist/*.{js,d.ts,map} + README + package.json
```

## One-time decisions before the first publish

1. **Package name.** Currently `offboard` (unscoped). Unscoped names are first-come — if it's
   taken, scope it (`@your-org/offboard`) and update the `import` paths in the README. Scoped
   public packages need `"publishConfig": { "access": "public" }` (already set).
2. **License.** Currently `UNLICENSED`. Fine for a closed package; switch to `MIT` (and add a
   `LICENSE` file) if you want frictionless adoption.
3. **npm auth.** `npm login` (or set `NPM_TOKEN` in CI). `npm whoami` should print your user.

## Publish

```bash
cd sdk
npm version patch            # or minor / major — bumps package.json + git tag
npm publish                  # prepack builds dist/ first; --access public is implied by config
```

For a prerelease: `npm version prerelease --preid rc && npm publish --tag next`.

## What is NOT published here

The Python engine is intentionally **not** an npm package, and publishing it to PyPI is a
separate decision (it bundles your prompts, taxonomy, and decisioning IP). Keep it hosted, or
split an `engine`-only wheel before ever pushing to PyPI. See the repo root `pyproject.toml`.
