# One-prompt project setup

Give the agent the unpacked `traf-seo-master` folder and the existing target project. Paste this prompt with the actual package/project paths:

> Install the unpacked TRAF SEO Master at `<package>` into the existing project `<project>` for the host you are actually running. Read the package SKILL.md and project instructions. Run `python3 <package>/scripts/seo-master.py doctor`, then `install --host <actual-host> --project <project> --dry-run`. Use the planned project-local install and verify its bytes with doctor. If the host supports reading files but does not discover the skill in this session, use the installed SKILL.md directly. Then audit `<URL or supplied file>` using `quick-audit` and [Quick audit](references/quick-audit.md). For a domain choose a ten-page sample, for an exact URL check that page, and for a supplied file use its local mode. Save HTML and JSON in a fresh output directory, give the first five evidence-backed fixes and the actual gaps. For a full Master audit continue with its exact profile/evidence checks. For a site before launch or weekly monitoring, follow [Site planning](references/site-planning.md).

The host/path values are user environment inputs, not literal directory names to create. No new project should be initialized just to install the skill.

After setup, a concrete audit invocation is: **“Use traf-seo-master to add `<actual site>` and audit it with the full master. Reuse valid receipts, execute permitted checks, and return findings, authorized fixes/readback and missing inputs.”** Supply an existing project/source locator when available. Profiles and retained evidence distinguish sites; the shared registry supplies the same check universe. For portfolio gaps, request `site-missing --sites <actual sites root>` and continue through the permitted native checks, including never-audited sites and newer-registry gaps.

| Host | Project skill destination | Invocation |
|---|---|---|
| Codex | `.agents/skills/traf-seo-master/` | `$traf-seo-master` or matching task |
| Claude Code | `.claude/skills/traf-seo-master/` | `/traf-seo-master` or matching task |
| Cursor | `.cursor/skills/traf-seo-master/` | `/` skill selector or matching task |
| GitHub Copilot | `.github/skills/traf-seo-master/` | Matching request in a skills-capable surface |
| Gemini CLI | `.gemini/skills/traf-seo-master/` | `/skills list` and the host's activation prompt |
| OpenClaw | `.agents/skills/traf-seo-master/` in the selected workspace | Host skill list or matching request |
| Windsurf or Devin Local | Verify the active project's skill folder in that version | Explicit `SKILL.md` fallback when file tools are available |
| Hermes | Use the selected profile's configured skill directory or its native import | Native skill discovery or explicit `SKILL.md` |

Project skill locations are documented by [OpenAI](https://learn.chatgpt.com/docs/build-skills), [Claude Code](https://code.claude.com/docs/en/skills), [Cursor](https://cursor.com/docs/context/skills), [GitHub Copilot](https://docs.github.com/en/copilot/concepts/customizations/agent-skills), [Gemini](https://geminicli.com/docs/cli/skills/) and [OpenClaw](https://docs.openclaw.ai/tools/skills). Hermes documents its [profile and import paths](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills/). A conversation without filesystem and Python tools cannot execute the local collector. Filesystem layout/copy checks do not establish actual model/runtime/tool compatibility in every product, remote session or operating system. Other agents can read SKILL.md explicitly if they support filesystem access, but automatic discovery is not claimed for them. A prompt alone does not grant tools or account access.

If the host was launched from an umbrella or different repository, a project-local skill may not be discovered from the target folder. Select the actual target project or use the installed SKILL.md explicitly; do not modify unrelated shared roots/global settings merely to force the current session's palette. Verify discovery separately from byte-correct installation.

Python 3.10+ runs the bundled quick audit. Python 3 runs the core local helper. The core install and site profile/evidence/report commands are offline and do not bundle Node/Chrome/Lighthouse or paid API clients. Actual collection and expert work use the native host's available tools; missing account/time access remains explicit. The optional single-URL `audit` collector requires Node >=22.19, existing Chrome and the explicitly pinned Lighthouse13.6.0 CLI; see [performance](references/performance.md). Any dependency installation follows the current project storage/permission rules and current official source, outside the copy-only installer.

Public `assets/lighthouse/package.json` and `package-lock.json` preserve the qualified optional dependency tree. If that tool is needed and local dependency installation is within scope, copy these manifests into an approved tool/cache folder and run `npm ci --ignore-scripts --no-audit --no-fund --prefix <tool-folder> --cache <approved-cache>`. Provide the resulting actual executable path to the audit helper; no credentials or model provider are configured by this step. Do not download Chrome or write caches into another storage contour implicitly.

Identical existing installs are reusable. A differing target is a conflict; preserve it and inspect its managed/owner changes rather than forcing a replacement. Do not edit user AGENTS/CLAUDE rules, registry credentials, `.env`, MCP configuration or model settings during bootstrap. New tool/source versions need a new bounded qualification; the source catalogue does not auto-update execution.
