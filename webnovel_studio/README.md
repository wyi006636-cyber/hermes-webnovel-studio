# Webnovel Studio for Hermes

**Write a hundred-chapter serialized novel with Hermes without it forgetting who is alive, what was foreshadowed, or what happened in chapter 3.**

Webnovel Studio is a Hermes Agent plugin for long-form fiction, built first for Chinese web novels (网文). It turns a one-line idea into a project with a world bible, a volume outline and finished chapters on disk. The book's state is kept outside the chat: every chapter is planned, written, reviewed and saved by a runtime, so the story stays consistent across sessions, crashes and model switches.

A prompt or a single skill can write *a* chapter. A serialized novel also needs things a prompt can't provide:

| What a long serial needs | What the plugin does |
|---|---|
| Story memory across 100+ chapters | Before each chapter, a context-curator role assembles the outline, settings, character state and recent prose from the project files, not from chat history |
| Never losing a chapter | Each write is a **resumable run** (`.webnovel/runs/<id>/run.json`) that can be resumed or aborted after a crash or timeout |
| One writer at a time | A cross-process **project lock** stops two sessions from writing the same book at once |
| Protecting the book's canonical state | A `pre_tool_call` **guard** blocks other tools (shell, file edits) from silently rewriting `.webnovel/state.json`, `index.db` or published chapters, so all changes go through the runtime |
| Right model for each job | Calls are routed by role through Hermes's own `ctx.llm` (architect, context curator, writer, reader-editor), each with its own temperature and timeout. The plugin never sees an API key |
| Picking up where you left off | A `pre_llm_call` hook adds a one-line project summary (title, current chapter, word count) when you continue a session |
| Seeing the whole book | A local, read-only dashboard shows characters, foreshadowing, pacing and files |

## Install

```bash
hermes plugins install webnovel-studio      # once listed in the catalog
# or straight from GitHub:
hermes plugins install "wyi006636-cyber/hermes-webnovel-studio#webnovel_studio" --yes-deps --enable
```

Requires Hermes `>=0.22.5`. At install time Hermes asks for consent to install the plugin's Python dependencies: `filelock`, `pydantic`, `fastapi`, `uvicorn` and `watchdog`. The last three are used only by the dashboard.

## Use

Everything happens in a normal Hermes chat, through slash-command skills:

| Command | What it does |
|---|---|
| `/webnovel-init <premise>` | Proposes a novel (genre, hook, protagonist, world). After one confirmation it creates the project folder |
| `/webnovel-plan 1` | Writes the outline for volume 1: story events, character choices and chapter-end hooks |
| `/webnovel-write 12 <notes>` | Writes chapter 12 from the outline, settings, earlier chapters and your notes, then saves it to `正文/` |
| `/webnovel-review 10 12` | Optional craft review of story, characters, dialogue, pacing and prose. Advisory only, never blocking |
| `/webnovel-query <name / loop / rule>` | Looks up characters, relationships, open loops or world rules without changing anything |
| `/webnovel-learn <lesson>` | Saves a reusable writing lesson to project memory |
| `/webnovel-dashboard` | Starts the read-only dashboard on `127.0.0.1` |
| `/webnovel-doctor` | Read-only health check of the project |

A project is a plain folder you can open in any editor or Obsidian:

```
my-novel/
├── 设定集/      world bible, characters, power system
├── 大纲/        master outline and volume outlines
├── 正文/        the chapters
└── .webnovel/   runtime state, run ledger, lock (managed by the plugin)
```

### Per-role model routing (optional)

By default every role uses the model of the current Hermes session. To give a role its own model, create `~/.hermes/plugin-data/webnovel-studio/config.yaml` and refer to providers by name only; credentials stay in Hermes:

```yaml
routing:
  allowed_providers: [openrouter]
  allowed_models: [anthropic/claude-sonnet-4]
roles:
  writer:
    route: configured
    provider: openrouter
    model: anthropic/claude-sonnet-4
    temperature: 0.9
```

## Security and disclosure

Please read this before installing.

- **Subprocesses.** The novel engine runs as a Python subprocess (`_vendor/webnovel-writer/scripts/webnovel.py`), using the same interpreter and import path Hermes runs on, with `shell=False`. The child process gets an **allowlisted environment only** (paths, locale, temp directories, plus `PYTHONPATH` so it sees the plugin's installed dependencies). Hermes credentials and other environment variables are never passed to it.
- **Background process.** `/webnovel-dashboard` starts a local web server (uvicorn) bound to `127.0.0.1` on a free port. It serves read-only GET endpoints and stops when you ask, or when Hermes exits.
- **Files read and written.** The plugin reads and writes only inside the novel project folder you name (`设定集/`, `大纲/`, `正文/`, `.webnovel/`) and inside its own Hermes data directory, `<hermes home>/plugin-data/webnovel-studio/`, which holds an optional `config.yaml` and the engine's project registry. Overwriting an existing chapter needs explicit confirmation (`--rewrite`).
- **Network.** The plugin makes **no network calls of its own**. All model calls go through Hermes (`ctx.llm`). The upstream engine's optional vector retrieval feature, which sends text to third-party embedding/rerank APIs, is **disabled** in this build, and the engine never reads `.env` files.
- **Credentials.** None are stored, read or requested. `requires_env` is empty.
- **Guards and approvals.** The `pre_tool_call` hook can only *block* tool calls that would bypass the runtime and edit protected story files. It never approves anything, and it never runs commands.
- **Telemetry.** None.
- **No self-updating.** Updates arrive only through a reviewed catalog SHA bump.

## Credits and license

Webnovel Studio is **derived from [lingfengQAQ/webnovel-writer](https://github.com/lingfengQAQ/webnovel-writer)** (GPL-3.0), the Claude Code plugin whose engine (project layout, state store, story contracts, dashboard) is vendored under `webnovel_studio/_vendor/webnovel-writer/`. The vendored copy is a *reduced* tree of upstream tag `v6.2.1` (commit `59654ccaa17f240c5ae41fe51db9443284f8ca1f`) with four small, reviewable patches in [`vendor-patches/`](https://github.com/wyi006636-cyber/hermes-webnovel-studio/tree/main/vendor-patches):

1. `0001`: preflight no longer requires the Claude Code skill tree (this plugin ships its own Hermes skills).
2. `0002`: vector retrieval and `.env` loading are disabled, so the engine makes no network calls.
3. `0003`: removes the engine's built-in self-test block and renames an HTML marker. Both were false positives in the Hermes install scanner.
4. `0004`: `doctor` reports the Hermes build profile, with vector retrieval shown as disabled and no `.env.example` or `aiohttp` expected.

`python scripts/vendor_upstream.py --upstream <clone> --check` rebuilds the vendor tree from upstream plus these patches and verifies it byte for byte.

Everything Hermes-specific is new work in this repository: the runtime and tool contract, role-routed `ctx.llm` writing pipeline, resumable run ledger, project lock, `pre_tool_call` guard, session-context hook, Hermes skills, and the test suite (400+ tests).

Licensed under **GPL-3.0-only**, the same as upstream. See [LICENSE](https://github.com/wyi006636-cyber/hermes-webnovel-studio/blob/main/LICENSE).
