# lldbAiHelper_MCP

* Update: `20260305`

## Function

LLDB MCP (Model Context Protocol) Bridge + Server, enabling AI assistants (Qoder, Cursor, Claude, etc.) to directly interact with LLDB debugger for iOS/Android reverse engineering and dynamic analysis.

Features:

- **Dual-File Architecture**: LLDB-side Bridge + Independent MCP Server, cleanly separated
- **Short Connection**: Stateless per-request TCP, Bridge crash/restart won't affect MCP Server
- **Async Continue**: Non-blocking `continue` + `wait_for_stop`, allowing parallel `stop` even during long-running execution
- **Concurrent Threads**: Each request handled in independent thread, supporting parallel operations (e.g., `wait_for_stop` + `lldb_stop`)
- **Auto Port**: Automatically finds available port (19527-19537), handshake via `~/.lldb_mcp_port`
- **ObjC Support**: `po` and `_shortMethodDescription` for iOS class introspection
- **Breakpoint Condition Error Detection**: `wait_for_stop` distinguishes between condition-matched breakpoint hits and condition expression parse/eval errors, preventing AI from misinterpreting syntax errors as successful matches
- **Event-Based Wait**: `wait_for_stop` uses LLDB SBListener event mechanism instead of polling, more reliable and responsive
- **File Logging**: Both MCP Server and Bridge write detailed logs to `logs/YYYYMMDD/` for debugging and troubleshooting
- **Auto Confirm**: Bridge auto-sets `auto-confirm true` so AI-driven operations (e.g., `breakpoint delete`) won't block on user prompts
- **Full Toolset**: 18 MCP tools covering execution control, memory, registers, breakpoints, disassembly, flow control, and ObjC analysis

## Git Repo

https://github.com/crifan/lldbAiHelper_MCP

https://github.com/crifan/lldbAiHelper_MCP.git

## Architecture

```
Qoder/Cursor/Claude  <── stdio (MCP) ──>  lldbAiHelper_MCP.py  <── Socket (TCP) ──>  lldbAiHelper_MCP_bridge.py (inside lldb)
    (AI Client)                              (MCP Server)                                (LLDB Bridge)
```

- **lldbAiHelper_MCP_bridge.py**: Runs inside LLDB process, uses LLDB Python API (`HandleCommand`), exposes Socket Server
- **lldbAiHelper_MCP.py**: Independent Python process, started by AI client, translates MCP calls to Socket requests

## File Structure

```bash
lldbAiHelper_MCP/
├── README.md
├── requirements.txt                  # Python dependencies (mcp[cli])
├── mcp_config_example.json           # AI client config example
├── lldbAiHelper_MCP.py               # MCP Server (AI client starts this)
├── lldbAiHelper_MCP_bridge.py        # LLDB Bridge (loaded inside lldb)
└── logs/                             # Runtime logs (auto-created, git-ignored)
    └── YYYYMMDD/                     # Date-grouped log files
        ├── mcp_server_*.log
        └── lldb_bridge_*.log
```

## Prerequisites

### Python Environment

Create a virtual environment and install dependencies:

```bash
cd /path/to/lldbAiHelper_MCP
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> Note: The MCP Server (`lldbAiHelper_MCP.py`) runs as an independent process and does NOT depend on LLDB's internal Python. Use any Python 3.10+ environment.

### LLDB

No extra installation needed for the Bridge. It uses LLDB's built-in Python API (`import lldb`), which is available when loading scripts inside LLDB.

## Usage

### 1. Load Bridge in LLDB

In your LLDB session (Terminal, Xcode console, or remote `debugserver`):

```
(lldb) command script import /path/to/lldbAiHelper_MCP_bridge.py
```

Output:

```
[Bridge] 已启动，监听 127.0.0.1:19527
[Bridge] 端口已写入 /Users/xxx/.lldb_mcp_port
[Bridge] 命令已注册: mcp_status, mcp_stop, mcp_restart
[Bridge] 已设置 auto-confirm=true (自动确认删除等操作)
[Bridge] 日志文件: /path/to/logs/YYYYMMDD/lldb_bridge_*.log
```

To auto-load on every LLDB session, add to `~/.lldbinit`:

```
command script import /path/to/lldbAiHelper_MCP_bridge.py
```

### 2. Configure AI Client

Add MCP server config to your AI client (Qoder, Cursor, Claude Desktop, etc.):

```json
{
  "mcpServers": {
    "lldbAiHelper": {
      "command": "/path/to/lldbAiHelper_MCP/venv/bin/python",
      "args": [
        "/path/to/lldbAiHelper_MCP/lldbAiHelper_MCP.py"
      ]
    }
  }
}
```

> Note: Replace `/path/to/` with your actual paths. See `mcp_config_example.json` for reference.

### 3. Start Debugging with AI

Once both Bridge and MCP Server are connected, the AI assistant can directly interact with your LLDB session. Example workflow:

1. AI calls `lldb_connect()` to verify Bridge connectivity
2. AI calls `lldb_status()` to get current debug state
3. AI calls `lldb_execute("breakpoint set -n main")` to set breakpoints
4. AI calls `lldb_continue()` to resume, then `lldb_wait_stop()` to wait for breakpoint hit
5. AI calls `lldb_execute("bt")` for backtrace, `lldb_po("self")` for ObjC inspection

### 4. Bridge Management Commands

Available commands inside LLDB after loading the Bridge:

| Command | Description |
|---------|-------------|
| `mcp_status` | Show Bridge running status and port |
| `mcp_stop` | Stop Bridge and release port |
| `mcp_restart` | Restart Bridge (re-bind port) |

## MCP Tools

| Tool | Description |
|------|-------------|
| `lldb_connect()` | Test Bridge connectivity |
| `lldb_status()` | Get debug status (process, thread, frame) as structured JSON |
| `lldb_execute(command)` | Execute any LLDB command (universal tool) |
| `lldb_memory_read(address, count, format)` | Read memory (hex/string/instruction/pointer/binary) |
| `lldb_disassemble(target, count)` | Disassemble (current PC, function name, or address) |
| `lldb_continue()` | Resume execution (async, returns immediately) |
| `lldb_stop()` | Pause running process |
| `lldb_wait_stop(timeout)` | Wait for stop event; distinguishes normal breakpoint hits vs condition expression errors |
| `lldb_flow_control(action)` | Step control: next/step/finish/ni/si |
| `lldb_po(expression)` | Print ObjC object description |
| `lldb_objc_class_info(class_name)` | Get ObjC class methods (`_shortMethodDescription`) |
| `lldb_register_read(register)` | Read registers (all or specific) |
| `lldb_backtrace(count)` | Get call stack |
| `lldb_breakpoint_set(address, name, condition, one_shot)` | Set breakpoint (by address, symbol, or with condition) |
| `lldb_breakpoint_list()` | List all breakpoints |
| `lldb_breakpoint_delete(breakpoint_id)` | Delete breakpoint(s) |
| `lldb_image_list(filter)` | List loaded modules/dylibs (for base address calculation) |
| `lldb_expression(expr, lang)` | Evaluate expression (C/ObjC/Swift) |

## Design Notes

### Why Dual-File?

- **Isolation**: Bridge runs in LLDB's address space with `import lldb`; MCP Server runs independently
- **No stdio Conflict**: LLDB uses terminal stdio; MCP protocol uses stdio. Separating them avoids conflict
- **Robustness**: AI client crash/restart doesn't affect Bridge (and vice versa)

### Why Short Connections?

- AI operations are inherently slow (seconds per tool call), TCP handshake overhead is negligible
- AI clients frequently crash/restart; short connections mean Bridge sockets stay clean
- No stale connection state to manage

### Why Async Continue?

- `continue` may block indefinitely (anti-debug, deadloop, waiting for user input)
- Async `continue` returns immediately; AI uses `wait_for_stop` in separate thread to poll
- AI can call `lldb_stop()` concurrently to interrupt, even while `wait_for_stop` is pending
