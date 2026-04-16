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
import logging
from datetime import datetime
from typing import Dict, Any

# ============== 日志配置 - 按日期子目录存放 ==============
_now = datetime.now()
_date_str = _now.strftime('%Y%m%d')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", _date_str)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"mcp_server_{_now.strftime('%Y%m%d_%H%M%S')}.log")

_log_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
_log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_log_handler.setLevel(logging.DEBUG)

logger = logging.getLogger("mcp_server")
logger.setLevel(logging.DEBUG)
logger.addHandler(_log_handler)

# 确保日志立即写入
class FlushHandler(logging.Handler):
    def emit(self, record):
        _log_handler.emit(record)
        _log_handler.flush()

logger.addHandler(FlushHandler())

# ============== 配置 ==============
PORT_FILE = os.path.expanduser("~/.lldb_mcp_port")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19527

logger.info(f"MCP Server 启动, LOG_FILE={LOG_FILE}")


def _get_port() -> int:
    """从握手文件读取 Bridge 端口"""
    if os.path.exists(PORT_FILE):
        try:
            with open(PORT_FILE) as f:
                port = int(f.read().strip())
                logger.debug(f"从 {PORT_FILE} 读取端口: {port}")
                return port
        except Exception as e:
            logger.warning(f"读取端口文件失败: {e}")
    logger.debug(f"使用默认端口: {DEFAULT_PORT}")
    return DEFAULT_PORT


def call_bridge(cmd: str, socket_timeout: float = 120.0, **args) -> str:
    """
    发送命令到 Bridge（短连接：每次新建，用完关闭）
    Bridge 崩溃/重启后，下次调用自动恢复
    """
    port = _get_port()
    if port == -1:
        logger.error("找不到 Bridge 端口")
        return "错误: 找不到 Bridge 端口。请在 lldb 中执行: command script import lldbAiHelper_MCP_bridge.py"
    
    request = json.dumps({'cmd': cmd, 'args': args}, ensure_ascii=False) + '\n'
    logger.info(f"call_bridge: cmd={cmd}, args={args}, port={port}")
    
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
                logger.error("Bridge 无响应")
                return "错误: Bridge 无响应"
                
            result = json.loads(buf.split('\n')[0])
            if result.get('success'):
                data = result.get('result')
                logger.debug(f"call_bridge 成功: cmd={cmd}, result_type={type(data).__name__}")
                if isinstance(data, dict):
                    return json.dumps(data, indent=2, ensure_ascii=False)
                return str(data) if data is not None else "OK"
            else:
                error = result.get('error', '未知错误')
                tb = result.get('traceback', '')
                logger.error(f"call_bridge 失败: cmd={cmd}, error={error}")
                return f"错误: {error}" + (f"\n{tb}" if tb else "")
                
    except ConnectionRefusedError:
        logger.error(f"端口 {port} 拒绝连接")
        return f"错误: 端口 {port} 拒绝连接。请确认 lldb 中已执行 mcp_start"
    except socket.timeout:
        logger.error(f"超时 ({socket_timeout}s)")
        return f"错误: 超时 ({socket_timeout}s)"
    except Exception as e:
        logger.error(f"call_bridge 异常: {e}", exc_info=True)
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
    
    @mcp.tool()
    def lldb_memory_read_batch(addresses: str, count: int = 256, format: str = "x") -> str:
        """
        批量读取多个地址的内存
        
        Args:
            addresses: 逗号分隔的地址列表 (如 "0x100001234,$x0,$sp")
            count: 每个地址读取的字节数 (默认 256)
            format: 格式 - "x"(hex), "s"(string), "i"(instruction/disasm), "p"(pointer), "b"(binary)
            
        Examples:
            lldb_memory_read_batch("0x100001234,0x100005678")
            lldb_memory_read_batch("$x0,$sp,$x1", count=64, format="x")
        """
        addr_list = [a.strip() for a in addresses.split(',') if a.strip()]
        if not addr_list:
            return "错误: addresses 为空"
        
        fmt_map = {'x': 'x', 's': 's', 'i': 'i', 'p': 'A', 'b': 'Y'}
        fmt = fmt_map.get(format, 'x')
        commands = [f'memory read -c {count} -f {fmt} {addr}' for addr in addr_list]
        logger.info(f"lldb_memory_read_batch: {len(addr_list)} 个地址: {addresses[:120]}")
        return call_bridge('execute_batch', commands=commands, labels=addr_list)
        
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
            JSON 结构包含:
            - stopped: bool - 是否已停止
            - reason: str - 停止原因 ("breakpoint", "breakpoint_condition_error", "watchpoint", "signal", "exception", "trace", "step_complete", "process_ended")
            - stop_description: str - LLDB 原始停止描述（包含详细错误信息）
            - frame_info: str - 当前帧信息
            
            当 reason="breakpoint" 时额外包含:
            - breakpoint_id: int - 断点编号
            - breakpoint_location_id: int - 断点位置编号
            - condition: str - 条件表达式（如果有）
            - condition_error: bool - 条件表达式是否求值出错
            
            当 reason="breakpoint_condition_error" 时:
            - condition_error: true
            - error_message: str - 详细错误信息（如 "undeclared identifier" 等语法错误）
            
            注意: reason="breakpoint_condition_error" 表示断点的条件表达式存在语法/求值错误，
            进程是因为表达式出错而停止的，并非条件真正匹配。此时应检查并修正条件表达式。
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
    
    # ---- 高频调试命令 ----
    
    @mcp.tool()
    def lldb_register_read(register: str = "") -> str:
        """
        读取寄存器值（支持批量）
        
        Args:
            register: 寄存器名，支持逗号分隔批量读取 (空=全部, "x0", "x0,x1,sp,pc" 等)
            
        Examples:
            lldb_register_read()              # 读取所有通用寄存器
            lldb_register_read("x0")          # 读取 x0
            lldb_register_read("x0,x1,x8,sp") # 批量读取多个寄存器
        """
        if register:
            # 支持逗号分隔的批量读取: "x0,x1,sp" → "register read x0 x1 sp"
            regs = ' '.join(r.strip() for r in register.split(',') if r.strip())
            return call_bridge('execute', command=f'register read {regs}')
        return call_bridge('execute', command='register read')
    
    @mcp.tool()
    def lldb_backtrace(count: int = 20) -> str:
        """
        获取调用栈
        
        Args:
            count: 显示的栈帧数量 (默认 20)
        """
        return call_bridge('execute', command=f'bt {count}')
    
    @mcp.tool()
    def lldb_breakpoint_set(address: str = "", name: str = "", condition: str = "", one_shot: bool = False) -> str:
        """
        设置断点
        
        Args:
            address: 地址断点 (如 "0x100001234")
            name: 符号/函数名断点 (如 "-[NSObject init]", "main")
            condition: 条件表达式 (如 "$x0 == 0x1234")
            one_shot: 是否一次性断点 (命中后自动删除)
            
        Examples:
            lldb_breakpoint_set(address="0x100001234")
            lldb_breakpoint_set(name="-[UIView setFrame:]")
            lldb_breakpoint_set(address="0x100001234", condition="$x2 < 0x500")
        """
        if not address and not name:
            return "错误: 必须指定 address 或 name"
        
        cmd = "breakpoint set"
        if address:
            cmd += f" -a {address}"
        if name:
            cmd += f' -n "{name}"'
        if condition:
            cmd += f" -c '{condition}'"
        if one_shot:
            cmd += " -o"
        return call_bridge('execute', command=cmd)
    
    @mcp.tool()
    def lldb_breakpoint_set_batch(addresses: str, condition: str = "", one_shot: bool = False) -> str:
        """
        批量设置断点（按地址）
        
        Args:
            addresses: 逗号分隔的地址列表 (如 "0x100001234,0x100005678,0x10000abcd")
            condition: 条件表达式，应用到所有断点 (如 "$x0 == 0x1234")
            one_shot: 是否一次性断点
            
        Examples:
            lldb_breakpoint_set_batch("0x100001234,0x100005678")
            lldb_breakpoint_set_batch("0x100001234,0x100005678", condition="$x0 > 0")
        """
        addr_list = [a.strip() for a in addresses.split(',') if a.strip()]
        if not addr_list:
            return "错误: addresses 为空"
        
        commands = []
        for addr in addr_list:
            cmd = f"breakpoint set -a {addr}"
            if condition:
                cmd += f" -c '{condition}'"
            if one_shot:
                cmd += " -o"
            commands.append(cmd)
        
        logger.info(f"lldb_breakpoint_set_batch: {len(addr_list)} 个地址: {addresses[:120]}")
        return call_bridge('execute_batch', commands=commands, labels=addr_list)
    
    @mcp.tool()
    def lldb_breakpoint_list() -> str:
        """列出所有断点"""
        return call_bridge('execute', command='breakpoint list')
    
    @mcp.tool()
    def lldb_breakpoint_delete(breakpoint_id: str = "") -> str:
        """
        删除断点
        
        Args:
            breakpoint_id: 断点ID (空=删除全部, "1"=删除1号, "1.2"=删除1号的第2个位置)
        """
        if breakpoint_id:
            return call_bridge('execute', command=f'breakpoint delete {breakpoint_id}')
        return call_bridge('execute', command='breakpoint delete')
    
    @mcp.tool()
    def lldb_breakpoint_delete_batch(breakpoint_ids: str) -> str:
        """
        批量删除断点
        
        Args:
            breakpoint_ids: 逗号分隔的断点ID列表 (如 "1,2,3" 或 "1.2,3.1")
            
        Examples:
            lldb_breakpoint_delete_batch("1,2,3")
            lldb_breakpoint_delete_batch("1.2,3.1,5")
        """
        id_list = [i.strip() for i in breakpoint_ids.split(',') if i.strip()]
        if not id_list:
            return "错误: breakpoint_ids 为空"
        
        commands = [f'breakpoint delete {bid}' for bid in id_list]
        logger.info(f"lldb_breakpoint_delete_batch: {len(id_list)} 个断点: {breakpoint_ids[:120]}")
        return call_bridge('execute_batch', commands=commands, labels=id_list)
    
    @mcp.tool()
    def lldb_image_list(filter: str = "") -> str:
        """
        列出已加载的模块/动态库 (用于计算基地址)
        
        Args:
            filter: 过滤关键字 (如 "libmtguard", "UIKit")
            
        Returns:
            模块列表，包含基地址
        """
        logger.info(f"lldb_image_list: filter='{filter}'")
        result = call_bridge('execute', command='image list')
        if filter and not result.startswith("错误"):
            # 在 Python 端过滤
            lines = result.split('\n')
            filtered = [l for l in lines if filter.lower() in l.lower()]
            filtered_result = '\n'.join(filtered) if filtered else f"未找到包含 '{filter}' 的模块"
            logger.info(f"lldb_image_list: 过滤后 {len(filtered)} 行")
            return filtered_result
        return result
    
    @mcp.tool()
    def lldb_expression(expr: str, lang: str = "c") -> str:
        """
        求值表达式
        
        Args:
            expr: 表达式
            lang: 语言 - "c", "objc", "swift"
            
        Examples:
            lldb_expression("$x0 + 0x10")
            lldb_expression("[NSString stringWithFormat:@\"test\"]", lang="objc")
        """
        lang_map = {"c": "c++", "objc": "objc", "swift": "swift"}
        l = lang_map.get(lang, "c++")
        return call_bridge('execute', command=f'expression -l {l} -- {expr}')
    
    print("[MCP] 启动中...", file=sys.stderr)
    logger.info("MCP Server 准备运行 (stdio 模式)")
    mcp.run(transport='stdio')


if __name__ == "__main__":
    if '--help' in sys.argv:
        print("lldbAiHelper MCP Server")
        print("")
        print("1. 在 lldb 中: command script import lldbAiHelper_MCP_bridge.py")
        print("2. 本文件由 Qoder 自动启动，或: python lldbAiHelper_MCP.py")
    else:
        run_mcp_server()
