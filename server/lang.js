// WhatsApp MVP - 极简语言检测（共享给 index.js / worker.js）
//
// 只需要在"中文文案 / 英文文案"两套写死模板之间选一套——含中日韩表意
// 文字判中文，否则判英文。够用，零依赖，零延迟。

const CJK_RE = /[一-鿿㐀-䶿]/;
// 拉丁字母——用来把"真的是英文内容"和"没有语言信息的裸 token"区分开。
const LATIN_RE = /[A-Za-z]/;

export function detectLang(text) {
  return text && CJK_RE.test(text) ? "zh" : "en";
}

// 依次检查多个候选文本（比如"这次触发消息的文字"+"这个任务当初的 edit_request"）。
// 命中中文特征 -> zh；不含中文、但含拉丁字母且是"一句话"（含空格，真的是
// 英文句子，比如视频配文 "give a video"）-> en。修复 2026-07-15 实测事故：
// 此前"没查到中文"一律退回调用方给定的兜底语言(几乎总是写死的 zh)，导致
// 用户发纯英文配文/文字时被误判成中文回复——本函数原本只有能力"正面确认
// 中文"，对"确认是英文"完全没有识别力，任何非中文文本都被当成"不知道"。
//
// "含空格"这道门槛是刻意的：纯数字（选主视频阶段的裸编号"2"）和裸协议
// 指令词（confirm/go/cancel/retry 等，用户不管中英文都照打英文单词，本身
// 不携带语言信息）都是不含空格的单个 token，必须继续看下一个候选（通常是
// 更可靠的 edit_request/caption），而不是被误判成"用户在说英文"——这是第
// 一版修复漏掉的：把"含拉丁字母"直接当信号，会把这些裸指令词也判成英文。
export function resolveLang(fallback, ...texts) {
  for (const s of texts) {
    if (!s) continue;
    const trimmed = s.trim();
    if (CJK_RE.test(trimmed)) return "zh";
    if (LATIN_RE.test(trimmed) && /\s/.test(trimmed)) return "en";
  }
  return fallback;
}

// t(lang, zh文案, en文案) —— 按语言选一条写死文案。
export function t(lang, zh, en) {
  return lang === "zh" ? zh : en;
}
