// 客户端 schema 校验 —— 定位到具体字段，而不是甩一整面墙的错误文本。
//
// ajv 的 `instancePath` 长得就是 `/captions/3/startMs`，解析一下就能同时拿到
// 「哪个字段该标红」和「时间轴上哪个 clip 该标红」。服务端的 python jsonschema
// 才是最终裁判（`render_props_directly` 会再校验一次），所以这里只警告、不
// 阻止保存——本地放行、服务端拒绝，总好过本地误判把用户卡死。

import Ajv2020, { type ErrorObject } from "ajv/dist/2020";
import renderPropsSchema from "../../../contracts/render_props.schema.json";

const ajv = new Ajv2020({ allErrors: true, strict: false });
const validateFn = ajv.compile(renderPropsSchema as object);

export type FieldError = {
  /** 顶层属性名，例如 "captions"、"dataCards"；根级错误为 null。 */
  section: string | null;
  /** 数组下标；非数组字段为 null。 */
  index: number | null;
  /** 数组项内部的字段名，例如 "startMs"；整项错误为 null。 */
  field: string | null;
  message: string;
  /** 原始 instancePath，调试用。 */
  path: string;
};

export type ValidationResult = {
  valid: boolean;
  errors: FieldError[];
};

function parseInstancePath(path: string, err: ErrorObject): Omit<FieldError, "message" | "path"> {
  // "" | "/captions" | "/captions/3" | "/captions/3/startMs"
  const parts = path.split("/").filter(Boolean);
  const section = parts[0] ?? null;
  const maybeIndex = parts[1] !== undefined ? Number(parts[1]) : NaN;
  const index = Number.isInteger(maybeIndex) ? maybeIndex : null;
  let field = parts[2] ?? null;
  // required 类错误的 instancePath 指向对象本身，缺哪个字段在 params 里。
  if (!field && err.keyword === "required") {
    const missing = (err.params as { missingProperty?: string }).missingProperty;
    if (missing) field = missing;
  }
  return { section, index, field };
}

export function validateProps(props: Record<string, unknown>): ValidationResult {
  const valid = validateFn(props) as boolean;
  if (valid) return { valid: true, errors: [] };
  const errors: FieldError[] = (validateFn.errors || []).map((err) => ({
    ...parseInstancePath(err.instancePath || "", err),
    message: err.message || "invalid",
    path: err.instancePath || "(root)",
  }));
  return { valid: false, errors };
}

/** 建一个 `section/index/field` -> 错误信息 的查找表，供表单逐字段标红。 */
export function errorIndex(errors: FieldError[]) {
  const byField = new Map<string, string>();
  const byItem = new Map<string, string>();
  for (const e of errors) {
    if (e.section === null) continue;
    if (e.index !== null) {
      const itemKey = `${e.section}/${e.index}`;
      if (!byItem.has(itemKey)) byItem.set(itemKey, e.message);
      if (e.field) {
        const key = `${e.section}/${e.index}/${e.field}`;
        if (!byFieldHas(byField, key)) byField.set(key, e.message);
      }
    } else if (e.field) {
      const key = `${e.section}/${e.field}`;
      if (!byFieldHas(byField, key)) byField.set(key, e.message);
    } else {
      const key = e.section;
      if (!byFieldHas(byField, key)) byField.set(key, e.message);
    }
  }
  return {
    /** 数组项里某个字段的错误。 */
    forField: (section: string, index: number | null, field: string): string | undefined =>
      byField.get(index === null ? `${section}/${field}` : `${section}/${index}/${field}`),
    /** 整个数组项有没有错（时间轴 clip 标红用）。 */
    forItem: (section: string, index: number): boolean => byItem.has(`${section}/${index}`),
    /** 顶层字段本身的错误。 */
    forSection: (section: string): string | undefined => byField.get(section),
  };
}

function byFieldHas(m: Map<string, string>, k: string): boolean {
  return m.has(k);
}
