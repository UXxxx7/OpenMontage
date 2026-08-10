import React, { useCallback } from "react";
import { ObjectField, JSONSchema, FieldErrorLookup, defaultForSchema } from "../../SchemaForm";
import { colorForSection, humanLabel, singularLabel } from "../../fieldHints";
import { CAPTION_POSITION_SECTION } from "../../state/model";

/**
 * 选中某一项时显示的面板——一次只显示这一项的字段，不是整整 30 个手风琴。
 * 这是这次重写里最关键的一步：以前每样东西同时展开在屏幕上，用户找不到
 * "我刚点的那个东西在哪"；现在点什么就编辑什么。
 */
export function ClipInspector({
  section,
  index,
  item,
  itemSchema,
  onChange,
  onDuplicate,
  onDelete,
  onClose,
  errorFor,
}: {
  section: string;
  /** null = a top-level OBJECT field (intro/outro/compliance/…), not an array item. */
  index: number | null;
  item: Record<string, unknown>;
  /** Undefined for schema-less chrome (chapterNav, rainbowBar — always
   *  rendered, no authored fields exist yet). The selection is still real;
   *  see the empty-state branch below. */
  itemSchema: JSONSchema | undefined;
  onChange: (next: Record<string, unknown>) => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onClose: () => void;
  errorFor: FieldErrorLookup;
}) {
  const handleChange = useCallback((v: Record<string, unknown>) => onChange(v), [onChange]);
  // Not a content card — there's always exactly one entry (either a real
  // top-level object, or captionPosition's array-of-exactly-one), so
  // Duplicate/Delete (which assume an arbitrary-length array of
  // interchangeable items) don't apply.
  const isSingleton = index === null || section === CAPTION_POSITION_SECTION;

  return (
    <>
      <div className="inspector__header">
        <div className="inspector__title">
          <span className="lane__swatch" style={{ background: colorForSection(section) }} />
          {isSingleton ? singularLabel(section) : `${singularLabel(section)} #${index + 1}`}
          <span className="toolbar__spacer" />
          <button type="button" className="btn btn--icon btn--ghost" onClick={onClose} title="Deselect (Esc)">✕</button>
        </div>
        <div className="inspector__kind">
          {section === CAPTION_POSITION_SECTION
            ? "Drag on the preview to move where captions appear on screen."
            : section === "captions"
            ? "Drag the box on the preview to move where captions appear on screen."
            : humanLabel(section)}
        </div>
      </div>
      <div className="inspector__body">
        {itemSchema ? (
          <>
            <ObjectField
              schema={itemSchema}
              value={item}
              onChange={handleChange}
              errorFor={errorFor}
              section={section}
            />
            {!isSingleton && (
              <div className="inspector__actions">
                <button type="button" className="btn btn--sm" onClick={onDuplicate}>Duplicate</button>
                <button type="button" className="btn btn--sm btn--danger" onClick={onDelete}>Delete</button>
              </div>
            )}
          </>
        ) : (
          <p className="inspector__empty">
            Nothing here to edit yet — this is shown for reference only.
          </p>
        )}
      </div>
    </>
  );
}

export function newItemFor(itemSchema: JSONSchema): Record<string, unknown> {
  return defaultForSchema(itemSchema) as Record<string, unknown>;
}
