import React, { useState } from "react";
import { FrameInput, MsInput } from "./frameInput";
import {
  humanLabel, isAdvancedField, isFrameField, isMsField, sortFieldNames,
} from "./fieldHints";

// Minimal JSON-Schema-lite shape —— 只覆盖 contracts/render_props.schema.json
// 真正用到的关键字（draft 2020-12，但这份 schema 只用了基础子集）。
export type JSONSchema = {
  type?: string | string[];
  properties?: Record<string, JSONSchema>;
  items?: JSONSchema;
  required?: string[];
  enum?: unknown[];
  default?: unknown;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  description?: string;
  additionalProperties?: boolean;
};

function schemaType(schema: JSONSchema): string | undefined {
  return Array.isArray(schema.type) ? schema.type[0] : schema.type;
}

export function defaultForSchema(schema: JSONSchema): unknown {
  const type = schemaType(schema);
  if (type === "object") {
    const out: Record<string, unknown> = {};
    for (const [name, sub] of Object.entries(schema.properties || {})) {
      if ((schema.required || []).includes(name)) out[name] = defaultForSchema(sub);
    }
    return out;
  }
  if (type === "array") return [];
  if (type === "string") return schema.enum ? schema.enum[0] : "";
  if (type === "number" || type === "integer") return schema.default ?? 0;
  if (type === "boolean") return false;
  return schema.default ?? null;
}

/** 逐字段错误查询——由 state/validation.ts 的 errorIndex 提供。 */
export type FieldErrorLookup = (field: string) => string | undefined;

type FieldProps = {
  schema: JSONSchema;
  value: unknown;
  onChange: (v: unknown) => void;
  name?: string;
  /** 只读（服务端会覆盖客户端提交的值）。 */
  readOnly?: boolean;
  error?: string;
  errorFor?: FieldErrorLookup;
};

// 26 种卡片类型全部共用这一套渲染逻辑——不是漏写了针对每种类型的专属表单，
// 是刻意的：schema 已经把每种卡片的字段/类型/枚举/长度限制声明得足够完整，
// 逐类型手写表单只会带来 26 份几乎相同、又要各自维护的代码，而且这个组件
// 天然覆盖未来任何新增的卡片类型。
export function SchemaField({ schema, value, onChange, name, readOnly, error, errorFor }: FieldProps) {
  const type = schemaType(schema);

  if (schema.enum) {
    return (
      <select
        className="select"
        value={(value as string) ?? (schema.default as string) ?? ""}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
      >
        {schema.enum.map((opt) => (
          <option key={String(opt)} value={String(opt)}>{String(opt)}</option>
        ))}
      </select>
    );
  }

  if (name && isFrameField(name) && (type === "integer" || type === "number")) {
    return <FrameInput value={Number(value) || 0} onChange={(v) => onChange(v)} invalid={!!error} />;
  }
  if (name && isMsField(name) && (type === "integer" || type === "number")) {
    return <MsInput value={Number(value) || 0} onChange={(v) => onChange(v)} invalid={!!error} />;
  }

  switch (type) {
    case "string":
      return (
        <input
          className={`input${error ? " input--invalid" : ""}${readOnly ? " input--pinned" : ""}`}
          value={(value as string) ?? ""}
          maxLength={schema.maxLength}
          readOnly={readOnly}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "number":
    case "integer":
      return (
        <input
          className={`input input--num${error ? " input--invalid" : ""}${readOnly ? " input--pinned" : ""}`}
          type="number"
          value={(value as number) ?? (schema.default as number) ?? 0}
          min={schema.minimum}
          max={schema.maximum}
          readOnly={readOnly}
          onChange={(e) => {
            const raw = e.target.value;
            if (raw === "") return;
            onChange(type === "integer" ? parseInt(raw, 10) : parseFloat(raw));
          }}
        />
      );
    case "boolean":
      return (
        <input
          className="checkbox"
          type="checkbox"
          checked={!!value}
          disabled={readOnly}
          onChange={(e) => onChange(e.target.checked)}
        />
      );
    case "array":
      return (
        <ArrayField
          schema={schema}
          value={(value as unknown[]) || []}
          onChange={onChange as (v: unknown[]) => void}
        />
      );
    case "object":
      return (
        <ObjectField
          schema={schema}
          value={(value as Record<string, unknown>) || {}}
          onChange={onChange as (v: Record<string, unknown>) => void}
          readOnlyFields={readOnly ? Object.keys(schema.properties || {}) : undefined}
          errorFor={errorFor}
        />
      );
    default:
      return <span className="field__hint">(unsupported field type: {String(type)})</span>;
  }
}

export function ObjectField({
  schema, value, onChange, readOnlyFields, errorFor, section,
}: {
  schema: JSONSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  /** 这些字段渲染成只读（服务端 pin 的资源路径）。 */
  readOnlyFields?: string[];
  errorFor?: FieldErrorLookup;
  /** Schema section name (e.g. "countdowns") — lets isAdvancedField keep
   *  x/y/width out of the fold for sections the Phase B overlay can drag.
   *  Absent for non-item objects (presenter, qrContact, ...), which keeps
   *  today's behavior unchanged for them. */
  section?: string;
}) {
  const propNames = Object.keys(schema.properties || {});
  const required = new Set(schema.required || []);
  const ordered = sortFieldNames(propNames);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const basic = ordered.filter((n) => !isAdvancedField(n, section));
  const advanced = ordered.filter((n) => isAdvancedField(n, section));
  const readOnly = new Set(readOnlyFields || []);

  const setField = (fieldName: string, fieldValue: unknown) => {
    onChange({ ...value, [fieldName]: fieldValue });
  };

  const renderField = (n: string) => {
    const err = errorFor?.(n);
    const ro = readOnly.has(n);
    return (
      <div className="field" key={n}>
        <label className={`field__label${required.has(n) ? " field__label--req" : ""}`}>
          {humanLabel(n)}
        </label>
        <SchemaField
          schema={schema.properties![n]}
          value={value[n]}
          onChange={(v) => setField(n, v)}
          name={n}
          readOnly={ro}
          error={err}
        />
        {ro && <div className="field__hint">Set by the server on save — editing has no effect.</div>}
        {err && <div className="field__error">{err}</div>}
      </div>
    );
  };

  return (
    <div className="nest">
      {basic.map(renderField)}
      {advanced.length > 0 && (
        <>
          <button type="button" className="disclosure" onClick={() => setShowAdvanced((s) => !s)}>
            {showAdvanced ? "▾ Hide position & size" : "▸ Position & size"}
          </button>
          {showAdvanced && advanced.map(renderField)}
        </>
      )}
    </div>
  );
}

function ArrayField({
  schema, value, onChange,
}: {
  schema: JSONSchema;
  value: unknown[];
  onChange: (v: unknown[]) => void;
}) {
  const itemSchema = schema.items || {};

  const updateItem = (idx: number, itemValue: unknown) => {
    const next = [...value];
    next[idx] = itemValue;
    onChange(next);
  };
  const removeItem = (idx: number) => onChange(value.filter((_, i) => i !== idx));
  const addItem = () => onChange([...value, defaultForSchema(itemSchema)]);
  const moveItem = (idx: number, dir: -1 | 1) => {
    const target = idx + dir;
    if (target < 0 || target >= value.length) return;
    const next = [...value];
    [next[idx], next[target]] = [next[target], next[idx]];
    onChange(next);
  };

  return (
    <div>
      {value.map((item, idx) => (
        <div className="arrayitem" key={idx}>
          <div className="arrayitem__bar">
            <button type="button" className="btn btn--sm btn--ghost" disabled={idx === 0}
              onClick={() => moveItem(idx, -1)} title="Move up">↑</button>
            <button type="button" className="btn btn--sm btn--ghost" disabled={idx === value.length - 1}
              onClick={() => moveItem(idx, 1)} title="Move down">↓</button>
            <button type="button" className="btn btn--sm btn--ghost btn--danger"
              onClick={() => removeItem(idx)} title="Remove">✕</button>
          </div>
          <SchemaField schema={itemSchema} value={item} onChange={(v) => updateItem(idx, v)} />
        </div>
      ))}
      <button type="button" className="btn btn--sm" onClick={addItem}>+ Add</button>
    </div>
  );
}
