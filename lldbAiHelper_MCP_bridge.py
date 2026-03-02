#!/usr/bin/env python3
"""
lldbAiHelper_MCP_bridge.py - LLDB Bridge (在 lldb 进程内运行)

功能：
    - 使用 lldb Python API 执行调试命令
    - Socket Server 接收来自 MCP Server 的请求
    - 不占用终端 stdio，支持手动/AI 协同调试

使用方式：
    (lldb) command script import /path/to/lldbAiHelper_MCP_bridge.py

加载后自动启动 Bridge 并注册命令：mcp_status, mcp_stop, mcp_restart

通信模式：短连接 + 并发线程
    - 每个请求新建一个 TCP 连接，处理完关闭
    - 多个请求可并发（如 wait_for_stop 和 lldb_stop 可同时进行）

协议 (JSON over TCP, 换行符分隔):
    请求: {"cmd": "execute", "args": {"command": "bt"}}
    响应: {"success": true, "result": "..."}
"""

import json
import os
import socket
import threading
import traceback
from typing import Optional, Dict, Any

# ============== 配置 ==============
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19527
MAX_PORT = 19537
PORT_FILE = os.path.expanduser("~/.lldb_mcp_port")

# lldb 模块（lldb 加载时注入）
lldb = None


class LLDBBridge:
    """
    LLDB Bridge - Socket Server (短连接 + 并发线程)
    """
    
    def __init__(self, debugger, host: str = DEFAULT_HOST):
        self.debugger = debugger
        self.host = host
        self.port = DEFAULT_PORT
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.exec_lock = threading.Lock()  # 保护 lldb 命令执行的串行化
        
    def start(self) -> bool:
        """启动 Socket Server（自动探测可用端口）"""
        if self.running:
            print(f"[Bridge] 已在运行: {self.host}:{self.port}")
            return True
            
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            
            # 自动探测可用端口
            bound = False
            for p in range(DEFAULT_PORT, MAX_PORT + 1):
                try:
                    self.server_socket.bind((self.host, p))
                    self.port = p
                    bound = True
                    break
                except OSError:
                    continue
                    
            if not bound:
                print(f"[Bridge] 端口 {DEFAULT_PORT}-{MAX_PORT} 均被占用")
                return False
                
            self.server_socket.listen(5)
            self.running = True
            
            # 写入握手文件
            with open(PORT_FILE, 'w') as f:
                f.write(str(self.port))
            
            thread = threading.Thread(target=self._accept_loop, daemon=True)
            thread.start()
            
            print(f"[Bridge] 已启动，监听 {self.host}:{self.port}")
            print(f"[Bridge] 端口已写入 {PORT_FILE}")
            return True
            
        except Exception as e:
            print(f"[Bridge] 启动失败: {e}")
            self.running = False
            return False
            
    def stop(self):
        """停止 Server"""
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass
            self.server_socket = None
        
        # 清理握手文件
        try:
            if os.path.exists(PORT_FILE):
                os.remove(PORT_FILE)
        except:
            pass
            
        print(f"[Bridge] 已停止，端口 {self.port} 已释放")
        
    def _accept_loop(self):
        """接受连接，每个连接 spawn 独立线程处理"""
        while self.running:
            try:
                self.server_socket.settimeout(1.0)
                client, addr = self.server_socket.accept()
                # 每个请求在独立线程处理（支持并发）
                t = threading.Thread(target=self._handle_request, args=(client,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                if self.running:
                    print(f"[Bridge] Accept 错误: {e}")
                    
    def _handle_request(self, client: socket.socket):
        """处理单个请求（短连接：处理完关闭）"""
        try:
            client.settimeout(300.0)  # 单个请求最长 5 分钟（wait_for_stop 等）
            
            # 接收完整请求
            buf = ""
            while '\n' not in buf:
                data = client.recv(65536)
                if not data:
                    return
                buf += data.decode('utf-8')
            
            line = buf.split('\n')[0].strip()
            if line:
                response = self._process_request(line)
                client.sendall((json.dumps(response, ensure_ascii=False) + '\n').encode('utf-8'))
        except Exception as e:
            if self.running:
                try:
                    err = json.dumps({'success': False, 'error': str(e)}) + '\n'
                    client.sendall(err.encode('utf-8'))
                except:
                    pass
        finally:
            try:
                client.close()
            except:
                pass
        
    def _process_request(self, request_str: str) -> Dict[str, Any]:
        """处理请求 → 分发到 _cmd_xxx 处理器"""
        try:
            request = json.loads(request_str)
            cmd = request.get('cmd', '')
            args = request.get('args', {})
            
            handler = getattr(self, f'_cmd_{cmd}', None)
            if handler:
                result = handler(**args)
                return {'success': True, 'result': result}
            else:
                return {'success': False, 'error': f'未知命令: {cmd}'}
                
        except json.JSONDecodeError as e:
            return {'success': False, 'error': f'JSON 解析错误: {e}'}
        except Exception as e:
            return {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
            
    # ============== 命令处理器 ==============
    
    def _cmd_ping(self) -> str:
        return "pong"
        
    def _cmd_execute(self, command: str) -> str:
        """执行 lldb 命令（同步模式 + 串行化）"""
        with self.exec_lock:
            orig_async = self.debugger.GetAsync()
            self.debugger.SetAsync(False)
            try:
                result = lldb.SBCommandReturnObject()
                self.debugger.GetCommandInterpreter().HandleCommand(command, result)
                
                output = ""
                if result.GetOutput():
                    output += result.GetOutput()
                if result.GetError():
                    output += result.GetError()
                return output.rstrip() if output.strip() else "[执行成功: 该命令无终端输出]"
            finally:
                self.debugger.SetAsync(orig_async)
            
    def _cmd_get_status(self) -> Dict[str, Any]:
        """获取结构化的调试状态"""
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                return {'has_target': False, 'message': '没有调试目标'}
                
            process = target.GetProcess()
            if not process.IsValid():
                return {
                    'has_target': True, 'has_process': False,
                    'target': target.GetExecutable().GetFilename() or 'unknown'
                }
                
            state = process.GetState()
            state_map = {
                lldb.eStateInvalid: "invalid", lldb.eStateUnloaded: "unloaded",
                lldb.eStateConnected: "connected", lldb.eStateAttaching: "attaching",
                lldb.eStateLaunching: "launching", lldb.eStateStopped: "stopped",
                lldb.eStateRunning: "running", lldb.eStateStepping: "stepping",
                lldb.eStateCrashed: "crashed", lldb.eStateDetached: "detached",
                lldb.eStateExited: "exited", lldb.eStateSuspended: "suspended"
            }
            
            info = {
                'has_target': True, 'has_process': True,
                'target': target.GetExecutable().GetFilename() or 'unknown',
                'pid': process.GetProcessID(),
                'state': state_map.get(state, str(state)),
                'num_threads': process.GetNumThreads()
            }
            
            if state == lldb.eStateStopped:
                thread = process.GetSelectedThread()
                if thread.IsValid():
                    frame = thread.GetSelectedFrame()
                    info['thread_id'] = thread.GetThreadID()
                    if frame.IsValid():
                        info['frame'] = str(frame)
            return info
            
    def _cmd_continue_async(self) -> Dict[str, Any]:
        """继续执行（异步，立即返回不阻塞）"""
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                return {'success': False, 'error': '没有调试目标'}
            process = target.GetProcess()
            if not process.IsValid():
                return {'success': False, 'error': '进程无效'}
            error = process.Continue()
            if error.Success():
                return {'success': True, 'message': '进程已继续执行'}
            return {'success': False, 'error': str(error)}
            
    def _cmd_stop_process(self) -> Dict[str, Any]:
        """暂停进程"""
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                return {'success': False, 'error': '没有调试目标'}
            process = target.GetProcess()
            if not process.IsValid():
                return {'success': False, 'error': '进程无效'}
            error = process.Stop()
            if error.Success():
                return {'success': True, 'message': '进程已暂停'}
            return {'success': False, 'error': str(error)}
                
    def _cmd_wait_for_stop(self, timeout: float = 30.0) -> Dict[str, Any]:
        """等待进程停止（不持有 exec_lock，允许并发 stop）"""
        import time
        target = self.debugger.GetSelectedTarget()
        if not target.IsValid():
            return {'stopped': False, 'error': '没有调试目标'}
        process = target.GetProcess()
        if not process.IsValid():
            return {'stopped': False, 'error': '进程无效'}
            
        start = time.time()
        while time.time() - start < timeout:
            state = process.GetState()
            if state == lldb.eStateStopped:
                thread = process.GetSelectedThread()
                reason = thread.GetStopReason() if thread.IsValid() else lldb.eStopReasonNone
                reason_map = {
                    lldb.eStopReasonBreakpoint: "breakpoint",
                    lldb.eStopReasonWatchpoint: "watchpoint",
                    lldb.eStopReasonSignal: "signal",
                    lldb.eStopReasonException: "exception",
                    lldb.eStopReasonTrace: "trace",
                    lldb.eStopReasonPlanComplete: "step_complete",
                }
                # 获取帧信息时才需要 lock
                with self.exec_lock:
                    frame_info = self._cmd_execute('frame info')
                return {
                    'stopped': True,
                    'reason': reason_map.get(reason, str(reason)),
                    'frame_info': frame_info
                }
            elif state in [lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached]:
                return {'stopped': True, 'reason': 'process_ended'}
            time.sleep(0.1)
            
        return {'stopped': False, 'error': f'等待超时 ({timeout}s)'}


# ============== 全局实例 ==============
_bridge: Optional[LLDBBridge] = None


# ============== lldb 命令 ==============

def __lldb_init_module(debugger, internal_dict):
    """lldb 加载入口"""
    global _bridge, lldb
    import lldb as lldb_module
    lldb = lldb_module
    
    _bridge = LLDBBridge(debugger)
    _bridge.start()
    
    for cmd in ['mcp_status', 'mcp_stop', 'mcp_restart']:
        debugger.HandleCommand(f'command script add -f {__name__}.{cmd} {cmd}')
    print("[Bridge] 命令已注册: mcp_status, mcp_stop, mcp_restart")


def mcp_status(debugger, command, result, internal_dict):
    if _bridge and _bridge.running:
        print(f"Bridge 运行中: {_bridge.host}:{_bridge.port}")
    else:
        print("Bridge 未运行")


def mcp_stop(debugger, command, result, internal_dict):
    global _bridge
    if _bridge:
        _bridge.stop()
        _bridge = None


def mcp_restart(debugger, command, result, internal_dict):
    global _bridge
    if _bridge:
        _bridge.stop()
    _bridge = LLDBBridge(debugger)
    _bridge.start()


if __name__ == "__main__":
    print("此脚本需在 lldb 内加载:")
    print("  (lldb) command script import /path/to/lldbAiHelper_MCP_bridge.py")
