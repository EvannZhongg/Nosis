# Security Policy

## Reporting a vulnerability

Report security issues privately through GitHub's private vulnerability
reporting:

<https://github.com/EvannZhongg/Nosis/security/advisories/new>

Please do not open a public issue, and avoid posting exploit details in
discussions, pull requests or social media before a fix is available.

A useful report includes:

- the commit or version you tested, the platform, and the shell backend
- the permission preset and execution scope in use
- what you expected the boundary to be, and what it did instead
- reproducing steps, and whether the trigger is untrusted content the agent
  can be steered by — a fetched page, an MCP server response, a file in the
  project it was asked to work on

## Scope

Nosis runs commands and edits files on the machine it is installed on, so the
boundaries it claims matter more than the absence of bugs. In scope:

- the workspace boundary enforced by the file tools
- the workspace sandbox used by workspace-scope shell commands on each platform
- the approval gate for host-scope commands, file writes and desktop actions
- anything that turns content the agent reads into execution or file access
  outside those boundaries

Out of scope: what every agent with workspace write access can already do —
editing the files you pointed it at, creating files inside the workspace, or
reading host files from a shell backend that documents retained read access.

## What to expect

We will acknowledge the report, say whether we consider it in scope, and keep
you updated until a fix ships. Fixes land as ordinary commits on `main`; there
are no long-term support branches to backport to.

If you want credit, tell us the name or account to use. We add it to the
Contributors section of the README when the fix ships; otherwise the fix is
published without attribution.
