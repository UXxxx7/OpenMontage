import React, { useState } from "react";
import { ObjectField, SchemaField, JSONSchema, FieldErrorLookup } from "../../SchemaForm";
import {
  BASIC_SCALAR_FIELDS, OBJECT_GROUP_FIELDS, SERVER_PINNED_NESTED,
  SERVER_PINNED_TOP_LEVEL, humanLabel,
} from "../../fieldHints";
import type { VideoCut } from "../../../src/cuts";
import { AddMenu } from "./AddMenu";
import { AudioPanel } from "./AudioPanel";
import { CutsPanel } from "./CutsPanel";

/**
 * 没有任何选中项时显示的面板——项目级设置。这不是"退而求其次"的占位符，
 * 是 Figma/Premiere 都在用的模式："选中东西看它的属性，没选中东西看画布/
 * 项目的属性"，不需要额外的标签页或菜单，白得的设计。
 */
export function ProjectInspector({
  schema,
  props,
  onChange,
  onAddItem,
  errorFor,
  cuts,
  sourceDurationFrames,
  onCutsChange,
}: {
  schema: JSONSchema;
  props: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  onAddItem: (section: string) => void;
  errorFor: FieldErrorLookup;
  cuts: VideoCut[];
  sourceDurationFrames: number;
  onCutsChange: (next: VideoCut[]) => void;
}) {
  const [openGroup, setOpenGroup] = useState<string | null>(null);

  return (
    <>
      <div className="inspector__header">
        <div className="inspector__title">Project settings</div>
        <div className="inspector__kind">Nothing selected — click a clip on the timeline to edit it</div>
      </div>
      <div className="inspector__body">
        <div className="section">
          <div className="section__title">Add to timeline</div>
          <AddMenu onAdd={onAddItem} />
        </div>

        <div className="section">
          <div className="section__title">Cuts</div>
          <CutsPanel
            cuts={cuts}
            sourceDurationFrames={sourceDurationFrames}
            onCutsChange={onCutsChange}
          />
        </div>

        <div className="section">
          <div className="section__title">Basics</div>
          {BASIC_SCALAR_FIELDS.filter((n) => schema.properties?.[n]).map((n) => {
            const err = errorFor(n);
            return (
              <div className="field" key={n}>
                <label className="field__label">{humanLabel(n)}</label>
                <SchemaField
                  schema={schema.properties![n]}
                  value={props[n]}
                  onChange={(v) => onChange(n, v)}
                  name={n}
                  error={err}
                />
                {err && <div className="field__error">{err}</div>}
              </div>
            );
          })}
          {[...SERVER_PINNED_TOP_LEVEL].filter((n) => n in props).map((n) => (
            <div className="field" key={n}>
              <label className="field__label">{humanLabel(n)}</label>
              <input className="input input--pinned" readOnly value={String(props[n] ?? "")} />
              <div className="field__hint">Set by the server — read only.</div>
            </div>
          ))}
        </div>

        <div className="section">
          <div className="section__title">Audio</div>
          <AudioPanel props={props} onChange={onChange} />
        </div>

        <div className="section">
          <div className="section__title">Presenter &amp; branding</div>
          {OBJECT_GROUP_FIELDS.filter((n) => schema.properties?.[n]).map((n) => {
            const isOpen = openGroup === n;
            const pinnedField = SERVER_PINNED_NESTED[n];
            return (
              <div key={n} style={{ marginBottom: 6 }}>
                <button
                  type="button"
                  className="disclosure"
                  onClick={() => setOpenGroup(isOpen ? null : n)}
                >
                  {isOpen ? "▾" : "▸"} {humanLabel(n)} {props[n] ? "" : "(not set)"}
                </button>
                {isOpen && (
                  <ObjectField
                    schema={schema.properties![n]}
                    value={(props[n] as Record<string, unknown>) || {}}
                    onChange={(v) => onChange(n, v)}
                    readOnlyFields={pinnedField ? [pinnedField] : undefined}
                    errorFor={(f) => errorFor(`${n}.${f}`)}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
    </>
  );
}
