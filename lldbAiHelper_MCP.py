#!/usr/bin/env python3
"""
lldbAiHelper_MCP.py - MCP Server (由 Qoder 启动)

通过 Socket 连接到 lldb 内的 Bridge，向 AI 暴露调试工具。
自动读取 ~/.lldb_mcp_port 获取 Bridge 端口（端口自动协商）。

通信模式：短连接（无状态）
    - 每个命令独立建连，处理完关闭
    - Bridge 崩溃/重启不影响 MCP Server 状态

架构:
    Qoder ←─ stdio (MCP) ─→ 本文件 ←─ Socket ─→ lldbAiHelper_MCP_bridge.py (lldb内)
"""

import sys
import os
import json
import socket
from typing import Dict, Any

PORT_FILE = os.path.expanduser("~/.lldb_mcp_port")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19527


def _get_port() -> int:
    """从握手文件读取 Bridge 端口"""
    if os.path.exists(PORT_FILE):
        try:
            with open(PORT_FILE) as f:
                return int(f.read().strip())
        except:
            pass
    return DEFAULT_PORT


def call_bridge(cmd: str, socket_timeout: float = 120.0, **args) -> str:
    """
    发送命令到 Bridge（短连接：每次新建，用完关闭）
    Bridge 崩溃/重启后，下次调用自动恢复
    """
    port = _get_port()
    if port == -1:
        return "错误: 找不到 Bridge 端口。请在 lldb 中执行: command script import lldbAiHelper_MCP_bridge.py"
    
    request = json.dumps({'cmd': cmd, 'args': args}, ensure_ascii=False) + '\n'
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(socket_timeout)
            s.connect((DEFAULT_HOST, port))
            s.sendall(request.encode('utf-8'))
            
            # 接收响应
            buf = ""
            while '\n' not in buf:
                data = s.recv(65536)
                if not data:
                    break
                buf += data.decode('utf-8')
                
            if not buf.strip():
                return "错误: Bridge 无响应"
                
            result = json.loads(buf.split('\n')[0])
            if result.get('success'):
                data = result.get('result')
                if isinstance(data, dict):
                    return json.dumps(data, indent=2, ensure_ascii=False)
                return str(data) if data is not None else "OK"
            else:
                error = result.get('error', '未知错误')
                tb = result.get('traceback', '')
                return f"错误: {error}" + (f"\n{tb}" if tb else "")
                
    except ConnectionRefusedError:
        return f"错误: 端口 {port} 拒绝连接。请确认 lldb 中已执行 mcp_start"
    except socket.timeout:
        return f"错误: 超时 ({socket_timeout}s)"
    except Exception as e:
        return f"错误: {e}"


# ============== MCP Server ==============

def run_mcp_server():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("lldbAiHelper", json_response=True)
    
    # ---- 连接/状态 ----
    
    @mcp.tool()
    def lldb_connect() -> str:
        """测试与 lldb Bridge 的连接（自动读取端口握手文件）"""
        return call_bridge('ping')
        
    @mcp.tool()
    def lldb_status() -> str:
        """获取调试状态（进程、线程、当前帧等结构化信息）"""
        return call_bridge('get_status')
        
    # ---- 核心：通用命令 ----
    
    @mcp.tool()
    def lldb_execute(command: str) -> str:
        """
        执行任意 lldb 命令（万能工具）
        
        Args:
            command: 任意 lldb 命令
            
        Examples:
            lldb_execute("bt")
            lldb_execute("register read")
            lldb_execute("po [UIApplication sharedApplication]")
            lldb_execute("breakpoint set -n main")
            lldb_execute("image list")
            lldb_execute("thread list")
            lldb_execute("frame variable")
            lldb_execute("watchpoint set expression -w write -- 0x1234")
            lldb_execute("process connect connect://127.0.0.1:1234")
            lldb_execute("expression -l objc -O -- [NSClassFromString(@\"UIViewController\") _shortMethodDescription]")
        """
        return call_bridge('execute', command=command)
        
    # ---- 内存读取 ----
    
    @mcp.tool()
    def lldb_memory_read(address: str, count: int = 256, format: str = "x") -> str:
        """
        读取内存
        
        Args:
            address: 地址 (如 "0x100001234", "$x0", "$sp")
            count: 字节数 (默认 256)
            format: 格式 - "x"(hex), "s"(string), "i"(instruction/disasm), "p"(pointer), "b"(binary)
        """
        fmt_map = {'x': 'x', 's': 's', 'i': 'i', 'p': 'A', 'b': 'Y'}
        fmt = fmt_map.get(format, 'x')
        return call_bridge('execute', command=f'memory read -c {count} -f {fmt} {address}')
        
    # ---- 反汇编 ----
    
    @mcp.tool()
    def lldb_disassemble(target: str = "", count: int = 30) -> str:
        """
        反汇编
        
        Args:
            target: 空=当前PC, 函数名="-[NSObject init]", 地址="0x100001234"
            count: 指令数量
        """
        if not target:
            return call_bridge('execute', command=f'disassemble -c {count}')
        elif target.startswith('0x'):
            return call_bridge('execute', command=f'disassemble -s {target} -c {count}')
        else:
            return call_bridge('execute', command=f'disassemble -n "{target}"')
            
    # ---- 执行控制（异步 continue + wait_stop） ----
    
    @mcp.tool()
    def lldb_continue() -> str:
        """继续执行程序（异步，立即返回。之后用 lldb_wait_stop 等待断点命中）"""
        return call_bridge('continue_async')
        
    @mcp.tool()
    def lldb_stop() -> str:
        """暂停正在运行的程序"""
        return call_bridge('stop_process')
        
    @mcp.tool()
    def lldb_wait_stop(timeout: float = 60.0) -> str:
        """
        等待程序停止（断点/异常/单步完成）
        
        Args:
            timeout: 超时秒数
            
        Returns:
            停止原因和当前帧信息
        """
        # socket_timeout 需大于 Bridge 端的 wait 超时
        return call_bridge('wait_for_stop', socket_timeout=timeout + 10, timeout=timeout)
        
    @mcp.tool()
    def lldb_flow_control(action: str) -> str:
        """
        控制执行流（单步等）
        
        Args:
            action: 动作
                - "next" / "n": 源码级步过
                - "step" / "s": 源码级步入
                - "finish": 步出到调用者
                - "nexti" / "ni": 汇编级步过
                - "stepi" / "si": 汇编级步入
        """
        action_map = {
            "next": "next", "n": "next",
            "step": "step", "s": "step",
            "finish": "finish",
            "nexti": "ni", "ni": "ni",
            "stepi": "si", "si": "si",
        }
        cmd = action_map.get(action.lower())
        if not cmd:
            return f"无效动作: {action}。可选: next, step, finish, nexti, stepi"
        return call_bridge('execute', command=cmd)
    
    # ---- iOS/ObjC 逆向专属 ----
    
    @mcp.tool()
    def lldb_po(expression: str) -> str:
        """
        打印 ObjC 对象描述 (print object)
        
        Args:
            expression: ObjC 表达式
            
        Examples:
            lldb_po("self")
            lldb_po("[UIApplication sharedApplication]")
            lldb_po("(id)0x1234abcd")
        """
        return call_bridge('execute', command=f'po {expression}')
    
    @mcp.tool()
    def lldb_objc_class_info(class_name: str) -> str:
        """
        获取 ObjC 类的方法列表和属性
        
        Args:
            class_name: 类名 (如 "UIViewController", "NSURLSession")
        """
        expr = f'expression -l objc -O -- [NSClassFromString(@"{class_name}") _shortMethodDescription]'
        return call_bridge('execute', command=expr)
    
    print("[MCP] 启动中...", file=sys.stderr)
    mcp.run(transport='stdio')


if __name__ == "__main__":
    if '--help' in sys.argv:
        print("lldbAiHelper MCP Server")
        print("")
        print("1. 在 lldb 中: command script import lldbAiHelper_MCP_bridge.py")
        print("2. 本文件由 Qoder 自动启动，或: python lldbAiHelper_MCP.py")
    else:
        run_mcp_server()
