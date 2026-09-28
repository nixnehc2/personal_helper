"""Terminal-only QQ progress renderer. No dependency on sync or its storage."""
import os
import shutil
import sys
import time
import unicodedata


def _supports_refresh():
    if not sys.stdout.isatty() or os.environ.get("TERM") == "dumb":
        return False
    if os.name == "nt":
        # Only use ANSI cursor movement when the console already supports it.
        # Do not change the user's console mode.
        import ctypes
        import msvcrt
        mode = ctypes.c_ulong()
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdout.fileno()))
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)) and mode.value & 4)
    return True


def _safe(value):
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(value))


def _wrap(text, width):
    """Avoid implicit terminal wraps, including double-width Chinese characters."""
    lines, current, used = [], "", 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if current and used + size > width:
            lines.append(current)
            current, used = "", 0
        current += char
        used += size
    return lines + [current]


class ConsoleSkipEvent:
    """Event-like nonblocking Windows input polling at sync cancellation points."""
    def __init__(self):
        import msvcrt
        self.console = msvcrt
        self.requested = False
        self.typed = ''
        self.extended = False

    def clear(self):
        self.requested = False

    def is_set(self):
        while self.console.kbhit():
            char = self.console.getwch()
            if self.extended:
                self.extended = False
                continue
            if char in ('\x00', '\xe0'):
                self.extended = True
            elif char == '\x03':
                raise KeyboardInterrupt
            elif char in ('\r', '\n'):
                if not self.typed.strip():
                    self.requested = True
                self.typed = ''
            elif char == '\b':
                self.typed = self.typed[:-1]
            else:
                self.typed += char
        return self.requested


class QQSyncProgress:
    def __init__(self, emit=print, *, interactive=None, width=None, clock=time.monotonic):
        self.emit = emit
        try:
            self.interactive = (emit is print and _supports_refresh()) if interactive is None else interactive
        except Exception:
            self.interactive = False
        self.can_skip = False
        self.width = width
        self.clock = clock
        self.lines = 0
        self.last_page = None

    @staticmethod
    def _conversation(state):
        chat = state.get("conversation") or {}
        kind = {"group": "群聊", "private": "私聊"}.get(chat.get("type"), "")
        name = str(chat.get("name", ""))
        if len(name) > 80:
            name = name[:77] + "..."
        return _safe(f"{kind} {name} {chat.get('id', '')}".strip())

    @staticmethod
    def _counts(counts):
        return " | ".join(f"{label} {counts[key]}" for label, key in (
            ("扫描", "scanned"), ("可读消息", "text"), ("新增", "added"),
            ("重复", "duplicates"), ("无文字内容跳过", "skipped"), ("失败", "failed")))

    def _frame(self, state):
        total, done = state["total_conversations"], state["completed_conversations"]
        percent = done * 100 // total if total else (100 if total == 0 and state["event"] == "finish" else 0)
        filled = percent // 5
        lines = [f"QQ同步 [{'█' * filled}{'-' * (20-filled)}] {done}/{total if total is not None else '?'} {percent}%"]
        if state.get("conversation"):
            lines.append(f"当前：{self._conversation(state)} | page={state['page']}")
            lines.append("本会话：" + self._counts(state["conversation_counts"]))
        suffix = "（本会话待提交）" if state["provisional"] else ""
        if state["rolled_back"]:
            suffix = "（本会话已回滚，总计不含回滚数据）"
        lines.append("总计：" + self._counts(state["total_counts"]) + suffix)
        if self.can_skip and state["event"] in ("conversation_start", "page"):
            lines.append("  按 Enter 可永久跳过当前会话")
        return lines

    def _draw(self, lines):
        if not self.interactive:
            self.emit("\n".join(lines))
            return
        width = max(10, (self.width or shutil.get_terminal_size((80, 24)).columns) - 1)
        physical = [part for line in lines for part in _wrap(line, width)]
        prefix = "\r" + (f"\x1b[{self.lines-1}A" if self.lines > 1 else "") + "\x1b[J"
        self.emit(prefix + "\n".join(physical), end="", flush=True)
        self.lines = len(physical)

    def close(self):
        try:
            if self.lines:
                self.emit("", flush=True)
        except Exception:
            pass
        finally:
            self.lines = 0

    def __call__(self, state):
        try:
            event = state["event"]
            if event == "connecting":
                self.emit("[QQ] 正在连接并获取会话列表…")
                return
            if event in ("error", "message_error"):
                self.close()
                detail = "消息处理失败" if event == "message_error" else "同步失败"
                continuation = "继续本会话其他消息…" if event == "message_error" else "继续处理其余可用会话…"
                if not state.get("conversation"):
                    continuation = "本次更新未能完成，请查看最终统计。"
                self.emit(f"[QQ] {self._conversation(state)} {detail}：{_safe(state['error'])}\n{continuation}")
                return
            # Pre-marked skip: conversation was already in the skip list.
            if event == "conversation_skipped":
                self.emit(f"[{state['current_conversation']}/{state['total_conversations']}] "
                          f"{self._conversation(state)} 已跳过（已标记忽略）")
                return
            # User skip during sync: show confirmation and hint.
            if event == "conversation_skipped_user":
                self.close()
                self.emit(f"[{state['current_conversation']}/{state['total_conversations']}] "
                          f"{self._conversation(state)} 已跳过，并标记为以后不再同步。")
                return
            now = self.clock()
            # Pipes/log collectors get bounded textual page updates; all boundary/error events remain visible.
            if event == "page" and not self.interactive:
                if self.last_page is not None and now - self.last_page < 1:
                    return
                self.last_page = now
            elif event == "conversation_start":
                self.last_page = None
            self._draw(self._frame(state))
            if event in ("conversation_end", "finish"):
                self.close()
        except Exception:
            # Best-effort output only, including broken pipes and limited encodings.
            self.interactive = False
            self.lines = 0
