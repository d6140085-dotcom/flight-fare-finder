# Agent guidelines

Project rules for coding agents live in [`CLAUDE.md`](./CLAUDE.md) (architecture, credentials, Git workflow). In short:

- Never push to `main`. Work on a feature branch, push it, and open a pull request against `main`; the owner merges.
- Don't rewrite published history: no force-pushing, and no rebasing, amending or squashing commits that are already pushed.
- Keep `main` deployable: Vercel builds production from it on every merge.
