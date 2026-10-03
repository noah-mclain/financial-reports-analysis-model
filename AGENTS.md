# Agent Instructions 

## GitHub Workflow Integration 
- Use the `gh` tool primitives via the shell or `pi-github` extensions to watch and pull open issues, PR lists, and specific PR diffs. 
- Automatically compose review summaries, construct comments, or draft new issues based on code flaws. 

## Execution Rules 
- All code 
execution tasks, rewrites, and file edits must be performed entirely via the **OpenAI Codex/ChatGPT** environment. 
- Do NOT trigger any calls or routing steps to Anthropic Claude APIs to prevent auxiliary billing. 
- If a sub-agent team workflow is required, delegate the execution sub-tasks explicitly to Codex-backed workers.
