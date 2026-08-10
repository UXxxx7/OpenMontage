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

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

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
    # Phase 8 —— 冻结提示第 9 条(editable manifest)要求每个清单字段都从
    # props.overrides?.[id]?.field 读取。这里只查符号本身存在(跟上面三条
    # 同一个粗粒度),不check overrides 用得对不对——manifest 里每个 id 真的
    # 接没接上,由下面 check_manifest_ids_wired 单独、更精确地查。
    ("contract_overrides_used", r"\boverrides\b",
     "必须读取 overrides prop(冻结提示第 9 条——即使这个场景没有任何可编辑元素,也应显式写 props.overrides 兜底读取,而不是完全不提)"),
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
    # Phase 8 补丁 —— 清单完整性/几何接线两条观测性检查(见下面
    # _check_manifest_incomplete / _check_manifest_geometry_unwired)的发现。
    # 刻意跟 violations 分开、**不参与 ok 的判定**:compose_orchestrator.compose()
    # 的闭环规则是"验证不过 → 最多 max_revise_rounds(2)轮修订 → 仍不过就
    # 整体落穿 fallback_fn(Arm A)"(compose_orchestrator.py 自己的 settle()/
    # do_fallback() 逻辑,已实测确认:验证若从未通过,render_fn 从未被调用过,
    # best 恒为 None,必然触发 do_fallback)。geometry_unwired 在真实产出里
    # 实测 0/18 达标,即使 scene_author.py 的提示词已经给了带示例代码的强调
    # 段落——把它做成硬违规,会让"改完还是没接上"的场景从"能看但拖不动"
    # 退化成"整支 Arm B 直接跳车回 Arm A"这种严重得多的后果,拿一个从未被
    # 观测到能修好的东西去冒着把整个 job 打回模板的风险不划算。这两条检查
    # 因此只落盘/打日志,供后续统计模型这方面的真实达标率——真要收紧成硬性
    # 门槛,应该先看这份数据再决定,而不是凭直觉。
    advisory: list = field(default_factory=list)

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


# ─────────────────────────── 清单接线检查(Phase 8)───────────────────────────

def _check_manifest_ids_wired(src: str, manifest: list) -> list:
    """清单里每个 id 必须在 tsx 源码里字面出现过——否则这份清单是摆设,没有
    真的接上 props.overrides?.[id] 读取,手动编辑会完全没有效果。跟静态检查
    其它规则不同,这里在**原文**(不是剥离字符串/注释后的文本)上找,因为
    id 合法出现的位置本来就是字符串字面量(`props.overrides?.['stat-1']`
    这个 'stat-1' 本身就是字符串),剥离会把它连同真实用法一起抹掉。"""
    violations: list[Violation] = []
    for el in manifest or []:
        if not isinstance(el, dict):
            continue
        el_id = el.get("id")
        if el_id and el_id not in src:
            violations.append(Violation(
                "manifest_id_unwired",
                f"清单里的 id {el_id!r} 没有在 tsx 源码里出现过——没有接上 props.overrides 读取,是摆设清单",
                0))
    return violations


# 每个 `overrides?.["id"]` 引用里的 id——跟上面 _check_manifest_ids_wired 反过来
# 的方向要扫的东西:代码里引用过、但清单没列出来的 id。
_OVERRIDE_ID_RE = re.compile(r"""overrides\?\.\[(["'])([^"']+)\1\]""")


def _check_manifest_incomplete(src: str, manifest: list) -> list:
    """_check_manifest_ids_wired 只查"清单 -> 源码"这一个方向(清单里列的 id
    必须真的在源码里出现),从没查过反方向。confirmed 真实生产事故(2026-08,
    job_36563f8d4d73 / job_d14dbb929709):两个 job 的 manifest.json 都是 `[]`,
    但各自的 scene.tsx 分别有 26、30 处 `props.overrides?.["id"]?.field` 读取,
    横跨 5-6 个稳定的 id——场景本身完全接好了线,清单只是没被正确产出/落盘。
    这正是 Rule 17(呈现型标准的盲区)那类漏检:manifest_id_unwired 对空清单
    永远平凡通过。这条检查补上反方向。

    仅供观测(见 ValidationResult.advisory 的类型说明)——不参与 ok 判定。"""
    violations: list[Violation] = []
    referenced = {m.group(2) for m in _OVERRIDE_ID_RE.finditer(src)}
    listed = {el.get("id") for el in (manifest or []) if isinstance(el, dict)}
    for el_id in sorted(referenced - listed):
        violations.append(Violation(
            "manifest_incomplete",
            f"源码里读取了 props.overrides?.[{el_id!r}],但清单没有列出这个 id——"
            "清单不完整,这个元素编辑器里编辑不到",
            0))
    return violations


def _check_manifest_geometry_unwired(src: str, manifest: list) -> list:
    """冻结提示第 9 条(scene_author.py)专门用带示例代码的一整段要求 x/y/w/h
    必须从 props.overrides?.[id]?.<field> 读取,而不是硬编码成跟清单数字一样
    的字面量。实测across 全部 18 份真实产出场景:0 份做到。这条检查把这个
    已确认的缺口变成可程序化观测的信号,而不是继续只停留在
    AuthoredEditor.tsx 的 scenePredatesPositionEditing 这种前端事后横幅里。

    仅供观测——不参与 ok 判定(理由见 ValidationResult.advisory 的类型说明:
    0/18 的历史达标率意味着把这个做成硬性门槛,大概率只是白烧一轮修订预算,
    然后因为 compose_orchestrator.compose() 的收口规则,让整个 job 从"能看
    但拖不动"退化成整支 Arm B 直接跳车回 Arm A)。"""
    violations: list[Violation] = []
    for el in manifest or []:
        if not isinstance(el, dict):
            continue
        el_id = el.get("id")
        if not el_id:
            continue
        pattern = re.compile(
            r"""overrides\?\.\[["']""" + re.escape(el_id) + r"""["']\]\??\.(x|y|w|h)\b"""
        )
        if not pattern.search(src):
            violations.append(Violation(
                "manifest_geometry_unwired",
                f"清单元素 {el_id!r} 的 x/y/w/h 没有任何一个从 "
                f"props.overrides?.[{el_id!r}]?.<field> 读取——拖动/缩放这个元素"
                "在渲染里不会有任何效果",
                0))
    return violations


# ─────────────────────────── cuts 契约观测(Phase 8 · 中途剪切)───────────────────────────

# 匹配 <OffthreadVideo ...> 整个开标签(用有界窗口而不是贪婪 [^>]*,防止标签
# 内某个 JSX 表达式自带的 `>`——如 style={{opacity: x > 0.5 ? 1 : 0}}——把扫描
# 提前截断)。400 字符足够覆盖真实场景里这个标签常见的属性数量。
_OFFTHREAD_VIDEO_TAG_RE = re.compile(r"<OffthreadVideo\b[\s\S]{0,400}?(?:/>|>)")
# base 视频的 src 只可能是 videoSrc 这个变量名(冻结提示第 4 条钦定的唯一名字)
# ——直接写字面量 props.videoSrc,或先 `const { videoSrc } = props` 解构再引用。
_VIDEO_SRC_ATTR_RE = re.compile(r"\bsrc\s*=\s*\{\s*(?:props\s*\.\s*)?videoSrc\s*\}")


def _check_base_video_rendered_by_scene(src: str) -> list:
    """冻结提示第 4 条(scene_author.py,mid-video 剪切改动)——AI 写的场景不再
    自己渲染 base 视频,AuthoredCutWrapper 在场景外面渲染,场景只画 overlay。
    这条检查扫场景自己的 JSX 里是否还有 `<OffthreadVideo src={videoSrc}>`
    (或 `src={props.videoSrc}`)——真出现意味着这个场景两份视频重叠播放(wrapper
    的 + 场景自己的),剪切功能对这个场景完全没用(它自己的视频永远整段播放,
    不听 sourceFrame)。

    仅供观测(ValidationResult.advisory,不参与 ok 判定)——跟
    manifest_geometry_unwired 同一个理由:这是一条全新的契约要求,真实历史
    达标率未知,做成硬性门槛只会在模型还没学会遵守之前,把"剪切对这个场景
    不生效"这种局部退化,升级成 compose_orchestrator.compose() 收口规则下
    "整支 Arm B 直接跳车回 Arm A"的更严重后果。"""
    violations: list[Violation] = []
    stripped = _strip_strings_and_comments(src)
    for m in _OFFTHREAD_VIDEO_TAG_RE.finditer(stripped):
        if _VIDEO_SRC_ATTR_RE.search(m.group(0)):
            violations.append(Violation(
                "base_video_rendered_by_scene",
                "场景自己渲染了 <OffthreadVideo src={videoSrc}>——base 视频现在应该"
                "由 AuthoredCutWrapper 在场景外面渲染,场景只画 overlay;这个场景"
                "如果留着自己的 base 视频,中途剪切对它不会有任何效果",
                _line_of(stripped, m.start())))
    return violations


def _check_source_frame_not_used(src: str) -> list:
    """冻结提示第 3/4 条——场景的时间基准应该是 `props.sourceFrame`,不是
    `useCurrentFrame()` 的返回值(两者在没有剪切时数值相同,但一旦真的剪切,
    只有 sourceFrame 是正确的)。这里只做一个粗粒度、全文级别的信号:出现了
    useCurrentFrame( 但全文完全没有 sourceFrame 这个词——大概率是场景还在用
    旧写法,尚未迁移到新契约。不尝试判断 useCurrentFrame 的返回值有没有被真的
    赋给 sourceFrame(那需要真的做数据流分析,这里只要够便宜、够能发现问题
    就够了,跟 manifest_incomplete 等检查同一个精度取舍)。

    仅供观测——理由同上,新契约要求,不做硬性门槛。"""
    stripped = _strip_strings_and_comments(src)
    if re.search(r"\buseCurrentFrame\s*\(", stripped) and not re.search(r"\bsourceFrame\b", stripped):
        return [Violation(
            "source_frame_unused",
            "场景调用了 useCurrentFrame() 但全文没有出现 sourceFrame——内容计时"
            "应该读 props.sourceFrame,不是 useCurrentFrame() 的返回值,否则剪切"
            "发生时这个场景的每一处计时都会错位",
            0)]
    return []


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

def validate_tsx(src: str, run_compile_probe: bool = True,
                  manifest: list | None = None) -> ValidationResult:
    """manifest 可选(Phase 8)——传了就额外校验清单里每个 id 是否真的接上了
    overrides 读取。省略时行为跟改动前完全一致,不影响任何既有调用方。"""
    violations = _check_static(src)
    if manifest:
        violations.extend(_check_manifest_ids_wired(src, manifest))

    # manifest_incomplete / manifest_geometry_unwired(见各自函数的说明)——
    # 观测性检查,故意用 `is not None`(不是 truthy)门控:manifest=[] 恰恰
    # 是 manifest_incomplete 最该抓的那个真实事故形态(清单整体丢失),用
    # `if manifest:` 会把这种情况直接跳过检查,正中盲区。
    advisory: list[Violation] = []
    if manifest is not None:
        advisory.extend(_check_manifest_incomplete(src, manifest))
        advisory.extend(_check_manifest_geometry_unwired(src, manifest))

    # cuts 契约观测——跟 manifest 无关,不需要 manifest is not None 才跑
    # (每次调用都跑;跟 manifest_incomplete 的空清单陷阱不是同一类问题)。
    advisory.extend(_check_base_video_rendered_by_scene(src))
    advisory.extend(_check_source_frame_not_used(src))

    for v in advisory:
        logger.info(f"tsx_validator advisory [{v.rule}]: {v.detail}")

    compiled: bool | None = None
    # 静态已挂就不必花编译时间;静态干净才值得跑探针
    if run_compile_probe and not violations:
        compiled = _check_compile(src)
        if compiled is False:
            violations.append(Violation("compile", "tsc --noEmit 编译不过(语法/结构错误)", 0))
    ok = not violations
    return ValidationResult(ok=ok, violations=violations, compiled=compiled, advisory=advisory)


if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not p or not p.exists():
        print("用法: python tsx_validator.py <AuthoredScene.tsx>")
        raise SystemExit(1)
    res = validate_tsx(p.read_text(encoding="utf-8"))
    print(("✓ " if res.ok else "✗ ") + res.summary())
    raise SystemExit(0 if res.ok else 1)