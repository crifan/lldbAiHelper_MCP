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
import logging
from datetime import datetime
from typing import Optional, Dict, Any

# ============== 配置 ==============
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19527
MAX_PORT = 19537
PORT_FILE = os.path.expanduser("~/.lldb_mcp_port")

# 日志配置 - 按日期子目录存放
_now = datetime.now()
_date_str = _now.strftime('%Y%m%d')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", _date_str)
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"lldb_bridge_{_now.strftime('%Y%m%d_%H%M%S')}.log")

# 创建 handler 并设置立即刷新
_log_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
_log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
_log_handler.setLevel(logging.DEBUG)

logging.basicConfig(level=logging.DEBUG, handlers=[_log_handler])
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# 确保日志立即写入
class FlushHandler(logging.Handler):
    def emit(self, record):
        _log_handler.emit(record)
        _log_handler.flush()

logger.addHandler(FlushHandler())

# lldb 模块（lldb 加载时注入）
lldb = None


class LLDBBridge:
    """
    LLDB Bridge - Socket Server (短连接 + 并发线程)
    """
    
    # LLDB enum 映射表（类级别，只构建一次）
    # 使用 getattr 安全获取，不同 LLDB 版本可能缺少某些枚举
    _STOP_REASON_MAP = None
    _STATE_MAP = None
    
    @classmethod
    def _build_stop_reason_map(cls) -> Dict[int, str]:
        """运行时安全构建 stop reason 映射，getattr 避免 AttributeError"""
        if cls._STOP_REASON_MAP is not None:
            return cls._STOP_REASON_MAP
        entries = [
            ("eStopReasonInvalid", "invalid"),
            ("eStopReasonNone", "none"),
            ("eStopReasonTrace", "trace"),
            ("eStopReasonBreakpoint", "breakpoint"),
            ("eStopReasonWatchpoint", "watchpoint"),
            ("eStopReasonSignal", "signal"),
            ("eStopReasonException", "exception"),
            ("eStopReasonExec", "exec"),
            ("eStopReasonPlanComplete", "plan_complete"),
            ("eStopReasonThreadExiting", "thread_exiting"),
            ("eStopReasonThreadShouldExit", "thread_exit"),
            ("eStopReasonInstrumentation", "instrumentation"),
            ("eStopReasonProcessorTrace", "processor_trace"),
            ("eStopReasonFork", "fork"),
            ("eStopReasonVFork", "vfork"),
            ("eStopReasonVForkDone", "vfork_done"),
        ]
        result = {}
        for attr, label in entries:
            value = getattr(lldb, attr, None)
            if value is not None:
                result[value] = label
        cls._STOP_REASON_MAP = result
        return result
    
    @classmethod
    def _build_state_map(cls) -> Dict[int, str]:
        """运行时安全构建 process state 映射，getattr 避免 AttributeError"""
        if cls._STATE_MAP is not None:
            return cls._STATE_MAP
        entries = [
            ("eStateInvalid", "invalid"),
            ("eStateUnloaded", "unloaded"),
            ("eStateConnected", "connected"),
            ("eStateAttaching", "attaching"),
            ("eStateLaunching", "launching"),
            ("eStateStopped", "stopped"),
            ("eStateRunning", "running"),
            ("eStateStepping", "stepping"),
            ("eStateCrashed", "crashed"),
            ("eStateDetached", "detached"),
            ("eStateExited", "exited"),
            ("eStateSuspended", "suspended"),
        ]
        result = {}
        for attr, label in entries:
            value = getattr(lldb, attr, None)
            if value is not None:
                result[value] = label
        cls._STATE_MAP = result
        return result
    
    def __init__(self, debugger, host: str = DEFAULT_HOST):
        self.debugger = debugger
        self.host = host
        self.port = DEFAULT_PORT
        self.server_socket: Optional[socket.socket] = None
        self.running = False
        self.exec_lock = threading.Lock()  # 保护 lldb 命令执行的串行化
        self._process_continued = False   # 进程是否处于 continue 运行态（continue_async 后为 True，停止后为 False）
        
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
        """停止 Server（记录退出诊断信息）"""
        self.running = False
        
        # 记录退出诊断信息：LLDB 进程状态、最后命令等
        try:
            diag_parts = []
            if self.debugger:
                target = self.debugger.GetSelectedTarget()
                if target and target.IsValid():
                    process = target.GetProcess()
                    if process and process.IsValid():
                        state = process.GetState()
                        pid = process.GetProcessID()
                        diag_parts.append(f"pid={pid}, state={state}")
                        if state == getattr(lldb, 'eStateStopped', 6):
                            thread = process.GetSelectedThread()
                            if thread and thread.IsValid():
                                pc = thread.GetSelectedFrame().GetPC() if thread.GetSelectedFrame().IsValid() else 0
                                diag_parts.append(f"tid={thread.GetThreadID()}, pc=0x{pc:x}")
                    else:
                        diag_parts.append("process 无效")
                else:
                    diag_parts.append("target 无效")
            diag_str = ", ".join(diag_parts) if diag_parts else "无诊断信息"
            logger.warning(f"Bridge 正在停止, 诊断: {diag_str}")
            print(f"[Bridge] 退出诊断: {diag_str}")
        except Exception as e:
            logger.warning(f"Bridge 停止时诊断失败: {e}")
        
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
                # 增强错误归因：记录异常栈和 LLDB 状态快照
                tb = traceback.format_exc()
                logger.error(f"_handle_request 异常: {e}\n{tb}")
                try:
                    # 尝试获取 LLDB 进程状态用于诊断
                    diag = ""
                    try:
                        if self.debugger:
                            tgt = self.debugger.GetSelectedTarget()
                            if tgt and tgt.IsValid():
                                proc = tgt.GetProcess()
                                if proc and proc.IsValid():
                                    diag = f" [lldb state={proc.GetState()}, pid={proc.GetProcessID()}]"
                    except:
                        pass
                    err = json.dumps({'success': False, 'error': f'{e}{diag}', 'traceback': tb}) + '\n'
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
            
            logger.info(f"收到命令: {cmd}, 参数: {args}")
            
            handler = getattr(self, f'_cmd_{cmd}', None)
            if handler:
                result = handler(**args)
                logger.info(f"命令 {cmd} 执行结果: {result}")
                return {'success': True, 'result': result}
            else:
                logger.warning(f"未知命令: {cmd}")
                return {'success': False, 'error': f'未知命令: {cmd}'}
                
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}")
            return {'success': False, 'error': f'JSON 解析错误: {e}'}
        except Exception as e:
            logger.error(f"执行异常: {e}\n{traceback.format_exc()}")
            return {'success': False, 'error': str(e), 'traceback': traceback.format_exc()}
            
    # ============== 命令处理器 ==============
    
    def _cmd_ping(self) -> str:
        logger.debug("ping: pong")
        return "pong"
        
    def _cmd_execute(self, command: str) -> str:
        """执行 lldb 命令（串行化 + 智能 async 管理 + 超时保护）"""
        logger.info(f"execute: 执行命令 '{command}'")
        # 超时保护：如果另一个命令正在执行（如 step），不无限期等待
        if not self.exec_lock.acquire(timeout=10):
            logger.warning(f"execute: 无法获取 exec_lock (10s 超时)，命令 '{command}' 被拒绝")
            return "[错误: 另一个命令正在执行中，请稍后重试。如需中断，请使用 lldb_stop]"
        try:
            # 关键修复: 仅在进程非运行态时切换同步模式
            # 进程运行中切换 SetAsync 会导致 LLDB 内部事件处理状态不一致（state desync）
            should_force_sync = not self._process_continued
            orig_async = self.debugger.GetAsync()
            if should_force_sync:
                self.debugger.SetAsync(False)
            try:
                result = lldb.SBCommandReturnObject()
                self.debugger.GetCommandInterpreter().HandleCommand(command, result)
                
                output = ""
                if result.GetOutput():
                    output += result.GetOutput()
                if result.GetError():
                    output += result.GetError()
                final_output = output.rstrip() if output.strip() else "[执行成功: 该命令无终端输出]"
                logger.debug(f"execute: 命令 '{command}' 输出长度={len(final_output)}, force_sync={should_force_sync}")
                return final_output
            finally:
                if should_force_sync:
                    self.debugger.SetAsync(orig_async)
        finally:
            self.exec_lock.release()
    
    def _cmd_execute_batch(self, commands: list, labels: list = None) -> str:
        """批量执行多个 lldb 命令（一次锁获取，一次连接 + 超时保护）"""
        logger.info(f"execute_batch: 批量执行 {len(commands)} 个命令")
        if not self.exec_lock.acquire(timeout=10):
            logger.warning(f"execute_batch: 无法获取 exec_lock (10s 超时)，批量命令被拒绝")
            return "[错误: 另一个命令正在执行中，请稍后重试。如需中断，请使用 lldb_stop]"
        try:
            should_force_sync = not self._process_continued
            orig_async = self.debugger.GetAsync()
            if should_force_sync:
                self.debugger.SetAsync(False)
            try:
                results = []
                for i, command in enumerate(commands):
                    label = labels[i] if labels and i < len(labels) else command
                    result = lldb.SBCommandReturnObject()
                    self.debugger.GetCommandInterpreter().HandleCommand(command, result)
                    
                    output = ""
                    if result.GetOutput():
                        output += result.GetOutput()
                    if result.GetError():
                        output += result.GetError()
                    output = output.rstrip() if output.strip() else "[无输出]"
                    results.append(f"=== {label} ===\n{output}")
                
                combined = "\n\n".join(results)
                logger.debug(f"execute_batch: {len(commands)} 个命令完成, 总输出长度={len(combined)}")
                return combined
            finally:
                if should_force_sync:
                    self.debugger.SetAsync(orig_async)
        finally:
            self.exec_lock.release()
            
    def _cmd_get_status(self) -> Dict[str, Any]:
        """
        获取结构化的调试状态（只读操作，不持有 exec_lock）
        
        LLDB SB API 的读操作（GetState/GetProcess/GetThread 等）是线程安全的，
        不需要 exec_lock 保护。去掉锁后，status 可在步骤命令执行期间并发查询。
        """
        logger.info("get_status: 获取调试状态")
        # 只读操作不需要 exec_lock，LLDB SB API 的读操作是线程安全的
        target = self.debugger.GetSelectedTarget()
        if not target.IsValid():
            logger.warning("get_status: 没有调试目标")
            return {'has_target': False, 'message': '没有调试目标'}
            
        process = target.GetProcess()
        if not process.IsValid():
            logger.warning("get_status: 进程无效")
            return {
                'has_target': True, 'has_process': False,
                'target': target.GetExecutable().GetFilename() or 'unknown'
            }
            
        state = process.GetState()
        state_map = self._build_state_map()
        
        info = {
            'has_target': True, 'has_process': True,
            'target': target.GetExecutable().GetFilename() or 'unknown',
            'pid': process.GetProcessID(),
            'state': state_map.get(state, str(state)),
            'num_threads': process.GetNumThreads()
        }
        
        if state == getattr(lldb, 'eStateStopped', 6):
            thread = process.GetSelectedThread()
            if thread.IsValid():
                frame = thread.GetSelectedFrame()
                info['thread_id'] = thread.GetThreadID()
                if frame.IsValid():
                    info['frame'] = str(frame)
        
        logger.info(f"get_status: state={info.get('state')}, pid={info.get('pid')}")
        return info
            
    def _cmd_continue_async(self) -> Dict[str, Any]:
        """继续执行（异步，立即返回不阻塞）"""
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                logger.error("continue_async: 没有调试目标")
                return {'success': False, 'error': '没有调试目标'}
            process = target.GetProcess()
            if not process.IsValid():
                logger.error("continue_async: 进程无效")
                return {'success': False, 'error': '进程无效'}
            
            # 记录当前状态
            state_before = process.GetState()
            logger.info(f"continue_async: 执行前状态={state_before}, pid={process.GetProcessID()}")
            
            # 关键：必须设为异步模式，否则 HandleCommand("continue") 会阻塞等待进程停止
            orig_async = self.debugger.GetAsync()
            logger.info(f"continue_async: orig_async={orig_async}, 设置为 True")
            self.debugger.SetAsync(True)
            
            try:
                # 关键修复: 使用 HandleCommand 而非 process.Continue()
                # 通过命令解释器执行 continue 确保:
                # 1. 断点回调 (breakpoint command add -F/-o) 能被正确调度
                # 2. LLDB 内部事件处理链完整（包括 auto-continue 逻辑）
                cmd_result = lldb.SBCommandReturnObject()
                self.debugger.GetCommandInterpreter().HandleCommand('process continue', cmd_result)
                
                state_after = process.GetState()
                state_map = self._build_state_map()
                state_str = state_map.get(state_after, str(state_after))
                logger.info(f"continue_async: HandleCommand('process continue') succeeded={cmd_result.Succeeded()}, 状态={state_after}({state_str})")
                
                if not cmd_result.Succeeded():
                    error_msg = cmd_result.GetError() or '继续执行失败'
                    logger.error(f"continue_async: 失败: {error_msg}")
                    return {'success': False, 'error': error_msg}
                
                # 关键校验: HandleCommand 返回成功不代表进程真的 resume 了
                # LLDB 有时 "process continue" 返回 Succeeded 但进程实际仍处于 stopped（如 auto-continue 回调失败）
                if state_after == getattr(lldb, 'eStateStopped', 6):
                    logger.warning(f"continue_async: HandleCommand 成功但进程仍为 stopped，可能未真正 resume")
                    self._process_continued = False
                    # 尝试获取停止原因，帮助调用方诊断
                    thread = process.GetSelectedThread()
                    stop_desc = thread.GetStopDescription(256) if thread and thread.IsValid() else ""
                    return {
                        'success': False, 'did_not_resume': True,
                        'error': '进程未恢复运行，仍处于 stopped 状态',
                        'state': state_str, 'stop_description': stop_desc
                    }
                
                # 进程确实进入了 running/stepping 状态
                self._process_continued = True
                return {'success': True, 'message': '进程已继续执行', 'state': state_str}
            except Exception as e:
                logger.error(f"continue_async: 异常: {e}", exc_info=True)
                return {'success': False, 'error': str(e)}
    
    def _cmd_step_async(self, action: str) -> Dict[str, Any]:
        """
        执行单步命令（异步，立即返回不阻塞）
        
        与 continue_async 同理：SetAsync(True) 让 HandleCommand 立即返回，
        调用方用 wait_for_stop 等待步骤完成。
        这样 exec_lock 在步骤执行期间不会被持有，其他命令（stop/status）可并发执行。
        """
        logger.info(f"step_async: 执行步骤 '{action}'")
        
        # 统一映射：用户侧名称 → lldb 命令
        action_map = {
            "next": "next", "n": "next",
            "step": "step", "s": "step",
            "finish": "finish",
            "nexti": "nexti", "ni": "nexti",
            "stepi": "stepi", "si": "stepi",
        }
        lldb_cmd = action_map.get(action.lower())
        if not lldb_cmd:
            return {'success': False, 'error': f'无效动作: {action}。可选: next, step, finish, nexti, stepi'}
        
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                logger.error("step_async: 没有调试目标")
                return {'success': False, 'error': '没有调试目标'}
            process = target.GetProcess()
            if not process.IsValid():
                logger.error("step_async: 进程无效")
                return {'success': False, 'error': '进程无效'}
            
            state_before = process.GetState()
            logger.info(f"step_async: 执行前状态={state_before}, action={action}")
            
            # 关键：必须设为异步模式，否则 HandleCommand 会阻塞等待步骤完成
            self.debugger.SetAsync(True)
            
            try:
                cmd_result = lldb.SBCommandReturnObject()
                self.debugger.GetCommandInterpreter().HandleCommand(lldb_cmd, cmd_result)
                
                if cmd_result.Succeeded():
                    self._process_continued = True
                    logger.info(f"step_async: 步骤 '{action}' 已发出")
                    return {'success': True, 'action': action, 'message': f'步骤 {action} 已执行，请用 wait_for_stop 等待完成'}
                
                error_msg = cmd_result.GetError() or f'步骤执行失败: {lldb_cmd}'
                logger.error(f"step_async: 失败: {error_msg}")
                return {'success': False, 'error': error_msg}
            except Exception as e:
                logger.error(f"step_async: 异常: {e}", exc_info=True)
                return {'success': False, 'error': str(e)}
            
    def _cmd_stop_process(self) -> Dict[str, Any]:
        """
        暂停进程（不持有 exec_lock，允许中断正在执行的命令）
        
        关键设计：process.Stop() 是安全的并发操作，不需要 exec_lock。
        这样即使 step_async/execute 正在执行（持有 exec_lock），
        stop_process 仍可通过新连接并发送达并执行，实现可抢占的 process interrupt。
        """
        logger.info("stop_process: 尝试暂停进程")
        # 不使用 exec_lock，允许在另一个命令执行期间中断进程
        target = self.debugger.GetSelectedTarget()
        if not target.IsValid():
            logger.error("stop_process: 没有调试目标")
            return {'success': False, 'error': '没有调试目标'}
        process = target.GetProcess()
        if not process.IsValid():
            logger.error("stop_process: 进程无效")
            return {'success': False, 'error': '进程无效'}
        
        state_before = process.GetState()
        logger.info(f"stop_process: 执行前状态={state_before}")
        
        error = process.Stop()
        state_after = process.GetState()
        logger.info(f"stop_process: Stop() 返回 error.Success()={error.Success()}, 状态={state_after}")
        
        if error.Success():
            # 恢复同步模式，标记进程已停止
            self._process_continued = False
            self.debugger.SetAsync(False)
            return {'success': True, 'message': '进程已暂停'}
        
        # halt 失败（如 "Halt timed out"），附带进程诊断帮助判断原因
        state_map = self._build_state_map()
        diag = {
            'pid': process.GetProcessID(),
            'state': state_map.get(state_after, str(state_after)),
            'num_threads': process.GetNumThreads(),
        }
        # 检查 target 是否还活着
        target_alive = target.IsValid()
        diag['target_alive'] = target_alive
        
        logger.warning(f"stop_process: Stop() 失败: {error}, 诊断: {diag}")
        return {
            'success': False,
            'error': str(error),
            'diagnostics': diag,
            'hint': 'Halt timed out 可能原因: stale process (已退出但状态未刷新)、remote debugserver 卡死、调试服务状态不一致',
        }
                
    def _cmd_wait_for_stop(self, timeout: float = 30.0) -> Dict[str, Any]:
        """等待进程停止（不持有 exec_lock，允许并发 stop）"""
        import time
        logger.info(f"wait_for_stop: 开始等待, timeout={timeout}")
        
        target = self.debugger.GetSelectedTarget()
        if not target.IsValid():
            logger.error("wait_for_stop: 没有调试目标")
            return {'stopped': False, 'error': '没有调试目标'}
        process = target.GetProcess()
        if not process.IsValid():
            logger.error("wait_for_stop: 进程无效")
            return {'stopped': False, 'error': '进程无效'}
        
        # 使用 lldb 的事件监听机制，而不是轮询 GetState()
        listener = lldb.SBListener("wait_for_stop_listener")
        process.GetBroadcaster().AddListener(listener, lldb.SBProcess.eBroadcastBitStateChanged)
        
        # 运行时安全构建映射（getattr 避免 AttributeError）
        reason_map = self._build_stop_reason_map()
        state_map = self._build_state_map()
        
        # 预取常用枚举值（getattr 安全兜底）
        STATE_STOPPED = getattr(lldb, 'eStateStopped', 6)
        STATE_EXITED = getattr(lldb, 'eStateExited', 10)
        STATE_CRASHED = getattr(lldb, 'eStateCrashed', 8)
        STATE_DETACHED = getattr(lldb, 'eStateDetached', 9)
        REASON_BREAKPOINT = getattr(lldb, 'eStopReasonBreakpoint', 2)
        REASON_NONE = getattr(lldb, 'eStopReasonNone', 0)
        
        start = time.time()
        while time.time() - start < timeout:
            # 先检查当前状态
            state = process.GetState()
            logger.debug(f"wait_for_stop: 当前状态={state}")
            
            if state == STATE_STOPPED:
                # 关键修复: 等待一小段时间，确认不是 auto-continue 的瞬时停止
                # auto-continue 断点或回调返回 False 会让进程瞬时停止后立即恢复运行
                # 如果不做此检查，会误报为真正停止，后续命令与实际运行状态不一致（state desync）
                time.sleep(0.05)  # 50ms 等待 auto-continue 生效
                recheck_state = process.GetState()
                if recheck_state != STATE_STOPPED:
                    logger.info(f"wait_for_stop: 瞬时停止后恢复运行 (state={recheck_state}), 继续等待 (auto-continue)")
                    continue
                
                # 恢复同步模式，标记进程已停止
                self._process_continued = False
                self.debugger.SetAsync(False)
                
                thread = process.GetSelectedThread()
                reason = thread.GetStopReason() if thread.IsValid() else REASON_NONE
                logger.info(f"wait_for_stop: 进程已停止, reason={reason}")
                
                # 获取详细停止描述（关键：包含条件表达式错误等信息）
                stop_description = ""
                if thread.IsValid():
                    stop_description = thread.GetStopDescription(1024) or ""
                logger.info(f"wait_for_stop: stop_description='{stop_description}'")
                
                # 直接用 SBFrame API 获取信息，不调用 lldb 命令（避免阻塞）
                frame_info = ""
                if thread.IsValid():
                    frame = thread.GetSelectedFrame()
                    if frame.IsValid():
                        pc = frame.GetPC()
                        func = frame.GetFunctionName() or "unknown"
                        module = frame.GetModule()
                        mod_name = module.GetFileSpec().GetFilename() if module.IsValid() else "unknown"
                        frame_info = f"pc=0x{pc:x}, func={func}, module={mod_name}"
                
                # 构建基本返回结果（强制包含结构化字段）
                # reason 映射：未知值用 "unknown_N" 格式，避免返回裸数字如 "1"
                reason_str = reason_map.get(reason, f"unknown_{reason}")
                
                # 提取结构化字段
                thread_id = None
                pc = None
                if thread.IsValid():
                    thread_id = thread.GetThreadID()
                    frame = thread.GetSelectedFrame()
                    if frame.IsValid():
                        pc = frame.GetPC()
                
                result = {
                    'stopped': True,
                    'reason': reason_str,
                    'stop_description': stop_description,
                    'frame_info': frame_info,
                    # 强制结构化字段（确保 AI 端总能拿到，无需从 frame_info 解析）
                    'thread_id': thread_id,
                    'pc': f"0x{pc:x}" if pc is not None else None,
                    'process_state': state_map.get(state, str(state)),
                }
                
                # 针对断点停止，进一步区分条件断点的情况
                if reason == REASON_BREAKPOINT and thread.IsValid():
                    bp_detail = self._get_breakpoint_stop_detail(thread, target, stop_description)
                    result.update(bp_detail)
                
                logger.info(f"wait_for_stop: 返回 result={result}")
                return result
            elif state in [STATE_EXITED, STATE_CRASHED, STATE_DETACHED]:
                self._process_continued = False
                self.debugger.SetAsync(False)
                logger.info(f"wait_for_stop: 进程已结束, state={state}")
                return {
                    'stopped': True,
                    'reason': 'process_ended',
                    'process_state': state_map.get(state, str(state)),
                }
            
            # 等待事件（比轮询更可靠）
            event = lldb.SBEvent()
            if listener.WaitForEvent(1, event):  # 等待最多1秒
                if lldb.SBProcess.EventIsProcessEvent(event):
                    new_state = lldb.SBProcess.GetStateFromEvent(event)
                    logger.info(f"wait_for_stop: 收到事件, new_state={new_state}")
            
        # 超时：恢复状态
        self._process_continued = False
        self.debugger.SetAsync(False)
        logger.warning(f"wait_for_stop: 超时 ({timeout}s)")
        return {'stopped': False, 'error': f'等待超时 ({timeout}s)'}
    
    def _get_breakpoint_stop_detail(self, thread, target, stop_description: str) -> Dict[str, Any]:
        """
        针对断点停止，提取详细信息：
        - 区分条件断点正常命中 vs 条件表达式求值错误
        - 返回断点ID、条件表达式等
        """
        detail = {}
        
        try:
            # 从 StopReasonData 获取断点 ID 和 location ID
            # index 0 = breakpoint id, index 1 = location id
            bp_id = thread.GetStopReasonDataAtIndex(0)
            bp_loc_id = thread.GetStopReasonDataAtIndex(1)
            detail['breakpoint_id'] = int(bp_id)
            detail['breakpoint_location_id'] = int(bp_loc_id)
            
            # 获取断点对象，提取条件表达式
            bp = target.FindBreakpointByID(int(bp_id))
            if bp and bp.IsValid():
                condition = bp.GetCondition()
                if condition:
                    detail['condition'] = condition
            
            # 检测条件表达式求值错误
            # LLDB 在条件表达式出错时，stop_description 包含 "error evaluating condition" 等关键信息
            # 同时也通过 GetStopDescription 获取完整错误描述
            is_condition_error = False
            
            # 方法1: 检查 stop_description 是否包含条件错误关键词
            desc_lower = stop_description.lower()
            condition_error_indicators = [
                "error evaluating condition",
                "couldn't parse conditional expression",
                "stopped due to an error evaluating condition",
            ]
            for indicator in condition_error_indicators:
                if indicator in desc_lower:
                    is_condition_error = True
                    break
            
            # 方法2: 使用 lldb 命令获取更详细的错误信息（如果 stop_description 不够详细）
            if not is_condition_error and detail.get('condition'):
                # 有条件表达式的断点，通过 'thread info' 命令获取更详细信息
                try:
                    result = lldb.SBCommandReturnObject()
                    self.debugger.GetCommandInterpreter().HandleCommand('thread info', result)
                    thread_info_output = ""
                    if result.GetOutput():
                        thread_info_output += result.GetOutput()
                    if result.GetError():
                        thread_info_output += result.GetError()
                    
                    thread_info_lower = thread_info_output.lower()
                    for indicator in condition_error_indicators:
                        if indicator in thread_info_lower:
                            is_condition_error = True
                            detail['thread_info'] = thread_info_output.strip()
                            break
                except Exception as e:
                    logger.warning(f"_get_breakpoint_stop_detail: thread info 执行失败: {e}")
            
            if is_condition_error:
                detail['reason'] = "breakpoint_condition_error"
                detail['condition_error'] = True
                detail['error_message'] = stop_description
                logger.warning(f"_get_breakpoint_stop_detail: 条件表达式求值错误! bp_id={bp_id}, condition={detail.get('condition')}, desc={stop_description}")
            else:
                detail['condition_error'] = False
                
        except Exception as e:
            logger.error(f"_get_breakpoint_stop_detail 异常: {e}\n{traceback.format_exc()}")
            
        return detail


# ============== 全局实例 ==============
_bridge: Optional[LLDBBridge] = None


# ============== lldb 命令 ==============

def __lldb_init_module(debugger, internal_dict):
    """lldb 加载入口"""
    global _bridge, lldb
    import lldb as lldb_module
    lldb = lldb_module
    
    logger.info(f"Bridge 初始化开始, LOG_FILE={LOG_FILE}")
    
    # 自动确认所有需要用户输入的命令（如 breakpoint delete）
    # 避免 AI 调用时阻塞等待用户输入
    debugger.HandleCommand('settings set auto-confirm true')
    
    _bridge = LLDBBridge(debugger)
    _bridge.start()
    
    for cmd in ['mcp_status', 'mcp_stop', 'mcp_restart']:
        debugger.HandleCommand(f'command script add -f {__name__}.{cmd} {cmd}')
    print("[Bridge] 命令已注册: mcp_status, mcp_stop, mcp_restart")
    print("[Bridge] 已设置 auto-confirm=true (自动确认删除等操作)")
    print(f"[Bridge] 日志文件: {LOG_FILE}")
    logger.info("Bridge 初始化完成")


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
