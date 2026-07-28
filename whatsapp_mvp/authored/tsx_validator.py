#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M1 · TsxValidator —— Arm B 安全闸(渲染前静态校验 + 编译探针)。

职责(设计文档 §2.3/§5):在渲染**之前**校验模型现写的 AuthoredScene.tsx,
挡住危险/不合契约的输出。执行模型生成的代码 = 任意代码执行,这道闸是笼子。

分两级:
  1) 静态检查(纯 Python,无外部依赖,毫秒级)—— import 白名单、危险符号、
     不确定性符号、契约形状、体量上限。任何一条不过即 ok=False。
  2) 编译探针(tsc --noEmit,可选)—— 语法/基本类型层面能不能编译。
     tsc 不存在时探针跳过(compiled=None),静态结论仍然有效。

返回:ValidationResult{ok, violations[], compiled}
  violations 每条 {rule, detail, line} —— rule 是稳定标识,可直接作为
  FeedbackReviser 的"按规矩重写"缺陷输入。

设计要点(对抗式审查后定下的):
  - 扫描危险符号前先**剥离字符串字面量与注释**:字幕文案里出现 "update"、
    JSX 文本里出现单词 Date 不应误伤;但剥离后代码区里的 Date.now( 逃不掉。
  - 危险符号用**带上下文的模式**而非裸词:`Date` 只在 `new Date` / `Date.` /
    `Date(` 形态下才违规,避免 candidate/update 这类误报。
  - import 解析覆盖四种形态:import x from 'm' / import 'm' / require('m') /
    import('m')(后两种直接违规,不看模块名)。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ─────────────────────────── 可配置项(与设计文档 §5 对齐)───────────────────────────

ALLOWED_IMPORTS = {"react", "remotion"}   # import 白名单;放开 @remotion/* 是未决问题,默认不放
MAX_BYTES = 200_000                        # 体量上限:200KB 源码(实测正常场景 ~20KB)
MAX_LINES = 3_000

# 危险/不确定性符号:带上下文的正则,降低误报。每条 (rule, pattern, 说明)
_BANNED_PATTERNS: list[tuple[str, str, str]] = [
    ("eval",          r"\beval\s*\(",                    "eval() 任意代码执行"),
    ("function_ctor", r"\bnew\s+Function\b|\bFunction\s*\(", "Function 构造器等价 eval"),
    ("require",       r"\brequire\s*\(",                 "require() 绕过 import 白名单"),
    ("dynamic_import", r"\bimport\s*\(",                 "动态 import() 绕过白名单"),
    ("process",       r"\bprocess\s*\.",                 "process.* 访问进程/环境变量"),
    ("fetch",         r"\bfetch\s*\(",                   "网络请求"),
    ("xhr",           r"\bXMLHttpRequest\b",             "网络请求"),
    ("websocket",     r"\bWebSocket\b",                  "网络连接"),
    ("globalthis",    r"\bglobalThis\b",                 "全局对象逃逸"),
    ("window",        r"\bwindow\s*\.",                  "浏览器全局副作用"),
    ("document",      r"\bdocument\s*\.",                "DOM 直接操作(应由 React 渲染)"),
    # 不确定性:破坏"每帧由 frame 决定"的确定性渲染(设计文档 §5.3)
    ("date",          r"\bnew\s+Date\b|\bDate\s*[.(]",   "Date 破坏帧确定性"),
    ("random",        r"\bMath\s*\.\s*random\b",         "Math.random 破坏帧确定性"),
    ("performance",   r"\bperformance\s*\.",             "performance.* 破坏帧确定性"),
    ("timers",        r"\bset(?:Timeout|Interval)\s*\(", "定时器不属于确定性渲染"),
]

# 契约形状(设计文档 §4):必须存在的符号
_REQUIRED = [
    ("contract_component", r"\bAuthoredScene\b", "必须定义/导出名为 AuthoredScene 的组件"),
    ("contract_default_export", r"\bexport\s+default\b", "必须 export default"),
    # RenderQA 不做字幕语义判断(§2.5),改由这里保证代码确实消费了 words prop
    ("contract_words_used", r"\bwords\b", "必须使用 words prop 渲染字幕"),
]


@dataclass
class Violation:
    rule: str
    detail: str
    line: int  # 1-based;定位不到时为 0


@dataclass
class ValidationResult:
    ok: bool
    violations: list = field(default_factory=list)
    compiled: bool | None = None  # None = 探针不可用(tsc 缺失),未参与判定

    def summary(self) -> str:
        if self.ok:
            return "OK" + ("(已过编译探针)" if self.compiled else "(编译探针未运行)")
        return "; ".join(f"[{v.rule}] L{v.line}: {v.detail}" for v in self.violations)


# ─────────────────────────── 字符串/注释剥离(误报防线)───────────────────────────

def _strip_strings_and_comments(src: str) -> str:
    """把字符串字面量('' "" ``)与注释(// /* */)替换为等长空白,保留换行,
    行号不变。模板字面量的 `${...}` 内部是**代码**,保留并继续扫描(藏在
    `${eval(...)}` 里的危险调用不能逃);嵌套模板串用栈处理。
    JSX 文本(标签之间的裸文字)不剥离——所以危险模式都要求代码形态
    (如 `Date.`),纯单词不触发。"""
    out: list[str] = []
    i, n = 0, len(src)
    # 栈元素:每进入一层模板字面量压一个 [在该层 ${} 内的花括号深度计数用] 占位
    tpl_stack: list[int] = []
    in_tpl_expr = False  # 当前是否在最内层模板串的 ${...} 表达式里

    def blank(seg: str) -> str:
        return "".join(ch if ch == "\n" else " " for ch in seg)

    while i < n:
        c = src[i]
        two = src[i:i + 2]

        if in_tpl_expr:
            # ${...} 内是代码:原样保留,但要处理嵌套 {}、嵌套字符串/注释/模板串
            if c == "{":
                tpl_stack[-1] += 1
                out.append(c); i += 1
            elif c == "}":
                if tpl_stack[-1] == 0:
                    in_tpl_expr = False        # 回到模板串的字符串部分
                    out.append(c); i += 1
                else:
                    tpl_stack[-1] -= 1
                    out.append(c); i += 1
            elif two == "//":
                j = src.find("\n", i); j = n if j == -1 else j
                out.append(" " * (j - i)); i = j
            elif two == "/*":
                j = src.find("*/", i + 2); j = n if j == -1 else j + 2
                out.append(blank(src[i:j])); i = j
            elif c in ("'", '"'):
                j = i + 1
                while j < n and src[j] != c:
                    j += 2 if src[j] == "\\" else 1
                out.append(c + blank(src[i + 1:j]) + (c if j < n else "")); i = j + 1
            elif c == "`":
                tpl_stack.append(0)            # 嵌套模板串:进入其字符串部分
                in_tpl_expr = False
                out.append(c); i += 1
            else:
                out.append(c); i += 1
            continue

        if tpl_stack:
            # 在模板串的字符串部分:抹空白,直到 ` 结束或 ${ 进入表达式
            if c == "\\":
                out.append(blank(src[i:i + 2])); i += 2
            elif two == "${":
                out.append("${"); i += 2
                in_tpl_expr = True
            elif c == "`":
                tpl_stack.pop()
                out.append(c); i += 1
                # 弹出后若外层还是模板串,外层此刻必在 ${} 表达式里
                in_tpl_expr = bool(tpl_stack)
            else:
                out.append(c if c == "\n" else " "); i += 1
            continue

        # 普通代码区
        if two == "//":
            j = src.find("\n", i); j = n if j == -1 else j
            out.append(" " * (j - i)); i = j
        elif two == "/*":
            j = src.find("*/", i + 2); j = n if j == -1 else j + 2
            out.append(blank(src[i:j])); i = j
        elif c in ("'", '"'):
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(c + blank(src[i + 1:j]) + (c if j < n else "")); i = j + 1
        elif c == "`":
            tpl_stack.append(0)
            out.append(c); i += 1
        else:
            out.append(c); i += 1
    return "".join(out)


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


# ─────────────────────────── 静态检查 ───────────────────────────

_IMPORT_RE = re.compile(
    r"""\bimport\b            # import 关键字
        (?:[^'"]*?)           # 绑定部分(可空,如裸 import 'm')
        ['"]([^'"]+)['"]      # 模块名
    """, re.VERBOSE)


def _check_static(src: str) -> list:
    violations: list[Violation] = []

    # 0) 体量
    if len(src.encode("utf-8", errors="replace")) > MAX_BYTES:
        violations.append(Violation("size_bytes", f"源码超过 {MAX_BYTES} 字节上限", 0))
    if src.count("\n") + 1 > MAX_LINES:
        violations.append(Violation("size_lines", f"源码超过 {MAX_LINES} 行上限", 0))

    stripped = _strip_strings_and_comments(src)

    # 1) import 白名单(在剥离文本上找位置、在原文上取模块名会错位——直接扫原文,
    #    但为防"字符串里写了 import"误报,同时要求该行在剥离文本中仍含 import)
    for m in _IMPORT_RE.finditer(src):
        line_no = _line_of(src, m.start())
        stripped_line = stripped.splitlines()[line_no - 1] if line_no <= len(stripped.splitlines()) else ""
        if "import" not in stripped_line:
            continue  # import 出现在字符串/注释里,不算
        module = m.group(1)
        if module not in ALLOWED_IMPORTS:
            violations.append(Violation(
                "import_whitelist", f"import {module!r} 不在白名单 {sorted(ALLOWED_IMPORTS)}", line_no))

    # 2) 危险/不确定性符号(在剥离文本上扫)
    for rule, pattern, why in _BANNED_PATTERNS:
        for m in re.finditer(pattern, stripped):
            violations.append(Violation(rule, why, _line_of(stripped, m.start())))

    # 3) 契约形状(在剥离文本上查,防止只在注释/文案里出现)
    for rule, pattern, why in _REQUIRED:
        if not re.search(pattern, stripped):
            violations.append(Violation(rule, why, 0))

    return violations


# ─────────────────────────── 编译探针(tsc --noEmit)───────────────────────────

_SHIMS = {
    # 环境里未必有 @types/react;探针目标是**语法/结构**错误(括号错配、JSX 未闭合、
    # 语句残缺),类型环境用 any-shim 兜住,真类型检查留给用户机上有真 node_modules 的
    # 渲染阶段。shim 提供:react 常用类型出口 + 全局 JSX 命名空间。
    "react.d.ts": (
        'declare module "react" {\n'
        '  export type FC<P = any> = (props: P) => any;\n'
        '  export type ReactNode = any;\n'
        '  export function useMemo<T>(f: () => T, deps: any[]): T;\n'
        '  const React: any;\n'
        '  export default React;\n'
        '}\n'
        'declare module "react/jsx-runtime";\n'
        'declare module "react/jsx-dev-runtime";\n'
        'declare namespace JSX { interface IntrinsicElements { [elem: string]: any } }\n'
        # `import React` 后在类型位置写 React.FC —— 需要同名全局命名空间(UMD 手法)
        'declare namespace React { type FC<P = any> = (props: P) => any; type ReactNode = any; }\n'
    ),
    "remotion.d.ts": 'declare module "remotion";\n',
}

def _check_compile(src: str, timeout_s: int = 60) -> bool | None:
    """tsc --noEmit 语法/结构探针。True=过, False=不过, None=tsc 不可用(跳过)。"""
    tsc = shutil.which("tsc")
    if not tsc:
        return None
    with tempfile.TemporaryDirectory(prefix="tsxv_") as td:
        d = Path(td)
        (d / "scene.tsx").write_text(src, encoding="utf-8")
        for name, content in _SHIMS.items():
            (d / name).write_text(content, encoding="utf-8")
        try:
            r = subprocess.run(
                [tsc, "--noEmit", "--jsx", "react-jsx", "--target", "es2018",
                 "--module", "esnext", "--moduleResolution", "bundler",
                 "--strict", "false", "--noImplicitAny", "false",
                 "--skipLibCheck", "scene.tsx", "react.d.ts", "remotion.d.ts"],
                cwd=str(d), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0


# ─────────────────────────── 入口 ───────────────────────────

def validate_tsx(src: str, run_compile_probe: bool = True) -> ValidationResult:
    violations = _check_static(src)
    compiled: bool | None = None
    # 静态已挂就不必花编译时间;静态干净才值得跑探针
    if run_compile_probe and not violations:
        compiled = _check_compile(src)
        if compiled is False:
            violations.append(Violation("compile", "tsc --noEmit 编译不过(语法/结构错误)", 0))
    ok = not violations
    return ValidationResult(ok=ok, violations=violations, compiled=compiled)


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not p or not p.exists():
        print("用法: python tsx_validator.py <AuthoredScene.tsx>")
        raise SystemExit(1)
    res = validate_tsx(p.read_text(encoding="utf-8"))
    print(("✓ " if res.ok else "✗ ") + res.summary())
    raise SystemExit(0 if res.ok else 1)