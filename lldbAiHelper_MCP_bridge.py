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
import re
import socket
import subprocess
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
    _ANDROID_PACKAGE_RE = re.compile(
        r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+(?::[A-Za-z0-9_./-]+)?$'
    )
    _INVALID_PID_VALUES = frozenset({0, -1, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF})

    # 会长时间阻塞 exec_lock 的 LLDB 命令（同步模式下 HandleCommand 会等进程停止才返回）
    # 这些命令必须走专用的 lldb_continue / lldb_flow_control 异步路径，
    # 否则会拖住后续所有 lldb_execute 调用。
    _BLOCKING_SINGLE_ALIASES = frozenset({
        # continue
        'c', 'co', 'con', 'cont', 'conti', 'continu', 'continue',
        # step (源码级 step-in)
        's', 'st', 'ste', 'step',
        # next (源码级 step-over)
        'n', 'ne', 'nex', 'next',
        # finish (step-out)
        'fin', 'fini', 'finis', 'finish',
        # nexti (汇编级 step-over)
        'ni', 'nexti',
        # stepi (汇编级 step-in)
        'si', 'stepi',
        # run / launch (仅启动进程时使用，正常不会出现在交互调试中)
        'r', 'ru', 'run',
    })

    _BLOCKING_COMPOUND_PREFIXES = frozenset({
        ('process', 'continue'),
        ('process', 'launch'),
        ('process', 'step-out'),
        ('thread', 'continue'),
        ('thread', 'step-in'),
        ('thread', 'step-over'),
        ('thread', 'step-out'),
        ('thread', 'step-inst'),
        ('thread', 'step-inst-over'),
    })

    _BLOCKING_HELP_HINT = (
        "[错误: lldb_execute 拒绝执行长阻塞命令 '{matched}'。\n"
        "原因: 此类命令会让 LLDB 同步等待进程停止，期间会一直持有 exec_lock，\n"
        "导致后续所有 lldb_execute / lldb_flow_control / lldb_continue 调用被拒绝或超时。\n"
        "请改用下列专用异步 MCP 工具:\n"
        "  - continue (c) / process continue / thread continue\n"
        "      → lldb_continue + lldb_wait_stop\n"
        "  - step (s) / next (n) / nexti (ni) / stepi (si) / finish\n"
        "      → lldb_flow_control(action=\"step\"/\"next\"/\"nexti\"/\"stepi\"/\"finish\") + lldb_wait_stop\n"
        "  - run (r) / process launch\n"
        "      → 一般在 LLDB 控制台手动启动，不走 MCP\n"
        "如果当前 exec_lock 已被占用、lldb_execute 全部返回“另一个命令正在执行中”:\n"
        "  请调用 lldb_stop 中断进程（lldb_stop / lldb_status 不依赖 exec_lock，可随时抢占）。]"
    )

    _INTERRUPT_SINGLE_ALIASES = frozenset({'interrupt', 'halt'})
    _INTERRUPT_COMPOUND_PREFIXES = frozenset({
        ('process', 'interrupt'),
        ('process', 'halt'),
    })

    @classmethod
    def _detect_blocking_command(cls, command: str) -> Optional[str]:
        """
        检测一条 LLDB 命令是否属于会长时间阻塞 exec_lock 的类型。
        返回命中的命令名（用于错误提示），未命中返回 None。

        仅凭首词 / 首+次词判断，不处理 “;” 多命令颗粒场景（LLDB 本身不支持）。
        """
        if not command:
            return None
        tokens = command.strip().split()
        if not tokens:
            return None
        first = tokens[0].lower()
        second = tokens[1].lower() if len(tokens) > 1 else None

        if first in cls._BLOCKING_SINGLE_ALIASES:
            return first
        if second is not None and (first, second) in cls._BLOCKING_COMPOUND_PREFIXES:
            return f"{first} {second}"
        return None

    @classmethod
    def _detect_interrupt_command(cls, command: str) -> Optional[str]:
        """识别会触发 process halt/interrupt 的命令，统一改走安全 stop_process 路径。"""
        if not command:
            return None
        tokens = command.strip().split()
        if not tokens:
            return None
        first = tokens[0].lower()
        second = tokens[1].lower() if len(tokens) > 1 else None

        if first in cls._INTERRUPT_SINGLE_ALIASES:
            return first
        if second is not None and (first, second) in cls._INTERRUPT_COMPOUND_PREFIXES:
            return f"{first} {second}"
        return None

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

    @classmethod
    def _state_label(cls, state: int) -> str:
        return cls._build_state_map().get(state, str(state))

    @classmethod
    def _is_process_ended_state(cls, state: int) -> bool:
        ended_states = {
            getattr(lldb, 'eStateExited', 10),
            getattr(lldb, 'eStateCrashed', 8),
            getattr(lldb, 'eStateDetached', 9),
            getattr(lldb, 'eStateInvalid', 0),
            getattr(lldb, 'eStateUnloaded', 1),
        }
        return state in ended_states

    @classmethod
    def _is_android_package_candidate(cls, value: str) -> bool:
        if not value:
            return False
        value = value.strip()
        if value in {'unknown', '<unknown>'}:
            return False
        if '/' in value or value.endswith('.so'):
            return False
        return bool(cls._ANDROID_PACKAGE_RE.match(value))

    @staticmethod
    def _safe_get_filename(filespec) -> Optional[str]:
        try:
            if filespec and filespec.IsValid():
                return filespec.GetFilename()
        except Exception:
            pass
        return None

    def _infer_android_package_name(self, target, process) -> Optional[str]:
        """尽力从 LLDB target/process 元数据推断 Android 包名，失败时返回 None。"""
        candidates = []

        try:
            process_info = process.GetProcessInfo()
            if process_info:
                for method_name in ('GetName', 'GetExecutableFile'):
                    try:
                        value = getattr(process_info, method_name)()
                        if method_name == 'GetExecutableFile':
                            value = self._safe_get_filename(value)
                        if value:
                            candidates.append(str(value))
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            executable = target.GetExecutable()
            filename = self._safe_get_filename(executable)
            if filename:
                candidates.append(filename)
        except Exception:
            pass

        for candidate in candidates:
            if self._is_android_package_candidate(candidate):
                return candidate
        return None

    @staticmethod
    def _safe_platform_name(target) -> Optional[str]:
        try:
            platform = target.GetPlatform()
            if platform and platform.IsValid():
                return platform.GetName()
        except Exception:
            pass
        return None

    @staticmethod
    def _safe_target_name(target) -> str:
        try:
            executable = target.GetExecutable()
            if executable and executable.IsValid():
                return executable.GetFilename() or 'unknown'
        except Exception:
            pass
        return 'unknown'

    def _get_adb_pid_for_package(self, package_name: str, adb_serial: str = "", timeout: float = 2.0) -> Dict[str, Any]:
        """通过 adb pidof 查询 Android 当前进程 PID。失败时返回 checked=False，不阻断 LLDB 操作。"""
        package_name = (package_name or "").strip()
        if not package_name:
            return {'checked': False, 'error': '未提供 Android package_name'}

        adb_path = os.environ.get('ADB', 'adb')
        command = [adb_path]
        adb_serial = (adb_serial or os.environ.get('ANDROID_SERIAL') or "").strip()
        if adb_serial:
            command.extend(['-s', adb_serial])
        command.extend(['shell', 'pidof', package_name])

        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return {
                'checked': False,
                'package_name': package_name,
                'adb_serial': adb_serial or None,
                'error': f'adb 不存在: {adb_path}',
            }
        except subprocess.TimeoutExpired:
            return {
                'checked': False,
                'package_name': package_name,
                'adb_serial': adb_serial or None,
                'error': f'adb pidof 超时 ({timeout}s)',
            }
        except Exception as e:
            return {
                'checked': False,
                'package_name': package_name,
                'adb_serial': adb_serial or None,
                'error': f'adb pidof 异常: {e}',
            }

        stdout = (completed.stdout or '').strip()
        stderr = (completed.stderr or '').strip()
        pids = []
        for token in stdout.replace(',', ' ').split():
            try:
                pids.append(int(token))
            except ValueError:
                pass

        return {
            'checked': True,
            'package_name': package_name,
            'adb_serial': adb_serial or None,
            'returncode': completed.returncode,
            'stdout': stdout,
            'stderr': stderr,
            'pid': pids[0] if pids else None,
            'pids': pids,
        }

    def _get_adb_process_for_pid(self, pid: int, adb_serial: str = "", timeout: float = 2.0) -> Dict[str, Any]:
        """不依赖包名，仅检查 LLDB PID 在 Android 设备上是否还存在。"""
        if pid in self._INVALID_PID_VALUES:
            return {'checked': False, 'pid': pid, 'error': '无效 PID'}

        adb_path = os.environ.get('ADB', 'adb')
        adb_serial = (adb_serial or os.environ.get('ANDROID_SERIAL') or "").strip()
        base_command = [adb_path]
        if adb_serial:
            base_command.extend(['-s', adb_serial])

        last_error = None
        for ps_args in (['shell', 'ps', '-A'], ['shell', 'ps']):
            command = base_command + ps_args
            try:
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except FileNotFoundError:
                return {
                    'checked': False,
                    'pid': pid,
                    'adb_serial': adb_serial or None,
                    'error': f'adb 不存在: {adb_path}',
                }
            except subprocess.TimeoutExpired:
                last_error = f"adb {' '.join(ps_args)} 超时 ({timeout}s)"
                continue
            except Exception as e:
                last_error = f"adb {' '.join(ps_args)} 异常: {e}"
                continue

            stdout = completed.stdout or ''
            stderr = (completed.stderr or '').strip()
            if completed.returncode != 0 and not stdout.strip():
                last_error = stderr or f"adb {' '.join(ps_args)} returncode={completed.returncode}"
                continue

            pid_str = str(pid)
            for line in stdout.splitlines():
                tokens = line.split()
                if not tokens:
                    continue
                if pid_str in tokens[:4]:
                    return {
                        'checked': True,
                        'pid': pid,
                        'pid_exists': True,
                        'process_name': tokens[-1] if tokens else None,
                        'process_line': line.strip(),
                        'adb_serial': adb_serial or None,
                    }

            return {
                'checked': True,
                'pid': pid,
                'pid_exists': False,
                'adb_serial': adb_serial or None,
                'stderr': stderr,
            }

        return {
            'checked': False,
            'pid': pid,
            'adb_serial': adb_serial or None,
            'error': last_error or 'adb ps 失败',
        }

    def _collect_process_diagnostics(
        self,
        target,
        process,
        package_name: str = "",
        adb_serial: str = "",
        include_adb: bool = True,
    ) -> Dict[str, Any]:
        """收集 halt/interrupt 前后的 LLDB + ADB 诊断信息。"""
        state = process.GetState()
        lldb_pid = process.GetProcessID()
        explicit_package = (package_name or "").strip()
        resolved_package = explicit_package or self._infer_android_package_name(target, process)
        platform_name = self._safe_platform_name(target)
        should_check_adb = (
            include_adb
            and lldb_pid not in self._INVALID_PID_VALUES
            and not self._is_process_ended_state(state)
        )

        diag = {
            'target_alive': target.IsValid(),
            'process_valid': process.IsValid(),
            'target': self._safe_target_name(target),
            'platform': platform_name,
            'lldb_pid': lldb_pid,
            'pid': lldb_pid,
            'state': self._state_label(state),
            'state_value': state,
            'num_threads': process.GetNumThreads(),
            'package_name': resolved_package,
            'package_name_source': 'explicit' if explicit_package else ('inferred' if resolved_package else None),
        }

        if should_check_adb and resolved_package:
            adb_diag = self._get_adb_pid_for_package(resolved_package, adb_serial=adb_serial)
            diag['adb_checked'] = adb_diag.get('checked', False)
            diag['adb_pid'] = adb_diag.get('pid')
            diag['adb_current_pid'] = adb_diag.get('pid')
            diag['adb_pids'] = adb_diag.get('pids', [])
            diag['adb_serial'] = adb_diag.get('adb_serial')
            diag['adb_error'] = adb_diag.get('error')
            diag['adb_returncode'] = adb_diag.get('returncode')
            diag['adb_stdout'] = adb_diag.get('stdout')
            diag['adb_stderr'] = adb_diag.get('stderr')
        else:
            diag['adb_checked'] = False
            diag['adb_pid'] = None
            diag['adb_current_pid'] = None
            diag['adb_pids'] = []
            diag['adb_serial'] = (adb_serial or os.environ.get('ANDROID_SERIAL') or "").strip() or None
            diag['adb_error'] = '未提供或无法推断 Android package_name'

        diag['adb_lldb_pid_checked'] = False
        diag['adb_lldb_pid_alive'] = None
        if should_check_adb and not resolved_package and platform_name and 'android' in platform_name.lower():
            pid_diag = self._get_adb_process_for_pid(lldb_pid, adb_serial=adb_serial)
            diag['adb_lldb_pid_checked'] = pid_diag.get('checked', False)
            diag['adb_lldb_pid_alive'] = pid_diag.get('pid_exists')
            diag['adb_lldb_process_name'] = pid_diag.get('process_name')
            diag['adb_lldb_process_line'] = pid_diag.get('process_line')
            diag['adb_lldb_pid_error'] = pid_diag.get('error')

        return diag

    def _interrupt_failure_payload(self, reason: str, diagnostics: Dict[str, Any], error: str) -> Dict[str, Any]:
        return {
            'success': False,
            'reason': reason,
            'error': error,
            'lldb_pid': diagnostics.get('lldb_pid'),
            'adb_pid': diagnostics.get('adb_pid'),
            'adb_current_pid': diagnostics.get('adb_current_pid'),
            'state': diagnostics.get('state'),
            'diagnostics': diagnostics,
        }

    def _preflight_interrupt(self, target, process, package_name: str = "", adb_serial: str = "") -> Optional[Dict[str, Any]]:
        """
        halt/interrupt 前检查 target 是否已经结束或指向旧 PID。
        返回 None 表示未发现 stale/ended，可以继续调用 process.Stop()。
        """
        diagnostics = self._collect_process_diagnostics(
            target,
            process,
            package_name=package_name,
            adb_serial=adb_serial,
            include_adb=True,
        )
        lldb_pid = diagnostics.get('lldb_pid')
        adb_pid = diagnostics.get('adb_pid')
        adb_pids = diagnostics.get('adb_pids') or []
        state_value = diagnostics.get('state_value')

        if lldb_pid in self._INVALID_PID_VALUES or self._is_process_ended_state(state_value):
            logger.warning(f"interrupt preflight: process_ended, diagnostics={diagnostics}")
            return self._interrupt_failure_payload(
                'process_ended',
                diagnostics,
                'LLDB target process 已结束或无有效 PID，请重新 attach 当前进程',
            )

        if diagnostics.get('adb_checked'):
            if adb_pid is None:
                logger.warning(f"interrupt preflight: ADB 未找到进程, diagnostics={diagnostics}")
                return self._interrupt_failure_payload(
                    'process_ended',
                    diagnostics,
                    'ADB 当前未找到该 package 的进程，请重新启动/attach',
                )
            if lldb_pid not in adb_pids:
                logger.warning(f"interrupt preflight: stale_target, diagnostics={diagnostics}")
                return self._interrupt_failure_payload(
                    'stale_target',
                    diagnostics,
                    'LLDB target PID 与 ADB 当前 PID 不一致，请重新 attach 新进程',
                )

        if diagnostics.get('adb_lldb_pid_checked') and diagnostics.get('adb_lldb_pid_alive') is False:
            logger.warning(f"interrupt preflight: ADB 上已找不到 LLDB PID, diagnostics={diagnostics}")
            return self._interrupt_failure_payload(
                'process_ended',
                diagnostics,
                'ADB 当前已找不到 LLDB target PID，请重新 attach 当前进程',
            )

        return None
    
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
        
    def _cmd_execute(self, command: str, package_name: str = "", adb_serial: str = "") -> Any:
        """执行 lldb 命令（串行化 + 智能 async 管理 + 超时保护 + 长阻塞命令拦截）"""
        logger.info(f"execute: 执行命令 '{command}'")

        # process interrupt/halt 也可能卡在 Halt timed out；统一走带 stale-target
        # 预检的 stop_process 路径，且不等待 exec_lock。
        interrupt = self._detect_interrupt_command(command)
        if interrupt:
            logger.warning(f"execute: 将 interrupt 命令 '{interrupt}' 重定向到 stop_process 安全路径")
            return self._cmd_stop_process(package_name=package_name, adb_serial=adb_serial)

        # 拦截会长时间持锁的 continue/step/run 类命令 → 引导到专用异步工具
        blocking = self._detect_blocking_command(command)
        if blocking:
            logger.warning(f"execute: 拒绝长阻塞命令 '{blocking}' (原始: '{command}')")
            return self._BLOCKING_HELP_HINT.format(matched=blocking)

        # 超时保护：如果另一个命令正在执行（如 step），不无限期等待
        if not self.exec_lock.acquire(timeout=10):
            logger.warning(f"execute: 无法获取 exec_lock (10s 超时)，命令 '{command}' 被拒绝")
            return "[错误: 另一个命令正在执行中，请稍后重试。如需中断，请使用 lldb_stop。lldb_stop / lldb_status 不依赖 exec_lock，可随时抢占。]"
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
        """批量执行多个 lldb 命令（一次锁获取，一次连接 + 超时保护 + 长阻塞命令拦截）"""
        logger.info(f"execute_batch: 批量执行 {len(commands)} 个命令")

        # 拦截批中任何一条长阻塞命令（一旦含有就整包拒绝，避免部分执行后被卡住）
        for idx, command in enumerate(commands):
            interrupt = self._detect_interrupt_command(command)
            if interrupt:
                logger.warning(f"execute_batch: 第 {idx} 条命令 '{command}' 是 interrupt '{interrupt}'，整批拒绝")
                return (
                    f"[错误: lldb_execute_batch 拒绝执行中断命令 '{interrupt}'。\n"
                    "原因: process interrupt/halt 需要先做 stale target preflight，批量命令无法携带 package_name/adb_serial 诊断参数。\n"
                    "请改用 lldb_stop(package_name=\"...\")，或 lldb_execute(command=\"process interrupt\", package_name=\"...\")。]"
                )

            blocking = self._detect_blocking_command(command)
            if blocking:
                logger.warning(f"execute_batch: 第 {idx} 条命令 '{command}' 是长阻塞 '{blocking}'，整批拒绝")
                return self._BLOCKING_HELP_HINT.format(matched=blocking)

        if not self.exec_lock.acquire(timeout=10):
            logger.warning(f"execute_batch: 无法获取 exec_lock (10s 超时)，批量命令被拒绝")
            return "[错误: 另一个命令正在执行中，请稍后重试。如需中断，请使用 lldb_stop。lldb_stop / lldb_status 不依赖 exec_lock，可随时抢占。]"
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
        """
        继续执行（异步，立即返回不阻塞）

        判定逻辑（按优先级）：
        1. cmd_result.Succeeded()==False → 命令本身失败 (success=False, did_not_resume=True)
        2. state_after != stopped → 进程已真正 resume (success=True, resumed=True, stopped_immediately=False)
        3. state_after == stopped 且 LLDB 输出含 "resuming" → resume 后立即停
           (success=True, resumed=True, stopped_immediately=True)
           典型场景：watchpoint/breakpoint/trace/signal 在前置位置立刻命中（如 VMP 单步 trace、auto-continue 链）
        4. state_after == stopped 且 LLDB 输出无 "resuming" → 真正未发出 resume
           (success=False, did_not_resume=True)
           典型场景：auto-continue 回调失败、内部状态错误等
        """
        with self.exec_lock:
            target = self.debugger.GetSelectedTarget()
            if not target.IsValid():
                logger.error("continue_async: 没有调试目标")
                return {'success': False, 'resumed': False, 'error': '没有调试目标'}
            process = target.GetProcess()
            if not process.IsValid():
                logger.error("continue_async: 进程无效")
                return {'success': False, 'resumed': False, 'error': '进程无效'}

            state_map = self._build_state_map()
            reason_map = self._build_stop_reason_map()
            STATE_STOPPED = getattr(lldb, 'eStateStopped', 6)

            # 记录 continue 前的 PC / 状态（用于 same_pc 判断）
            pc_before = None
            thread_before = process.GetSelectedThread()
            if thread_before and thread_before.IsValid():
                frame_before = thread_before.GetSelectedFrame()
                if frame_before and frame_before.IsValid():
                    pc_before = frame_before.GetPC()
            state_before = process.GetState()
            logger.info(f"continue_async: 执行前 state={state_before}, pid={process.GetProcessID()}, pc_before={('0x%x' % pc_before) if pc_before is not None else None}")

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

                # 收集 LLDB 命令的原始输出（同时含 stdout 和 stderr 通道）
                raw_output_parts = []
                if cmd_result.GetOutput():
                    raw_output_parts.append(cmd_result.GetOutput())
                if cmd_result.GetError():
                    raw_output_parts.append(cmd_result.GetError())
                raw_output = ''.join(raw_output_parts).strip()
                # "Process N resuming" 是 LLDB 在成功发出 resume 时打印的关键凭证
                output_indicates_resumed = ('resuming' in raw_output.lower())

                state_after = process.GetState()
                state_str = state_map.get(state_after, str(state_after))
                cmd_succeeded = cmd_result.Succeeded()
                logger.info(
                    f"continue_async: HandleCommand 完成 succeeded={cmd_succeeded}, "
                    f"state_after={state_after}({state_str}), "
                    f"output_indicates_resumed={output_indicates_resumed}, raw_output={raw_output!r}"
                )

                # 分支1: 命令本身失败
                if not cmd_succeeded:
                    error_msg = cmd_result.GetError() or '继续执行失败'
                    logger.error(f"continue_async: 命令失败: {error_msg}")
                    self._process_continued = False
                    return {
                        'success': False,
                        'resumed': False,
                        'did_not_resume': True,
                        'error': error_msg,
                        'raw_output': raw_output,
                        'state': state_str,
                    }

                # 分支2: 进程已真正 running/stepping → 干净 resume
                if state_after != STATE_STOPPED:
                    self._process_continued = True
                    return {
                        'success': True,
                        'resumed': True,
                        'stopped_immediately': False,
                        'message': '进程已继续执行',
                        'state': state_str,
                        'pc_before': f"0x{pc_before:x}" if pc_before is not None else None,
                        'raw_output': raw_output,
                    }

                # 分支3 / 4: state_after == stopped，需要凭借 raw_output 区分
                # 不论哪种情况，进程当前都是停止的，需恢复同步模式 + 标记非运行态
                self._process_continued = False
                self.debugger.SetAsync(False)

                # 收集停止现场（reason / pc_after / frame_info / stop_description）
                thread_after = process.GetSelectedThread()
                pc_after = None
                stop_desc = ""
                reason_label = None
                frame_info = ""
                thread_id = None
                if thread_after and thread_after.IsValid():
                    thread_id = thread_after.GetThreadID()
                    stop_desc = thread_after.GetStopDescription(1024) or ""
                    reason_value = thread_after.GetStopReason()
                    reason_label = reason_map.get(reason_value, f"unknown_{reason_value}")
                    frame_after = thread_after.GetSelectedFrame()
                    if frame_after and frame_after.IsValid():
                        pc_after = frame_after.GetPC()
                        func = frame_after.GetFunctionName() or "unknown"
                        module = frame_after.GetModule()
                        mod_name = module.GetFileSpec().GetFilename() if module.IsValid() else "unknown"
                        frame_info = f"pc=0x{pc_after:x}, func={func}, module={mod_name}"

                same_pc = None
                if pc_before is not None and pc_after is not None:
                    same_pc = (pc_before == pc_after)

                common_payload = {
                    'state': state_str,
                    'reason': reason_label,
                    'stop_description': stop_desc,
                    'thread_id': thread_id,
                    'pc_before': f"0x{pc_before:x}" if pc_before is not None else None,
                    'pc_after': f"0x{pc_after:x}" if pc_after is not None else None,
                    'same_pc': same_pc,
                    'frame_info': frame_info,
                    'raw_output': raw_output,
                }

                if output_indicates_resumed:
                    # 分支3: resume 已发出，但进程瞬间又因前置停止条件命中（trace/wp/bp/signal）
                    logger.info(
                        f"continue_async: resume 后立即停止 (stopped_immediately=True), "
                        f"reason={reason_label}, same_pc={same_pc}"
                    )
                    return {
                        'success': True,
                        'resumed': True,
                        'stopped_immediately': True,
                        'message': 'continue 已发出且进程瞬间恢复，但立即在前置停止条件命中（trace/watchpoint/breakpoint/signal 等）',
                        **common_payload,
                    }

                # 分支4: 真未 resume（如 auto-continue 回调失败）
                logger.warning(
                    f"continue_async: 命令成功但 LLDB 未输出 resuming，判定为真正未 resume; "
                    f"reason={reason_label}, raw_output={raw_output!r}"
                )
                return {
                    'success': False,
                    'resumed': False,
                    'did_not_resume': True,
                    'error': '进程未恢复运行，仍处于 stopped 状态（LLDB 命令成功但无 resuming 输出）',
                    **common_payload,
                }
            except Exception as e:
                logger.error(f"continue_async: 异常: {e}", exc_info=True)
                return {'success': False, 'resumed': False, 'error': str(e)}
    
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
            
    def _cmd_stop_process(self, package_name: str = "", adb_serial: str = "") -> Dict[str, Any]:
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
        state_map = self._build_state_map()
        state_before_str = state_map.get(state_before, str(state_before))
        logger.info(
            f"stop_process: 执行前状态={state_before}({state_before_str}), "
            f"pid={process.GetProcessID()}, package_name={package_name!r}, adb_serial={adb_serial!r}"
        )

        # halt/interrupt 前先校验 target PID/liveness，避免对 stale target 触发长时间 Halt timeout。
        preflight_failure = self._preflight_interrupt(
            target,
            process,
            package_name=package_name,
            adb_serial=adb_serial,
        )
        if preflight_failure:
            return preflight_failure

        if state_before == getattr(lldb, 'eStateStopped', 6):
            self._process_continued = False
            self.debugger.SetAsync(False)
            diagnostics = self._collect_process_diagnostics(
                target,
                process,
                package_name=package_name,
                adb_serial=adb_serial,
                include_adb=bool(package_name),
            )
            return {
                'success': True,
                'message': '进程已处于 stopped 状态',
                'already_stopped': True,
                'diagnostics': diagnostics,
            }
        
        error = process.Stop()
        state_after = process.GetState()
        logger.info(f"stop_process: Stop() 返回 error.Success()={error.Success()}, 状态={state_after}")
        
        if error.Success():
            # 恢复同步模式，标记进程已停止
            self._process_continued = False
            self.debugger.SetAsync(False)
            return {'success': True, 'message': '进程已暂停'}
        
        # halt 失败（如 "Halt timed out"），附带进程诊断帮助判断原因
        diag = self._collect_process_diagnostics(
            target,
            process,
            package_name=package_name,
            adb_serial=adb_serial,
            include_adb=True,
        )
        # Stop() 期间目标可能刚好重启，失败后再做一次 stale/process_ended 归因。
        postflight_failure = self._preflight_interrupt(
            target,
            process,
            package_name=package_name,
            adb_serial=adb_serial,
        )
        if postflight_failure:
            postflight_failure['interrupt_error'] = str(error)
            return postflight_failure
        
        logger.warning(f"stop_process: Stop() 失败: {error}, 诊断: {diag}")
        return {
            'success': False,
            'reason': 'halt_failed',
            'error': str(error),
            'lldb_pid': diag.get('lldb_pid'),
            'adb_pid': diag.get('adb_pid'),
            'adb_current_pid': diag.get('adb_current_pid'),
            'state': diag.get('state'),
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
