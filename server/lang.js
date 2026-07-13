// WhatsApp MVP - 极简语言检测（共享给 index.js / worker.js）
//
// 只需要在"中文文案 / 英文文案"两套写死模板之间选一套——含中日韩表意
// 文字判中文，否则判英文。够用，零依赖，零延迟。

const CJK_RE = /[一-鿿㐀-䶿]/;

export function detectLang(text) {
  return text && CJK_RE.test(text) ? "zh" : "en";
}

// 依次检查多个候选文本（比如"这次触发消息的文字"+"这个任务当初的 edit_request"），
// 任一个命中中文特征就判中文；全部候选都判不出（纯关键词命令、空文本）时，
// 退回调用方给定的兜底语言，而不是想当然地判英文。
export function resolveLang(fallback, ...texts) {
  for (const s of texts) {
    if (s && CJK_RE.test(s)) return "zh";
  }
  return fallback;
}

// t(lang, zh文案, en文案) —— 按语言选一条写死文案。
export function t(lang, zh, en) {
  return lang === "zh" ? zh : en;
}
