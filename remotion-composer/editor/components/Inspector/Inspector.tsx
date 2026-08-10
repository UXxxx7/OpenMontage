import React from "react";
import type { JSONSchema, FieldErrorLookup } from "../../SchemaForm";
import type { VideoCut } from "../../../src/cuts";
import { itemAt } from "../../state/model";
import { ClipInspector } from "./ClipInspector";
import { ProjectInspector } from "./ProjectInspector";

// index: null selects a top-level OBJECT field (intro/outro/compliance/…),
// not an array item — see state/model.ts's itemAt() for the read side.
export type Selection = { section: string; index: number | null } | null;

/**
 * 顶层切换器：有选中项显示 ClipInspector，没有显示 ProjectInspector。
 * 这一个组件 + 一个 selection 状态，取代了原来同时铺开的 ~30 个手风琴。
 */
export function Inspector({
  schema,
  props,
  selection,
  onSelectionChange,
  onFieldChange,
  onItemChange,
  onAddItem,
  onDuplicateItem,
  onDeleteItem,
  errorFor,
  errorForItem,
  className,
  cuts,
  sourceDurationFrames,
  onCutsChange,
}: {
  schema: JSONSchema;
  props: Record<string, unknown>;
  selection: Selection;
  onSelectionChange: (sel: Selection) => void;
  onFieldChange: (name: string, value: unknown) => void;
  onItemChange: (section: string, index: number | null, next: Record<string, unknown>) => void;
  onAddItem: (section: string) => void;
  onDuplicateItem: (section: string, index: number) => void;
  onDeleteItem: (section: string, index: number) => void;
  errorFor: FieldErrorLookup;
  errorForItem: (section: string, index: number | null) => FieldErrorLookup;
  className?: string;
  /** Cuts editing is project-level, not per-clip — only ever reaches ProjectInspector below. */
  cuts: VideoCut[];
  sourceDurationFrames: number;
  onCutsChange: (next: VideoCut[]) => void;
}) {
  const rootClass = `inspector app__inspector${className ? ` ${className}` : ""}`;

  if (selection) {
    const { section, index } = selection;
    const item = itemAt(props, section, index);
    // index===null selects the top-level OBJECT's own schema; a real index
    // selects that array's item schema — same split as itemAt's read side.
    const itemSchema = index === null ? schema.properties?.[section] : schema.properties?.[section]?.items;
    if (itemSchema && !item) {
      // A schema exists for this section but the item itself is gone — it
      // vanished in a prior edit (deleted/relayout). Back out to the project
      // panel rather than rendering a form pointed at nothing.
      return (
        <div className={rootClass}>
          <ProjectInspector
            schema={schema}
            props={props}
            onChange={onFieldChange}
            onAddItem={onAddItem}
            errorFor={errorFor}
            cuts={cuts}
            sourceDurationFrames={sourceDurationFrames}
            onCutsChange={onCutsChange}
          />
        </div>
      );
    }
    // Static chrome with no authored fields yet (chapterNav, rainbowBar) has
    // no itemSchema AND no item — that's a real, current selection, not a
    // vanished one. ClipInspector shows its own empty state for it rather
    // than falling back here, which would be indistinguishable from "nothing
    // selected" and is exactly the "click does nothing" bug this exists to
    // avoid.
    return (
      <div className={rootClass}>
        <ClipInspector
          section={section}
          index={index}
          item={item ?? {}}
          itemSchema={itemSchema}
          onChange={(next) => onItemChange(section, index, next)}
          onDuplicate={() => { if (index !== null) onDuplicateItem(section, index); }}
          onDelete={() => { if (index !== null) onDeleteItem(section, index); }}
          onClose={() => onSelectionChange(null)}
          errorFor={errorForItem(section, index)}
        />
      </div>
    );
  }

  return (
    <div className={rootClass}>
      <ProjectInspector
        schema={schema}
        props={props}
        onChange={onFieldChange}
        onAddItem={onAddItem}
        errorFor={errorFor}
        cuts={cuts}
        sourceDurationFrames={sourceDurationFrames}
        onCutsChange={onCutsChange}
      />
    </div>
  );
}
