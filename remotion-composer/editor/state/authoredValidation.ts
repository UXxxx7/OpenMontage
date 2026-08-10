// Client-side validation for Arm B overrides — mirrors state/validation.ts's
// shape (ajv, field-level lookup table) but validates the EFFECTIVE
// (contentDefaults + overrides) value per element, not the raw override
// patch. The patch is deliberately partial (only fields a human touched);
// the schema's `required` list is about the authored default, already
// satisfied before any override exists.

import Ajv2020, { type ErrorObject } from "ajv/dist/2020";
import manifestSchema from "../../../contracts/authored_manifest.schema.json";
import type { ManifestElement } from "./authoredHitTest";

const ajv = new Ajv2020({ allErrors: true, strict: false });

const defs = (manifestSchema as { $defs: Record<string, object> }).$defs;

const KIND_DEF: Record<ManifestElement["kind"], object> = {
  text_block: defs.textBlockFields,
  stat_card: defs.statCardFields,
  image_swap: defs.imageSwapFields,
  broll_window: defs.brollWindowFields,
};

const kindValidators: Record<ManifestElement["kind"], ReturnType<typeof ajv.compile>> = {
  text_block: ajv.compile(KIND_DEF.text_block),
  stat_card: ajv.compile(KIND_DEF.stat_card),
  image_swap: ajv.compile(KIND_DEF.image_swap),
  broll_window: ajv.compile(KIND_DEF.broll_window),
};

const NATIVE_W = 1080;
const NATIVE_H = 1920;

export type AuthoredFieldError = {
  id: string;
  /** null for element-level errors (e.g. geometry) not tied to one input. */
  field: string | null;
  message: string;
};

export type AuthoredValidationResult = {
  valid: boolean;
  errors: AuthoredFieldError[];
};

function fieldNameFromError(err: ErrorObject): string | null {
  const parts = (err.instancePath || "").split("/").filter(Boolean);
  if (parts.length > 0) return parts[0];
  if (err.keyword === "required") {
    const missing = (err.params as { missingProperty?: string }).missingProperty;
    if (missing) return missing;
  }
  return null;
}

/**
 * `effectiveValues[id]` must already be `{ ...contentDefaults(el), ...overrides[id] }`
 * — the same merge AuthoredEditor.tsx's Inspector feeds ObjectField's `value`
 * prop, generalized to every element (not just the selected one), so a bad
 * value on an element the user isn't currently looking at still blocks Save.
 */
export function validateAuthored(
  manifest: ManifestElement[],
  effectiveValues: Record<string, Record<string, unknown>>,
  durationInFrames: number
): AuthoredValidationResult {
  const errors: AuthoredFieldError[] = [];

  for (const el of manifest) {
    const value = effectiveValues[el.id] || {};

    const validate = kindValidators[el.kind];
    if (!validate(value)) {
      for (const err of validate.errors || []) {
        errors.push({ id: el.id, field: fieldNameFromError(err), message: err.message || "Invalid value." });
      }
    }

    // Bounds the JSON schema can't express — geometry inside the canvas,
    // and mountFrame/endFrame both sane and within the scene's own runtime.
    const x = Number(value.x ?? el.x);
    const y = Number(value.y ?? el.y);
    const w = Number(value.w ?? el.w);
    const h = Number(value.h ?? el.h);
    if (x < 0 || y < 0 || x + w > NATIVE_W || y + h > NATIVE_H) {
      errors.push({ id: el.id, field: "x", message: `Off-canvas — must fit inside ${NATIVE_W}×${NATIVE_H}.` });
    }

    const mountFrame = Number(value.mountFrame ?? el.mountFrame ?? 0);
    if (mountFrame < 0 || mountFrame > durationInFrames) {
      errors.push({ id: el.id, field: "mountFrame", message: `Must be between 0 and ${durationInFrames}.` });
    }
    const rawEnd = value.endFrame ?? el.endFrame;
    if (rawEnd != null) {
      const endFrame = Number(rawEnd);
      if (endFrame > durationInFrames) {
        errors.push({ id: el.id, field: "endFrame", message: `Must be at most ${durationInFrames}.` });
      }
      if (endFrame <= mountFrame) {
        errors.push({ id: el.id, field: "endFrame", message: "Must be after the start frame." });
      }
    }
  }

  return { valid: errors.length === 0, errors };
}

/** Field/item lookup table, same shape as state/validation.ts's errorIndex. */
export function authoredErrorIndex(errors: AuthoredFieldError[]) {
  const byField = new Map<string, string>();
  const byItem = new Set<string>();
  for (const e of errors) {
    byItem.add(e.id);
    if (e.field) {
      const key = `${e.id}/${e.field}`;
      if (!byField.has(key)) byField.set(key, e.message);
    }
  }
  return {
    forField: (id: string, field: string): string | undefined => byField.get(`${id}/${field}`),
    forItem: (id: string): boolean => byItem.has(id),
  };
}
