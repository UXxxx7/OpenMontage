import React, { useState } from "react";
import { SECTION_GROUPS, humanLabel } from "../../fieldHints";
import { TIME_DESCRIPTORS } from "../../state/model";

/** 只列出真的会出现在时间轴上的字段（TIME_DESCRIPTORS 是从 schema 推导出的）。 */
export function AddMenu({ onAdd }: { onAdd: (section: string) => void }) {
  const [open, setOpen] = useState(false);
  const sections = Object.keys(TIME_DESCRIPTORS);
  const groups = Object.entries(SECTION_GROUPS)
    .map(([g, fields]) => [g, fields.filter((f) => sections.includes(f))] as const)
    .filter(([, fields]) => fields.length > 0);

  return (
    <div style={{ position: "relative" }}>
      <button type="button" className="btn" onClick={() => setOpen((o) => !o)}>
        + Add at playhead
      </button>
      {open && (
        <div
          style={{
            position: "absolute", top: "100%", left: 0, marginTop: 4, zIndex: 10,
            background: "var(--bg-2)", border: "1px solid var(--bg-4)", borderRadius: "var(--radius)",
            padding: 6, minWidth: 220, maxHeight: 320, overflowY: "auto",
            boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
          }}
        >
          {groups.map(([group, fields]) => (
            <div key={group} style={{ marginBottom: 4 }}>
              <div style={{ fontSize: 10, color: "var(--fg-2)", padding: "4px 6px", textTransform: "uppercase" }}>
                {group}
              </div>
              {fields.map((f) => (
                <button
                  key={f}
                  type="button"
                  className="btn btn--ghost btn--sm"
                  style={{ display: "block", width: "100%", textAlign: "left" }}
                  onClick={() => { onAdd(f); setOpen(false); }}
                >
                  {humanLabel(f)}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
