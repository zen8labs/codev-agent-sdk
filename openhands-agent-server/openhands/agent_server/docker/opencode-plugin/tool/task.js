/**
 * Override OpenCode's builtin task tool with subprocess-based execution.
 *
 * The builtin task tool creates in-process subagent sessions that hang
 * indefinitely when OpenCode runs in serve mode (REST API). This override
 * spawns separate `opencode run` processes per subagent, which run
 * reliably because each process handles subagents in-process (no protocol
 * boundary).
 *
 * Exported as `default` so OpenCode's plugin loader registers it with
 * id = namespace = "task" (from the filename), which overwrites the
 * builtin task tool in the tool registry (custom tools are appended after
 * builtins and same-id wins in the Record<string, AITool>).
 *
 * Deployed to /workspace/project/tool/task.js in the Docker image.
 */

import { spawn } from "child_process"
import { mkdtempSync } from "fs"
import { join } from "path"
import { tmpdir } from "os"

const TASK_TIMEOUT_MS = 120000
const TASK_MAX_BUFFER = 10 * 1024 * 1024

function parseResult(stdout) {
  let responseText = ""
  let sessionID = null
  for (const line of stdout.trim().split("\n")) {
    if (!line.trim()) continue
    try {
      const event = JSON.parse(line)
      if (event.type === "session" && event.data?.id) {
        sessionID = event.data.id
      } else if (event.type === "message" && event.data?.role === "assistant") {
        for (const part of event.data?.parts || []) {
          if (part.type === "text") {
            responseText += part.text || ""
          }
        }
      }
    } catch {}
  }
  return { text: responseText, sessionID }
}

function runSubagent(prompt, agentType, cwd, env) {
  return new Promise((resolve) => {
    // Isolate subagent state from the parent serve process to prevent
    // shared database/session corruption that crashes the parent.
    const stateDir = mkdtempSync(join(tmpdir(), "opencode-subagent-"))
    const subEnv = { ...env, XDG_STATE_HOME: stateDir }
    const args = [
      "run", "--format", "json", "--dangerously-skip-permissions",
      "--agent", agentType || "general",
      prompt,
    ]
    const proc = spawn("opencode", args, {
      cwd,
      env: subEnv,
      stdio: ["ignore", "pipe", "pipe"],
      maxBuffer: TASK_MAX_BUFFER,
    })
    let stdout = ""
    let stderr = ""
    const timer = setTimeout(() => {
      proc.kill("SIGTERM")
      setTimeout(() => proc.kill("SIGKILL"), 5000)
    }, TASK_TIMEOUT_MS)

    proc.stdout.on("data", (chunk) => { stdout += chunk })
    proc.stderr.on("data", (chunk) => { stderr += chunk })
    proc.on("close", (code) => {
      clearTimeout(timer)
      const { text, sessionID } = parseResult(stdout)
      resolve({
        success: code === 0,
        text: text || stderr.slice(-500) || `(subagent exited with code ${code})`,
        sessionID,
        code,
      })
    })
    proc.on("error", (err) => {
      clearTimeout(timer)
      resolve({ success: false, text: `Subagent error: ${err.message}`, sessionID: null, code: -1 })
    })
  })
}

export default {
  description: "Launch a subagent to perform a specialized task. The subagent runs as a separate opencode run process for reliability.",
  args: {
    description: { type: "string", description: "A short (3-5 words) description of the task" },
    prompt: { type: "string", description: "The task for the agent to perform" },
    subagent_type: { type: "string", description: "The type of specialized agent to use for this task" },
    task_id: { type: "string", description: "Resume a previous task (optional)" },
    command: { type: "string", description: "The command that triggered this task (optional)" },
  },
  execute: async (args, ctx) => {
    const { description, prompt, subagent_type } = args
    const cwd = ctx?.directory || process.cwd()
    const result = await runSubagent(prompt, subagent_type, cwd, { ...process.env })
    const state = result.success ? "completed" : "error"
    const tag = result.success ? "task_result" : "task_error"
    return {
      title: description,
      metadata: {
        subagent_type,
        process_mode: "opencode-run",
        session_id: result.sessionID,
      },
      output: [
        `<task state="${state}">`,
        `<${tag}>`,
        result.text,
        `</${tag}>`,
        "</task>",
      ].join("\n"),
    }
  },
}
