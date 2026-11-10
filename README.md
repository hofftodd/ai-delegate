# ai-delegate

An MCP server, planned, that lets Claude Code hand bounded work to the local models behind
[ai-swap](https://github.com/hofftodd/ai-swap): quick questions and reviews answered by a local model,
and delegated coding jobs that run in a throwaway git worktree and come back as a diff. Claude keeps
the planning and the final say.

**Status: design only.** Nothing is built yet. A trial of an existing server
([Houtini LM](https://houtini.com/tools/houtini-lm/)) against ai-swap comes first, to find out what
delegation to these models is worth. See [DESIGN.md](DESIGN.md) for the plan, the tools, the
architecture and the criteria for building it.

Part of the [ai-tools](https://github.com/hofftodd/ai-tools) suite, alongside
[ai-swap](https://github.com/hofftodd/ai-swap) (serving models),
[ai-factory](https://github.com/hofftodd/ai-factory) (orchestrating coding agents) and
[ai-bench](https://github.com/hofftodd/ai-bench) (measuring models).
